import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.db.postgres import get_pool
from app.models.event import EventResponse, RawEventInput
from app.security.audit import log_audit
from app.security.auth import check_rate_limit, verify_api_key
from app.security.owner import resolve_owner_user_id
from app.security.sanitizer import sanitize_payload
from app.services.ingestor import save_raw_event
from app.services.quota_enforcer import enforce_event_quota

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventResponse)
async def ingest_event(payload: RawEventInput, request: Request):
    """7-шаговый пайплайн приёма события: auth → rate limit → quota → schema
    → sanitize → save → audit.

    PR #23: добавлена per-owner квота. Free tier — 10k events/day.

    Дополнительно: после save в L1 — пишем в replication_outbox для NATS-based
    server→local replication (DS-architecture, Hybrid pattern). Если outbox или
    NATS недоступны — основной flow не страдает.
    """
    # Шаг 1: Аутентификация
    agent_id = await verify_api_key(request)

    # Шаг 2: Rate limiting (per-agent)
    await check_rate_limit(agent_id)

    # Шаг 2b: Per-owner квота (multi-tenant) — 429 если over
    await enforce_event_quota(request)

    # Шаг 3: Валидация схемы — уже выполнена Pydantic (RawEventInput)

    # Шаг 4: Санитизация payload
    sanitized = sanitize_payload(payload.payload)

    # Шаг 5: Сохранение в L1 — с owner_user_id для tenant-isolation
    now = datetime.now(timezone.utc)
    owner_uid = await resolve_owner_user_id(request)
    event_id = await save_raw_event(
        agent_id, payload.domain, sanitized.payload,
        owner_user_id=owner_uid,
    )

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
