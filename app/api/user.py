"""Endpoints для работы с собственным аккаунтом и его ресурсами.

  GET    /user/me               — текущий профиль (id, email, display_name, is_admin)
  PATCH  /user/me               — обновить display_name / avatar_url
  GET    /user/rooms            — мои комнаты (где я владелец или участник)
  GET    /user/agents           — мои помощники
  POST   /user/agents/create    — создать нового помощника (привязать к этому user)
  DELETE /user/account          — soft-delete аккаунта (30-дневная отсрочка)

Все требуют валидную сессию через require_user.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/user", tags=["user"])


# ─────────────────────────────────────────────────────────────────────────
# /user/me
# ─────────────────────────────────────────────────────────────────────────
@router.get("/me")
async def get_me(request: Request):
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id::text AS user_id, email, display_name, avatar_url,
                   is_admin, email_verified, created_at, last_login_at
              FROM accounts WHERE user_id = $1::uuid
            """,
            user.user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    data = dict(row)
    for k in ("created_at", "last_login_at"):
        v = data.get(k)
        if isinstance(v, datetime):
            data[k] = v.isoformat()
    return data


class UpdateProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, max_length=80)
    avatar_url: str | None = Field(None, max_length=500)


@router.patch("/me")
async def patch_me(body: UpdateProfileBody, request: Request):
    user = await require_user(request)
    sets: list[str] = []
    args: list[Any] = []
    if body.display_name is not None:
        args.append(body.display_name.strip() or None)
        sets.append(f"display_name = ${len(args)}")
    if body.avatar_url is not None:
        url = body.avatar_url.strip() or None
        if url and not (url.startswith("https://") or url.startswith("http://")):
            raise HTTPException(status_code=400, detail="avatar_url должен начинаться с https://")
        args.append(url)
        sets.append(f"avatar_url = ${len(args)}")
    if not sets:
        return {"ok": True, "updated": 0}

    args.append(user.user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE user_id = ${len(args)}::uuid",
            *args,
        )
    return {"ok": True, "updated": len(sets)}


