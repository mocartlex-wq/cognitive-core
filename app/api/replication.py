"""Replication observability endpoint — для дашборда и мониторинга.

GET /replication/status — публичный (open) status server-side replication:
  - outbox: всего, processed, pending, failed, lag (oldest unpublished)
  - publisher running?
  - последнее опубликованное событие

Не требует X-API-Key — даёт безопасную картину системы.
"""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from app.db.postgres import get_pool

router = APIRouter(prefix="/replication", tags=["replication"])


@router.get("/status")
async def replication_status() -> dict[str, Any]:
    """Сводка состояния outbox + publisher для мониторинга."""
    pool = await get_pool()
    if pool is None:
        return {"error": "db pool not ready"}
    async with pool.acquire() as conn:
        # Сводка по outbox
        stats = await conn.fetchrow(
            """
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE published_at IS NOT NULL) AS published,
                count(*) FILTER (WHERE published_at IS NULL) AS pending,
                count(*) FILTER (WHERE publish_attempts >= 3 AND published_at IS NULL) AS stuck,
                max(published_at) AS last_published,
                min(created_at) FILTER (WHERE published_at IS NULL) AS oldest_pending,
                max(publish_attempts) AS max_attempts
            FROM replication_outbox
            """
        )
        by_kind = await conn.fetch(
            """
            SELECT kind, count(*) AS total,
                   count(*) FILTER (WHERE published_at IS NOT NULL) AS published
            FROM replication_outbox
            GROUP BY kind
            ORDER BY total DESC
            """
        )
        recent_errors = await conn.fetch(
            """
            SELECT id, kind, publish_attempts, last_error
            FROM replication_outbox
            WHERE last_error IS NOT NULL AND published_at IS NULL
            ORDER BY id DESC LIMIT 5
            """
        )

    now = datetime.now(timezone.utc)
    lag_seconds = None
    if stats["oldest_pending"]:
        lag_seconds = int((now - stats["oldest_pending"]).total_seconds())

    health = "ok"
    if stats["pending"] and stats["pending"] > 100:
        health = "lagging"
    if stats["stuck"] and stats["stuck"] > 0:
        health = "stuck"

    return {
        "health": health,
        "outbox": {
            "total": stats["total"],
            "published": stats["published"],
            "pending": stats["pending"],
            "stuck_high_attempts": stats["stuck"],
            "max_attempts_seen": stats["max_attempts"],
            "lag_seconds": lag_seconds,
            "last_published_at": stats["last_published"].isoformat() if stats["last_published"] else None,
        },
        "by_kind": [
            {
                "kind": r["kind"],
                "total": r["total"],
                "published": r["published"],
                "pending": r["total"] - r["published"],
            }
            for r in by_kind
        ],
        "recent_errors": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "attempts": r["publish_attempts"],
                "last_error": r["last_error"],
            }
            for r in recent_errors
        ],
        "checked_at": now.isoformat(),
    }
