import logging
from fastapi import APIRouter, Request, Depends
from app.models.event import RawEventInput, EventResponse
from app.security.auth import verify_api_key, check_rate_limit
from app.security.sanitizer import sanitize_payload
from app.services.ingestor import save_raw_event
from app.security.audit import log_audit
from app.db.postgres import get_pool
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventResponse)
async def ingest_event(payload: RawEventInput, request: Request):
    """6-шаговый пайплайн приёма события: auth → rate limit → schema → sanitize → save → audit.

    Дополнительно: после save в L1 — пишем в replication_outbox для NATS-based
    server→local replication (DS-architecture, Hybrid pattern). Если outbox или
    NATS недоступны — основной flow не страдает.
    """
    # Шаг 1: Аутентификация
    agent_id = await verify_api_key(request)

    # Шаг 2: Rate limiting
    await check_rate_limit(agent_id)

    # Шаг 3: Валидация схемы — уже выполнена Pydantic (RawEventInput)

    # Шаг 4: Санитизация payload
    sanitized = sanitize_payload(payload.payload)

    # Шаг 5: Сохранение в L1
    now = datetime.now(timezone.utc)
    event_id = await save_raw_event(agent_id, payload.domain, sanitized.payload)

    # Шаг 5b: Replication outbox — failure не блокирует основной flow
    try:
        from app.replication import write_outbox_event
        pool = await get_pool()
        async with pool.acquire() as conn:
            await write_outbox_event(
                conn,
                kind="l1_event",
                payload={
                    "id": str(event_id),
                    "agent_id": agent_id,
                    "domain": payload.domain,
                    "payload": sanitized.payload,
                    "created_at": now.isoformat(),
                },
            )
    except Exception as e:
        logger.warning("outbox write failed for event %s: %s", event_id, e)

    # Шаг 6: Аудит
    await log_audit(
        agent_id=agent_id,
        action="event_ingest",
        target_table="l1_raw_events",
        target_id=event_id,
        details={"domain": payload.domain, "warnings": sanitized.warnings},
        ip_address=request.client.host if request.client else "",
        success=True,
    )

    return EventResponse(id=event_id, status="accepted", timestamp=now)