# ─────────────────────────────────────────────────────────────────────────
# /user/rooms — мои комнаты
# ─────────────────────────────────────────────────────────────────────────
@router.get("/rooms")
async def my_rooms(request: Request):
    """Список комнат пользователя — где он владелец ИЛИ участник.

    Возвращает пустой список если таблицы rooms ещё нет (cognitive-rooms.py
    не задеплоен — в этом случае возвращаем 200 + items=[]).
    """
    user = await require_user(request)
    pool = await get_pool()
    items: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT DISTINCT r.id::text AS id, r.name, r.created_at,
                       (r.owner_user_id = $1::uuid) AS is_owner,
                       COALESCE(r.is_public, TRUE) AS is_public
                  FROM rooms r
                  LEFT JOIN room_participants p
                         ON p.room_id = r.id
                 WHERE r.owner_user_id = $1::uuid
                    OR p.user_id = $1::uuid
                    OR p.agent_id IN (
                        SELECT agent_id FROM agent_states WHERE owner_user_id = $1::uuid
                    )
                 ORDER BY r.created_at DESC
                 LIMIT 200
                """,
                user.user_id,
            )
            for r in rows:
                d = dict(r)
                if isinstance(d.get("created_at"), datetime):
                    d["created_at"] = d["created_at"].isoformat()
                items.append(d)
        except Exception as e:
            # rooms таблица может ещё не существовать в этой БД
            logger.info("my_rooms_skip user_id=%s err=%s", user.user_id, e)

    return {"count": len(items), "items": items}


# ─────────────────────────────────────────────────────────────────────────
# /user/agents — мои помощники
# ─────────────────────────────────────────────────────────────────────────
@router.get("/agents")
async def my_agents(request: Request):
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id, current_task, project, machine, capabilities,
                   last_heartbeat_at, total_events, total_checkpoints, updated_at,
                   last_mcp_connect_at, last_mcp_disconnect_at,
                   first_mcp_connect_at,
                   machine_fingerprint, machine_label,
                   -- Presence: MCP-online если connect в последние 60 сек
                   (last_mcp_connect_at IS NOT NULL
                    AND last_mcp_connect_at > NOW() - INTERVAL '60 seconds') AS mcp_online
              FROM agent_states
             WHERE owner_user_id = $1::uuid
             ORDER BY machine_fingerprint NULLS LAST,
                      mcp_online DESC,
                      last_heartbeat_at DESC NULLS LAST
            """,
            user.user_id,
        )
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("last_heartbeat_at", "updated_at", "last_mcp_connect_at",
                  "last_mcp_disconnect_at", "first_mcp_connect_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        items.append(d)

    # v3: группируем по machine_fingerprint в отдельной структуре для UI
    # (агенты без fp идут в группу «legacy» — те что без installer-а)
    machines: dict[str, dict[str, Any]] = {}
    for item in items:
        fp = item.get("machine_fingerprint") or "_no_machine"
        if fp not in machines:
            machines[fp] = {
                "machine_fingerprint": item.get("machine_fingerprint"),
                "machine_label": item.get("machine_label") or "Без машины",
                "agents": [],
                "any_online": False,
            }
        machines[fp]["agents"].append(item)
        if item.get("mcp_online"):
            machines[fp]["any_online"] = True

    return {
        "count": len(items),
        "items": items,  # backward compat — flat list
        "machines": list(machines.values()),  # v3 — grouped by machine
    }


class CreateAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[\w.\-]+$")
    description: str | None = Field(None, max_length=300)
    project: str | None = Field(None, max_length=64)
    machine: str | None = Field(None, max_length=128)
    capabilities: list[str] | None = None


async def _create_agent_core(user, body: CreateAgentBody) -> dict:
    """Реализация регистрации помощника. Reusable из других routers
    (connect.py wizard) — без HTTP-hop, без повторного require_user.

    Параметры:
        user: объект пользователя с .user_id (из require_user / session)
        body: CreateAgentBody уже валидированный

    Returns:
        dict с полями ok, agent_id, api_key, warning.

    Raises:
        HTTPException 409 если agent_id занят.
    """
    import json as _json

    api_key = secrets.token_urlsafe(32)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Проверка уникальности
        existing = await conn.fetchval(
            "SELECT 1 FROM agent_states WHERE agent_id = $1", body.agent_id,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Помощник с id «{body.agent_id}» уже существует. Выберите другой id.",
            )

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_states
                    (agent_id, owner_user_id, project, machine, capabilities, notes)
                VALUES ($1, $2::uuid, $3, $4, $5::jsonb, $6)
                """,
                body.agent_id,
                user.user_id,
                body.project,
                body.machine,
                _json.dumps(body.capabilities or [], ensure_ascii=False),
                body.description,
            )
            await conn.execute(
                """
                INSERT INTO agent_keys (api_key, agent_id, description, owner_user_id)
                VALUES ($1, $2, $3, $4::uuid)
                """,
                api_key, body.agent_id, body.description, user.user_id,
            )

    # v3+: lifecycle event — owner видит «agent_id зарегистрирован» в списке
    # событий, можно использовать для billing/audit (когда какой agent был создан)
    try:
        import json as _j
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO l1_raw_events (source_agent, domain, raw_payload) VALUES ($1, $2, $3::jsonb)",
                body.agent_id, "agent_lifecycle",
                _j.dumps({
                    "event": "agent_created",
                    "task": "agent зарегистрирован",
                    "project": body.project or "?",
                    "machine": body.machine,
                    "description": body.description,
                }, ensure_ascii=False),
            )
    except Exception:
        pass  # lifecycle event — не критично если упало

    logger.info(
        "agent_created user_id=%s agent_id=%s project=%s",
        user.user_id, body.agent_id, body.project or "?",
    )
    return {
        "ok": True,
        "agent_id": body.agent_id,
        "api_key": api_key,
        "warning": "Сохраните api_key — больше его показать нельзя.",
    }


@router.get("/agents/{agent_id}/events")
async def agent_events(agent_id: str, request: Request, limit: int = 20):
    """Список последних L1-событий конкретного помощника.

    Owner может развернуть card агента → увидеть что он делал. Используется
    для:
    - Audit log (история действий)
    - Billing readiness — счётчик event-ов под подписку/тариф
    - Debug — понять чем агент занят

    Owner-check: проверяем что agent принадлежит user'у.
    """
    user = await require_user(request)
    limit = max(1, min(int(limit), 100))
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")
        rows = await conn.fetch(
            """
            SELECT id::text AS id, domain, raw_payload, timestamp::text AS timestamp
              FROM l1_raw_events
             WHERE source_agent = $1
             ORDER BY timestamp DESC
             LIMIT $2
            """,
            agent_id, limit,
        )
        items = [dict(r) for r in rows]

        # Synthetic «agent_created» event для legacy агентов без lifecycle-записи.
        # Берётся из agent_states.updated_at (когда впервые зарегистрирован).
        # Cмотрим есть ли уже lifecycle event с domain=agent_lifecycle — если нет,
        # добавляем синтетический в конец списка.
        has_lifecycle = any(
            (i.get("domain") == "agent_lifecycle") for i in items
        )
        if not has_lifecycle:
            meta = await conn.fetchrow(
                """
                SELECT updated_at::text AS created, machine_label, project, notes
                  FROM agent_states WHERE agent_id = $1
                """,
                agent_id,
            )
            if meta:
                items.append({
                    "id": "synthetic-lifecycle",
                    "domain": "agent_lifecycle",
                    "raw_payload": {
                        "event": "agent_registered",
                        "task": "agent зарегистрирован",
                        "project": meta["project"] or "—",
                        "machine": meta["machine_label"] or "—",
                        "description": meta["notes"] or None,
                        "synthetic": True,
                    },
                    "timestamp": meta["created"],
                })

    return {
        "agent_id": agent_id,
        "count": len(items),
        "limit": limit,
        "events": items,
    }


@router.get("/usage")
async def get_usage(request: Request):
    """PR #23 multi-tenant: per-owner usage summary для profile.html карточки.

    Возвращает tier + (events/storage/agents): used/max/pct.
    Если owner_quotas строка не существует — fallback к defaults free-tier.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tier, max_events_per_day, max_storage_mb, max_agents,
                   max_recall_per_min, events_today, storage_mb_now,
                   agents_count, suspended
              FROM owner_quotas
             WHERE owner_user_id = $1::uuid
            """,
            user.user_id,
        )
    if not row:
        # Триггер ensure_owner_quota должен был создать строку при регистрации,
        # но если миграция 0007 ещё не применилась — возвращаем сырые defaults.
        return {
            "tier": "free",
            "events": {"used": 0, "max": 10000, "pct": 0},
            "storage_mb": {"used": 0, "max": 1024, "pct": 0},
            "agents": {"used": 0, "max": 10, "pct": 0},
            "suspended": False,
            "note": "owner_quotas row не найдена (migration 0007 не применена?)",
        }
    return {
        "tier": row["tier"],
        "events": {
            "used": row["events_today"],
            "max": row["max_events_per_day"],
            "pct": round(100 * row["events_today"] / max(1, row["max_events_per_day"]), 1),
        },
        "storage_mb": {
            "used": round(float(row["storage_mb_now"]), 2),
            "max": row["max_storage_mb"],
            "pct": round(100 * float(row["storage_mb_now"]) / max(1, row["max_storage_mb"]), 1),
        },
        "agents": {
            "used": row["agents_count"],
            "max": row["max_agents"],
            "pct": round(100 * row["agents_count"] / max(1, row["max_agents"]), 1),
        },
        "suspended": bool(row["suspended"]),
    }


@router.post("/agents/create")
async def create_agent(body: CreateAgentBody, request: Request):
    """Зарегистрировать нового помощника и выдать ему API key.

    Помощник сразу привязан к текущему пользователю (owner_user_id).
    Возвращает api_key один раз — после этого его нельзя увидеть снова.

    Тонкий wrapper над _create_agent_core() для reuse из connect.py wizard.
    """
    user = await require_user(request)
    return await _create_agent_core(user, body)


class PatchAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_label: str | None = Field(None, max_length=128)
    description: str | None = Field(None, max_length=300)
    project: str | None = Field(None, max_length=64)


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, body: PatchAgentBody, request: Request):
    """Переименовать машину / описание helper-а. Только owner может."""
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")
        # Build dynamic UPDATE
        sets = []
        vals = []
        if body.machine_label is not None:
            sets.append(f"machine_label = ${len(vals)+1}")
            vals.append(body.machine_label)
        if body.description is not None:
            sets.append(f"notes = ${len(vals)+1}")
            vals.append(body.description)
        if body.project is not None:
            sets.append(f"project = ${len(vals)+1}")
            vals.append(body.project)
        if not sets:
            return {"ok": True, "changed": 0}
        sets.append("updated_at = NOW()")
        vals.append(agent_id)
        await conn.execute(
            f"UPDATE agent_states SET {', '.join(sets)} WHERE agent_id = ${len(vals)}",
            *vals,
        )
    logger.info("agent_patched user=%s agent=%s fields=%s", user.user_id, agent_id, len(sets) - 1)
    return {"ok": True, "agent_id": agent_id}


@router.get("/media")
async def my_media(request: Request, limit: int = 24):
    """Список последних загрузок media владельца (через cogmedia или /ui/admin/media).

    Для admin показываем все L1 события domain=media_analysis (owner-key auth
    не имеет user_id binding, но admin владеет всем). Для не-admin —
    события где source_agent принадлежит этому user_id.
    """
    user = await require_user(request)
    limit = max(1, min(int(limit), 100))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user.is_admin:
            # Admin видит ВСЕ медиа на сервере
            rows = await conn.fetch(
                """
                SELECT id::text AS id, source_agent, raw_payload, timestamp::text AS timestamp
                  FROM l1_raw_events
                 WHERE domain = 'media_analysis'
                 ORDER BY timestamp DESC
                 LIMIT $1
                """,
                limit,
            )
        else:
            # Non-admin: только своих агентов
            rows = await conn.fetch(
                """
                SELECT e.id::text AS id, e.source_agent, e.raw_payload, e.timestamp::text AS timestamp
                  FROM l1_raw_events e
                  JOIN agent_states ast ON ast.agent_id = e.source_agent
                 WHERE e.domain = 'media_analysis'
                   AND ast.owner_user_id = $1::uuid
                 ORDER BY e.timestamp DESC
                 LIMIT $2
                """,
                user.user_id, limit,
            )
    items = []
    for r in rows:
        d = dict(r)
        payload = d.get("raw_payload")
        if isinstance(payload, str):
            try:
                import json as _j
                payload = _j.loads(payload)
            except Exception:
                payload = {}
        d["raw_payload"] = payload
        # Compact summary fields for UI
        d["media_id"] = payload.get("media_id", "?") if isinstance(payload, dict) else "?"
        d["kind"] = payload.get("kind", "?") if isinstance(payload, dict) else "?"
        d["filename"] = payload.get("filename", "?") if isinstance(payload, dict) else "?"
        d["size_bytes"] = payload.get("size_bytes", 0) if isinstance(payload, dict) else 0
        # Thumbnail URL
        if d["kind"] == "video" and isinstance(payload, dict):
            frames = payload.get("frames", [])
            d["thumbnail"] = frames[0].get("url", "") if frames else ""
        elif d["kind"] == "image" and isinstance(payload, dict):
            d["thumbnail"] = payload.get("url", "")
        else:
            d["thumbnail"] = ""  # audio has no thumb
        d["transcript"] = (payload.get("transcript") or "") if isinstance(payload, dict) else ""
        # TTL: 15 мин с момента upload. После — MinIO файлы удалены, metadata
        # остаётся. UI показывает chip «удалён» вместо thumbnail.
        d["cleaned_up"] = bool(payload.get("cleaned_up")) if isinstance(payload, dict) else False
        items.append(d)
    return {"count": len(items), "items": items, "ttl_minutes": 15}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    """Удалить помощника: revoke все api_keys + удалить agent_states.

    Hard delete — записи L1/L2 от этого agent_id остаются (история не теряется),
    но agent больше не может писать новые события (key revoked).
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")
        async with conn.transaction():
            # Revoke all api_keys (soft revoke — last_used_at + revoked_at = NOW)
            await conn.execute(
                "UPDATE agent_keys SET revoked_at = NOW() WHERE agent_id = $1 AND revoked_at IS NULL",
                agent_id,
            )
            # Hard delete agent_states row
            await conn.execute(
                "DELETE FROM agent_states WHERE agent_id = $1",
                agent_id,
            )
    logger.info("agent_deleted user=%s agent=%s", user.user_id, agent_id)
    return {"ok": True, "agent_id": agent_id, "message": "Помощник удалён, api_keys revoke'нуты"}


# ─────────────────────────────────────────────────────────────────────────
# /user/account — soft delete
# ─────────────────────────────────────────────────────────────────────────
@router.delete("/account")
async def delete_account(request: Request):
    """Soft delete аккаунта с 30-дневной отсрочкой.

    • accounts.deleted_at = NOW() — аккаунт «помечен на удаление»
    • Все сессии отозваны
    • Через 30 дней worker.py физически удалит row (CASCADE по FK)

    Безопасность: НЕ удаляем сразу, чтобы дать восстановить если случайно.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE accounts SET deleted_at = NOW() WHERE user_id = $1::uuid",
                user.user_id,
            )
            await conn.execute(
                "UPDATE sessions SET revoked = TRUE, revoked_at = NOW() "
                "WHERE user_id = $1::uuid AND NOT revoked",
                user.user_id,
            )
    logger.warning("account_soft_deleted user_id=%s email=%s", user.user_id, user.email)
    return {
        "ok": True,
        "deleted_at": datetime.utcnow().isoformat(),
        "will_be_removed_in_days": 30,
        "message": "Аккаунт помечен на удаление. Чтобы восстановить — напишите на support до окончания 30 дней.",
    }
