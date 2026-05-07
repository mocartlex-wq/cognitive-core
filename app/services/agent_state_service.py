"""Per-agent state checkpoint service.

Решает: при срыве сессии Claude Desktop / окончании токенов агент в новом
сеансе восстанавливает контекст одной командой `cognitive_continue`.

Архитектура (по DeepSeek-консультации):
- НЕ дублирует OP (24h TTL session) — это персистентное состояние
- НЕ создаёт новый L-слой — расширение L3/L5
- state_data sanitize (256KB limit, SQL/JS/XSS защита)
- Гибридный trigger: manual + auto (5min heartbeat) + session_close
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from app.db.postgres import get_pool
from app.security.sanitizer import sanitize_payload


# Лимит state_data — как у L1 payload, для consistency
MAX_STATE_SIZE_BYTES = 256 * 1024


async def save_checkpoint(
    agent_id: str,
    current_task: str | None = None,
    state_data: dict | None = None,
    active_session_ids: list[str] | None = None,
    notes: str | None = None,
    trigger: str = "manual",
) -> dict:
    """Сохранить checkpoint агента.

    1. Sanitize state_data (защита от инъекций)
    2. Проверить лимит размера
    3. UPSERT в agent_states (главная запись)
    4. INSERT в agent_state_history (для отката)

    trigger: manual | auto | heartbeat | session_close | event_milestone
    """
    state_data = state_data or {}

    # Sanitize (использует тот же конвейер что L1)
    # sanitize_payload возвращает SanitizeResult{payload, warnings}
    sanitize_res = sanitize_payload(state_data)
    state_data = sanitize_res.payload if hasattr(sanitize_res, 'payload') else sanitize_res

    # Size check
    serialized = json.dumps(state_data, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > MAX_STATE_SIZE_BYTES:
        raise ValueError(
            f"state_data exceeds {MAX_STATE_SIZE_BYTES} bytes "
            f"(got {len(serialized.encode('utf-8'))} bytes). "
            "Trim it or store large blobs separately."
        )

    # active_session_ids → UUID
    session_uuids = []
    if active_session_ids:
        for sid in active_session_ids:
            try:
                session_uuids.append(uuid.UUID(str(sid)))
            except ValueError:
                pass  # skip invalid

    if trigger not in ("manual", "auto", "heartbeat", "session_close", "event_milestone"):
        trigger = "manual"

    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # UPSERT main state
            await conn.execute(
                """
                INSERT INTO agent_states (
                    agent_id, current_task, state_data, active_session_ids,
                    last_checkpoint_at, total_checkpoints, notes, updated_at
                ) VALUES ($1, $2, $3, $4, $5, 1, $6, $5)
                ON CONFLICT (agent_id) DO UPDATE
                SET current_task = COALESCE(EXCLUDED.current_task, agent_states.current_task),
                    state_data = EXCLUDED.state_data,
                    active_session_ids = EXCLUDED.active_session_ids,
                    last_checkpoint_at = EXCLUDED.last_checkpoint_at,
                    total_checkpoints = agent_states.total_checkpoints + 1,
                    notes = COALESCE(EXCLUDED.notes, agent_states.notes),
                    updated_at = EXCLUDED.updated_at
                """,
                agent_id, current_task, serialized, session_uuids, now, notes,
            )
            # INSERT history snapshot
            await conn.execute(
                """
                INSERT INTO agent_state_history (
                    agent_id, current_task, state_data, active_session_ids, checkpoint_at, trigger
                ) VALUES ($1, $2, $3, $4, $5, $6)
                """,
                agent_id, current_task, serialized, session_uuids, now, trigger,
            )

    return {
        "agent_id": agent_id,
        "saved_at": now.isoformat(),
        "trigger": trigger,
        "state_size_bytes": len(serialized.encode("utf-8")),
    }


async def restore_state(
    agent_id: str,
    include_recent_events: int = 10,
    include_recent_knowledge: int = 5,
) -> dict:
    """Восстановить состояние агента — `cognitive_continue` для MCP.

    Возвращает:
      - last checkpoint (current_task, state_data, active_session_ids)
      - recent_events: последние N L1-событий этого агента
      - recent_knowledge: последние N L3-знаний из доменов где работал агент
      - meta: total_events, total_checkpoints, last_checkpoint_at
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Main state
        row = await conn.fetchrow(
            """
            SELECT agent_id, current_task, state_data, active_session_ids,
                   last_checkpoint_at, total_events, total_checkpoints, notes
            FROM agent_states WHERE agent_id = $1
            """,
            agent_id,
        )
        if not row:
            return {
                "agent_id": agent_id,
                "exists": False,
                "message": (
                    f"No saved state for agent '{agent_id}'. "
                    "First call cognitive_save_state to create checkpoint."
                ),
                "recent_events": [],
                "recent_knowledge": [],
            }

        state = dict(row)
        # Parse state_data jsonb
        if isinstance(state["state_data"], str):
            try: state["state_data"] = json.loads(state["state_data"])
            except: state["state_data"] = {}
        state["active_session_ids"] = [str(s) for s in (state["active_session_ids"] or [])]
        state["last_checkpoint_at"] = state["last_checkpoint_at"].isoformat() if state["last_checkpoint_at"] else None

        # Recent events
        ev_rows = await conn.fetch(
            """
            SELECT id, timestamp, domain, raw_payload
            FROM l1_raw_events
            WHERE source_agent = $1
            ORDER BY timestamp DESC LIMIT $2
            """,
            agent_id, include_recent_events,
        )
        recent_events = [
            {
                "id": str(r["id"]),
                "timestamp": r["timestamp"].isoformat(),
                "domain": r["domain"],
                "payload": r["raw_payload"] if not isinstance(r["raw_payload"], str)
                          else (json.loads(r["raw_payload"]) if r["raw_payload"] else {}),
            }
            for r in ev_rows
        ]

        # Recent knowledge from agent's domains
        domains = list({e["domain"] for e in recent_events}) if recent_events else []
        recent_knowledge = []
        if domains:
            kn_rows = await conn.fetch(
                """
                SELECT id, domain, knowledge_type, content, version, effective_from
                FROM l3_master_knowledge
                WHERE domain = ANY($1::text[]) AND effective_to IS NULL
                ORDER BY effective_from DESC LIMIT $2
                """,
                domains, include_recent_knowledge,
            )
            for r in kn_rows:
                content = r["content"]
                if isinstance(content, str):
                    try: content = json.loads(content)
                    except: pass
                recent_knowledge.append({
                    "id": str(r["id"]),
                    "domain": r["domain"],
                    "type": r["knowledge_type"],
                    "content": content,
                    "version": r["version"],
                })

    return {
        "agent_id": agent_id,
        "exists": True,
        "current_task": state["current_task"],
        "state_data": state["state_data"],
        "active_session_ids": state["active_session_ids"],
        "last_checkpoint_at": state["last_checkpoint_at"],
        "total_events": state["total_events"],
        "total_checkpoints": state["total_checkpoints"],
        "notes": state["notes"],
        "recent_events": recent_events,
        "recent_knowledge": recent_knowledge,
        "domains_active": domains,
    }


