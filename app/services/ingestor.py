import json
from uuid import UUID, uuid4
from datetime import datetime, timezone
from app.db.postgres import get_pool
from app.models.event import RawEventInput


async def save_raw_event(agent_id: str, domain: str, payload: dict) -> UUID:
    """Сохраняет сырое событие в L1. Возвращает ID."""
    event_id = uuid4()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO l1_raw_events (id, timestamp, source_agent, domain, raw_payload, created_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            event_id,
            now,
            agent_id,
            domain,
            json.dumps(payload, ensure_ascii=False),
            now,
        )
    return event_id


async def get_unprocessed_events(since_hours: int, domain: str | None = None) -> list[dict]:
    """Возвращает необработанные L1-события за последние N часов."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if domain:
            rows = await conn.fetch(
                """
                SELECT id, timestamp, source_agent, domain, raw_payload
                FROM l1_raw_events
                WHERE processed_to_l2 = FALSE
                  AND timestamp >= NOW() - ($1 || ' hours')::INTERVAL
                  AND domain = $2
                ORDER BY timestamp
                """,
                str(since_hours),
                domain,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, timestamp, source_agent, domain, raw_payload
                FROM l1_raw_events
                WHERE processed_to_l2 = FALSE
                  AND timestamp >= NOW() - ($1 || ' hours')::INTERVAL
                ORDER BY timestamp
                """,
                str(since_hours),
            )
        return [dict(r) for r in rows]


async def mark_events_processed(event_ids: list[UUID]) -> None:
    """Помечает L1-события как обработанные."""
    if not event_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE l1_raw_events SET processed_to_l2 = TRUE
            WHERE id = ANY($1)
            """,
            event_ids,
        )
