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


class ErrorAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(..., max_length=20)
    target: str = Field("", max_length=200)
    text: str | None = Field(None, max_length=80)
    ts: float | None = None


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
    error_kind: str = Field("js", max_length=32)   # "js" / "promise" / "fetch" / "console" / "resource"
    client_ts: float | None = None                  # epoch ms на стороне браузера
    last_actions: list[ErrorAction] | None = Field(None, max_length=10)
    dom_snapshot: str | None = Field(None, max_length=10000)  # sanitized HTML, max 10KB


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
async def list_errors(request: Request, limit: int = 50, since_hours: int = 168):
    """Последние ошибки — для админ-дашборда. Требует is_admin.

    Параметры:
      limit       — макс. количество (1..500), default 50
      since_hours — за сколько последних часов брать (1..720), default 168=7d
    """
    user = await require_admin(request)
    _ = user

    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit должен быть 1..500")
    if since_hours < 1 or since_hours > 720:
        raise HTTPException(status_code=400, detail="since_hours должен быть 1..720")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, timestamp, raw_payload
              FROM l1_raw_events
             WHERE domain = 'frontend_errors'
               AND timestamp > NOW() - $2 * INTERVAL '1 hour'
             ORDER BY timestamp DESC
             LIMIT $1
            """,
            limit, since_hours,
        )

    items = []
    for r in rows:
        d = dict(r)
        if d.get("timestamp"):
            d["timestamp"] = d["timestamp"].isoformat()
        items.append(d)
    return {"count": len(items), "items": items}


# ─────────────────────────────────────────────────────────────────────────
# Digest — статистика за последние N часов (для cron-задачи email-нотификации)
# ─────────────────────────────────────────────────────────────────────────
async def collect_digest(hours: int = 6) -> dict[str, Any]:
    """Собрать сводку ошибок за последние N часов.

    Возвращает структуру:
      {
        "since_hours": 6,
        "total": 42,
        "unique_messages": 12,
        "by_kind": {"js": 30, "fetch": 8, "promise": 4},
        "top_errors": [{"message": "...", "count": 15, "first_url": "..."}],
      }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM l1_raw_events
             WHERE domain = 'frontend_errors'
               AND timestamp > NOW() - $1 * INTERVAL '1 hour'
            """,
            hours,
        )
        if not total:
            return {"since_hours": hours, "total": 0, "top_errors": []}

        kind_rows = await conn.fetch(
            """
            SELECT raw_payload->>'error_kind' AS kind, COUNT(*) AS c
              FROM l1_raw_events
             WHERE domain = 'frontend_errors'
               AND timestamp > NOW() - $1 * INTERVAL '1 hour'
             GROUP BY raw_payload->>'error_kind'
             ORDER BY c DESC
            """,
            hours,
        )
        top_rows = await conn.fetch(
            """
            SELECT raw_payload->>'message' AS msg,
                   MIN(raw_payload->>'url') AS first_url,
                   MIN(raw_payload->>'error_kind') AS kind,
                   COUNT(*) AS c
              FROM l1_raw_events
             WHERE domain = 'frontend_errors'
               AND timestamp > NOW() - $1 * INTERVAL '1 hour'
             GROUP BY raw_payload->>'message'
             ORDER BY c DESC
             LIMIT 10
            """,
            hours,
        )
        unique = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT raw_payload->>'message')
              FROM l1_raw_events
             WHERE domain = 'frontend_errors'
               AND timestamp > NOW() - $1 * INTERVAL '1 hour'
            """,
            hours,
        )

    return {
        "since_hours": hours,
        "total": total or 0,
        "unique_messages": unique or 0,
        "by_kind": {r["kind"] or "?": r["c"] for r in kind_rows},
        "top_errors": [
            {
                "message": (r["msg"] or "")[:200],
                "first_url": r["first_url"] or "",
                "kind": r["kind"] or "?",
                "count": r["c"],
            }
            for r in top_rows
        ],
    }


@router.get("/digest")
async def get_digest(request: Request, hours: int = 6):
    """Сводка ошибок за N часов. Требует admin."""
    user = await require_admin(request)
    _ = user
    if hours < 1 or hours > 168:
        raise HTTPException(status_code=400, detail="hours должен быть 1..168")
    return await collect_digest(hours)


# ─────────────────────────────────────────────────────────────────────────
# Email-нотификация владельцу о новых ошибках
# ─────────────────────────────────────────────────────────────────────────
async def send_digest_email_if_needed(hours: int = 6) -> dict[str, Any]:
    """Если за hours были ошибки — отправить дайджест на owner_bootstrap_email.

    Вызывается из cron-задачи (worker.py). Если ошибок нет — silent skip.
    """
    from app.config import settings
    from app.services.email_client import send_notification

    target_email = (settings.owner_bootstrap_email or "").strip()
    if not target_email:
        return {"sent": False, "reason": "owner_bootstrap_email не задан"}

    digest = await collect_digest(hours)
    if digest["total"] == 0:
        return {"sent": False, "reason": "нет ошибок за период"}

    # Текст письма
    lines = [
        f"За последние {hours} часов на Cognitive Core зафиксировано {digest['total']} фронтенд-ошибок ",
        f"({digest['unique_messages']} уникальных).",
        "",
        "По типам:",
    ]
    for kind, count in digest.get("by_kind", {}).items():
        lines.append(f"  • {kind}: {count}")
    lines.append("")
    lines.append("Топ-10 повторяющихся:")
    for i, e in enumerate(digest.get("top_errors", []), 1):
        lines.append(f"  {i}. [{e['kind']}] ×{e['count']} — {e['message'][:120]}")
        if e.get("first_url"):
            lines.append(f"     {e['first_url'][:120]}")

    base_url = (settings.app_url or "").rstrip("/")
    body_text = "\n".join(lines)

    result = await send_notification(
        email=target_email,
        title=f"Cognitive Core: {digest['total']} фронтенд-ошибок за {hours}ч",
        body_text=body_text,
        action_url=f"{base_url}/ui/admin/errors" if base_url else None,
        action_label="Открыть админ-панель",
    )
    logger.info(
        "frontend_errors_digest_sent total=%d unique=%d hours=%d success=%s",
        digest["total"], digest["unique_messages"], hours, result.success,
    )
    return {
        "sent": result.success,
        "total": digest["total"],
        "unique": digest["unique_messages"],
        "message_id": result.message_id,
    }