async def get_history(agent_id: str, limit: int = 20) -> list[dict]:
    """История checkpoints — для отката или анализа."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, current_task, state_data, active_session_ids, checkpoint_at, trigger
            FROM agent_state_history
            WHERE agent_id = $1
            ORDER BY checkpoint_at DESC LIMIT $2
            """,
            agent_id, limit,
        )
    result = []
    for r in rows:
        sd = r["state_data"]
        if isinstance(sd, str):
            try: sd = json.loads(sd)
            except: sd = {}
        result.append({
            "id": str(r["id"]),
            "current_task": r["current_task"],
            "state_size_bytes": len(json.dumps(sd, ensure_ascii=False).encode("utf-8")),
            "active_session_ids": [str(s) for s in (r["active_session_ids"] or [])],
            "checkpoint_at": r["checkpoint_at"].isoformat(),
            "trigger": r["trigger"],
        })
    return result


async def list_all_agents() -> list[dict]:
    """Все агенты с текущим state — для дашборда."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id, current_task, last_checkpoint_at,
                   total_events, total_checkpoints, updated_at,
                   array_length(active_session_ids, 1) AS active_sessions
            FROM agent_states
            ORDER BY updated_at DESC
            """
        )
    return [
        {
            "agent_id": r["agent_id"],
            "current_task": r["current_task"][:120] if r["current_task"] else None,
            "last_checkpoint_at": r["last_checkpoint_at"].isoformat() if r["last_checkpoint_at"] else None,
            "active_sessions": r["active_sessions"] or 0,
            "total_events": r["total_events"],
            "total_checkpoints": r["total_checkpoints"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]
