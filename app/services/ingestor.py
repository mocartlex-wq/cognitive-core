import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.db.postgres import get_pool


async def save_raw_event(
    agent_id: str,
    domain: str,
    payload: dict,
    owner_user_id: str | None = None,
) -> UUID:
    """Сохраняет сырое событие в L1. Возвращает ID.

    PR #23 multi-tenant: owner_user_id обязателен для tenant-isolation.
    Если не передан явно — резолвим через agent_states.owner_user_id
    (best-effort fallback для legacy callers что ещё не обновлены).
    """
    event_id = uuid4()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Fallback: резолвим owner из agent_states если caller не передал
        if owner_user_id is None:
            owner_user_id = await conn.fetchval(
                "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1 LIMIT 1",
                agent_id,
            )
        await conn.execute(
            """
            INSERT INTO l1_raw_events
                (id, timestamp, source_agent, owner_user_id, domain, raw_payload, created_at)
            VALUES ($1, $2, $3, $4::uuid, $5, $6, $7)
            """,
            event_id,
            now,
            agent_id,
            owner_user_id,
            domain,
            json.dumps(payload, ensure_ascii=False),
            now,
        )
    return event_id


async def get_unprocessed_events(
    since_hours: int,
    domain: str | None = None,
    owner_user_id: str | None = None,
) -> list[dict]:
    """Возвращает необработанные L1-события за последние N часов.

    PR #23: добавлен owner_user_id фильтр. Если None — consolidator
    обрабатывает все (admin режим). Производственный путь — всегда
    передаём owner чтобы L1→L2 свёртки строились per-owner.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Build WHERE dynamically — meet either domain, owner, both, or neither
        clauses = [
            "processed_to_l2 = FALSE",
            "timestamp >= NOW() - ($1 || ' hours')::INTERVAL",
        ]
        params: list = [str(since_hours)]
        if domain:
            params.append(domain)
            clauses.append(f"domain = ${len(params)}")
        if owner_user_id:
            params.append(owner_user_id)
            clauses.append(f"owner_user_id = ${len(params)}::uuid")
        sql = (
            "SELECT id, timestamp, source_agent, owner_user_id::text AS owner_user_id, "
            "       domain, raw_payload "
            "FROM l1_raw_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp"
        )
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def mark_events_processed(event_ids: list[UUID], conn=None) -> None:
    """Помечает L1-события как обработанные.

    Если передан `conn`, выполняется на нём (для атомарной связки с
    INSERT в L2 — иначе была дыра: INSERT L2 успешен, UPDATE флага падает
    → события дублируются на следующем daily-цикле)."""
    if not event_ids:
        return
    sql = (
        "UPDATE l1_raw_events SET processed_to_l2 = TRUE "
        "WHERE id = ANY($1)"
    )
    if conn is not None:
        await conn.execute(sql, event_ids)
        return
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute(sql, event_ids)
