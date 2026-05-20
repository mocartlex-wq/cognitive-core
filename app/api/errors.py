"""Frontend error reporter — endpoint для сбора JS-ошибок с браузеров пользователей.

POST /api/errors  — принимает payload с ошибкой, валидирует, пишет в L1 events
                    с domain="frontend_errors". Anonymous (без auth).

GET  /api/errors  — список последних ошибок (требует admin). Для дашборда / мне
                    через `cognitive_recall domain:frontend_errors`.

Rate-limit: max 50 ошибок/час с одного IP — защита от спама.

Что собирается:
  - url       — где случилось
  - message   — текст ошибки
  - stack     — stack-trace если есть (обрезается до 2KB)
  - source    — какой файл .js источник
  - line / col— координаты в source
  - viewport  — {width, height} браузера
  - user_agent
  - referrer
  - timestamp (server-side)

НЕ собирается: содержимое DOM (PII risk), скриншот (большой объём, отдельный flow).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import optional_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/errors", tags=["errors"])


# In-memory rate-limit: ip → deque of timestamps за последний час
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMIT_PER_HOUR = 50
RATE_WINDOW_SEC = 3600


def _check_rate_limit(ip: str) -> bool:
    """True если можно принять, False если rate-limit."""
    now = time.time()
    bucket = _rate_buckets[ip]
    cutoff = now - RATE_WINDOW_SEC
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_HOUR:
        return False
    bucket.append(now)
    return True


class ErrorReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., max_length=500)
    message: str = Field(..., max_length=1000)
    stack: str | None = Field(None, max_length=2000)
    source: str | None = Field(None, max_length=300)
    line: int | None = Field(None, ge=0, le=999999)
    col: int | None = Field(None, ge=0, le=999999)
    viewport_w: int | None = Field(None, ge=0, le=100000)
    viewport_h: int | None = Field(None, ge=0, le=100000)
    user_agent: str | None = Field(None, max_length=400)
    referrer: str | None = Field(None, max_length=500)
    error_kind: str = Field("js", max_length=32)   # "js" / "promise" / "fetch" / "console"
    client_ts: float | None = None                  # epoch ms на стороне браузера


@router.post("")
async def post_error(body: ErrorReport, request: Request):
    """Принять ошибку с фронта. Anonymous endpoint (без auth)."""
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")

    if not _check_rate_limit(ip):
        logger.info("frontend_error_rate_limited ip=%s", ip)
        # 200 (а не 429) — чтобы клиент не ретрайл
        return {"ok": True, "throttled": True}

    # Дополним контекст с сервера
    payload: dict[str, Any] = body.model_dump()
    payload["ip"] = ip
    payload["server_ts"] = time.time()

    # Опционально привяжем user_id (если есть сессия)
    try:
        user = await optional_user(request)
        if user:
            payload["user_id"] = user.user_id
            payload["user_email"] = user.email
    except Exception:
        pass

    # Пишем в L1 raw_events. agent_id = "frontend_browser" чтобы фильтровать
    # отдельно от агентских событий.
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO l1_raw_events (source_agent, domain, raw_payload)
                VALUES ($1, $2, $3::jsonb)
                RETURNING id::text AS id
                """,
                "frontend_browser",
                "frontend_errors",
                __import__("json").dumps(payload, ensure_ascii=False),
            )
        logger.info(
            "frontend_error_logged ip=%s url=%s msg=%r kind=%s id=%s",
            ip, body.url, body.message[:80], body.error_kind, row["id"],
        )
        return {"ok": True, "event_id": row["id"]}
    except Exception as e:
        logger.warning("frontend_error_db_fail err=%s", e)
        # Даже при сбое БД — 200, чтобы клиент не повторял
        return {"ok": True, "stored": False, "error": str(e)[:200]}


@router.get("")
async def list_errors(request: Request, limit: int = 50):
    """Последние ошибки — для админ-дашборда. Требует is_admin."""
    user = await require_admin(request)
    _ = user  # отметим использование

    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit должен быть 1..500")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, timestamp, raw_payload
              FROM l1_raw_events
             WHERE domain = 'frontend_errors'
             ORDER BY timestamp DESC
             LIMIT $1
            """,
            limit,
        )

    items = []
    for r in rows:
        d = dict(r)
        if d.get("timestamp"):
            d["timestamp"] = d["timestamp"].isoformat()
        items.append(d)
    return {"count": len(items), "items": items}
