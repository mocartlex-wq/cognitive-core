"""Admin-only audit-log READ endpoints (M4 в v1.0 roadmap).

Read-only observability для compliance (152-ФЗ требует audit trail).
Админ видит per-tenant access events: логины, billing, agent claims, сводку.

  GET /admin/audit/logins?limit=50&days=7  — недавние логины (accounts.last_login_at)
  GET /admin/audit/billing?limit=50        — billing-события (если таблица есть)
  GET /admin/audit/agents?limit=50         — недавние claim/create помощников
  GET /admin/audit/summary                 — счётчики (accounts/agents/billing/rooms)

Все требуют is_admin=TRUE в session (через require_admin → 403 если не админ,
401 если нет сессии).

WIRE NOTE (делает владелец при ship):
    from app.api.admin_audit import router as admin_audit_router
    app.include_router(admin_audit_router)

Defensive-by-design: каждый запрос обёрнут в try/except. Если таблица или
колонка отсутствует (billing_processed_events НЕ создаётся в этом репо; rooms
живёт в отдельном сервисе cognitive-rooms) — endpoint возвращает пустой список
и поле "note" вместо 500. Так observability работает даже на неполной схеме.

SELECT-only: никаких INSERT/UPDATE/DELETE. Чисто чтение для аудита.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from app.db.postgres import get_pool
from app.security.middleware import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


def _mask_email(email: str | None) -> str:
    """Маскирует email для аудита: ``john.doe@example.com`` → ``jo***@example.com``.

    Сохраняет домен (полезно для compliance) + первые 2 символа локальной части.
    """
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    visible = local[:2]
    return f"{visible}***@{domain}"


def _mask_owner(owner_user_id: str | None) -> str | None:
    """Маскирует owner_user_id (UUID) до первых 8 символов: ``a1b2c3d4…``."""
    if not owner_user_id:
        return None
    s = str(owner_user_id)
    return f"{s[:8]}…" if len(s) > 8 else s


@router.get("/logins")
async def audit_logins(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    days: int = Query(7, ge=1, le=365),
):
    """Недавние логины аккаунтов (accounts.last_login_at за последние ``days``).

    Возвращает [{ owner_user_id (masked), email (masked), last_login_at,
                  created_at, is_admin }, ...] отсортировано по last_login_at DESC.
    """
    await require_admin(request)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id::text AS owner_user_id,
                       email,
                       last_login_at::text AS last_login_at,
                       created_at::text AS created_at,
                       is_admin
                  FROM accounts
                 WHERE deleted_at IS NULL
                   AND last_login_at IS NOT NULL
                   AND last_login_at > NOW() - ($1::int * INTERVAL '1 day')
              ORDER BY last_login_at DESC
                 LIMIT $2
                """,
                days, limit,
            )
    except Exception as exc:  # noqa: BLE001 — graceful если таблицы/колонки нет
        logger.warning("audit_logins query failed: %s", exc)
        return {"count": 0, "items": [], "note": f"accounts unavailable: {exc}"}

    items = [
        {
            "owner_user_id": _mask_owner(r["owner_user_id"]),
            "email": _mask_email(r["email"]),
            "last_login_at": r["last_login_at"],
            "created_at": r["created_at"],
            "is_admin": r["is_admin"],
        }
        for r in rows
    ]
    return {"count": len(items), "days": days, "items": items}


@router.get("/billing")
async def audit_billing(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
):
    """Обработанные billing-события (idempotency-журнал webhook'ов провайдера).

    Таблица billing_processed_events может отсутствовать (биллинг ещё не
    развёрнут в этой инсталляции) — тогда возвращаем пустой список + note.
    """
    await require_admin(request)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id::text AS event_id,
                       provider,
                       event_type,
                       processed_at::text AS processed_at
                  FROM billing_processed_events
              ORDER BY processed_at DESC
                 LIMIT $1
                """,
                limit,
            )
    except Exception as exc:  # noqa: BLE001 — таблица может не существовать
        logger.info("audit_billing: billing_processed_events unavailable: %s", exc)
        return {
            "count": 0,
            "items": [],
            "note": "billing_processed_events table not present in this deployment",
        }

    return {"count": len(rows), "items": [dict(r) for r in rows]}


@router.get("/agents")
async def audit_agents(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
):
    """Недавние claim/create помощников (agent_states).

    status выводится из last_heartbeat_at: online (<5 мин) / idle (<1 ч) / stale.
    owner_user_id маскируется. Сортировка по updated_at DESC (самые свежие сверху).
    """
    await require_admin(request)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id,
                       owner_user_id::text AS owner_user_id,
                       project,
                       created_at::text AS created_at,
                       updated_at::text AS updated_at,
                       CASE
                         WHEN last_heartbeat_at > NOW() - INTERVAL '5 minutes' THEN 'online'
                         WHEN last_heartbeat_at > NOW() - INTERVAL '1 hour'    THEN 'idle'
                         ELSE 'stale'
                       END AS status
                  FROM agent_states
              ORDER BY updated_at DESC
                 LIMIT $1
                """,
                limit,
            )
    except Exception as exc:  # noqa: BLE001 — graceful если схема неполная
        logger.warning("audit_agents query failed: %s", exc)
        return {"count": 0, "items": [], "note": f"agent_states unavailable: {exc}"}

    items = [
        {
            "agent_id": r["agent_id"],
            "owner_user_id": _mask_owner(r["owner_user_id"]),
            "project": r["project"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
    return {"count": len(items), "items": items}


async def _safe_count(conn, sql: str, *args) -> int | None:
    """Выполнить COUNT, вернуть None если таблица/колонка отсутствует."""
    try:
        return await conn.fetchval(sql, *args)
    except Exception as exc:  # noqa: BLE001
        logger.info("audit summary count skipped (%s): %s", sql.split()[3:5], exc)
        return None


@router.get("/summary")
async def audit_summary(request: Request):
    """Сводные счётчики для admin-дашборда наблюдаемости.

    Каждый счётчик независимо защищён: если таблица отсутствует — в ответе
    придёт null (а не 500), остальные посчитаются нормально.
    """
    await require_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_accounts = await _safe_count(
            conn, "SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL"
        )
        active_agents = await _safe_count(
            conn,
            "SELECT COUNT(*) FROM agent_states "
            "WHERE last_heartbeat_at > NOW() - INTERVAL '1 hour'",
        )
        billing_events_30d = await _safe_count(
            conn,
            "SELECT COUNT(*) FROM billing_processed_events "
            "WHERE processed_at > NOW() - INTERVAL '30 days'",
        )
        rooms_count = await _safe_count(conn, "SELECT COUNT(*) FROM rooms")

    return {
        "total_accounts": total_accounts,
        "active_agents": active_agents,
        "billing_events_30d": billing_events_30d,
        "rooms_count": rooms_count,
        "notes": {
            "billing": None if billing_events_30d is not None
            else "billing_processed_events not present",
            "rooms": None if rooms_count is not None
            else "rooms table lives in cognitive-rooms service, may be absent",
        },
    }
