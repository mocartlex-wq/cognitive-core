import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.db.postgres import get_pool
from app.models.event import EventResponse, RawEventInput, validate_checkpoint
from app.security.audit import log_audit
from app.security.auth import check_rate_limit, verify_api_key
from app.security.owner import resolve_owner_user_id
from app.security.sanitizer import sanitize_payload
from app.services.ingestor import save_idempotent_event, save_raw_event
from app.services.memory_records import read_record
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
    try:
        sanitized = sanitize_payload(validate_checkpoint(payload.payload))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Шаг 5: Сохранение в L1 — с owner_user_id для tenant-isolation
    now = datetime.now(timezone.utc)
    owner_uid = await resolve_owner_user_id(request)
    created = True
    if payload.idempotency_key or sanitized.payload.get("checkpoint"):
        if not owner_uid:
            raise HTTPException(403, "Idempotent memory requires an owner-scoped key")
        try:
            event_id, now, created = await save_idempotent_event(
                agent_id, payload.domain, sanitized.payload, owner_uid, payload.idempotency_key or str(uuid4()),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    else:
        event_id = await save_raw_event(
            agent_id, payload.domain, sanitized.payload,
            owner_user_id=owner_uid,
        )

    # Шаг 5b: Replication outbox — failure не блокирует основной flow
    try:
        if not created:
            return EventResponse(id=event_id, status="accepted", timestamp=now)
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


@router.get("/{record_id}")
async def get_record(record_id: UUID, request: Request):
    await verify_api_key(request)
    owner = await resolve_owner_user_id(request)
    pool = await get_pool()
    record = await read_record(pool, str(record_id), owner)
    if record is None:
        raise HTTPException(404, "Memory record not found")
    comments = await pool.fetch(
        """SELECT id, text, created_at FROM owner_memory_comments
           WHERE record_id=$1 AND owner_user_id=$2::uuid
           ORDER BY created_at DESC, id DESC LIMIT 11""",
        record_id, owner,
    )
    if comments:
        record["owner_comments"] = [dict(row) for row in comments[:10]]
        record["more_owner_comments"] = len(comments) > 10
    return record
