"""Admin-only endpoints для управления tenants (Phase 5B).

  GET    /admin/tenants                — список всех accounts + their owner_quotas + usage
  PATCH  /admin/tenants/{owner_id}/tier — изменить tier (free/pro/enterprise/admin)
  POST   /admin/tenants/{owner_id}/suspend   — отключить аккаунт
  POST   /admin/tenants/{owner_id}/unsuspend — обратно

Все требуют is_admin=TRUE в session (через require_admin).
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/tenants")
async def list_tenants(request: Request):
    """Полный список аккаунтов + их usage + tier. Для admin-tenants.html.

    Возвращает [{ owner_id, email, display_name, created_at, tier,
                  suspended, events_today, max_events_per_day, storage_mb_now,
                  max_storage_mb, agents_count, max_agents }, ...]
    """
    await require_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id::text AS owner_id,
                   a.email,
                   a.display_name,
                   a.created_at::text AS created_at,
                   COALESCE(q.tier, 'free') AS tier,
                   COALESCE(q.suspended, FALSE) AS suspended,
                   COALESCE(q.events_today, 0) AS events_today,
                   COALESCE(q.max_events_per_day, 10000) AS max_events_per_day,
                   COALESCE(q.storage_mb_now, 0)::float AS storage_mb_now,
                   COALESCE(q.max_storage_mb, 1024) AS max_storage_mb,
                   COALESCE(q.agents_count, 0) AS agents_count,
                   COALESCE(q.max_agents, 10) AS max_agents,
                   q.note
              FROM accounts a
         LEFT JOIN owner_quotas q ON q.owner_user_id = a.id
             WHERE a.deleted_at IS NULL
          ORDER BY a.created_at DESC
            """
        )
    return {
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }


class TierChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Literal["free", "pro", "enterprise", "admin"]
    note: str | None = Field(None, max_length=500)


# Tier defaults — синхронизировано с описанием на /ui/pricing.
TIER_LIMITS = {
    "free":       {"max_events_per_day": 10000,    "max_storage_mb": 1024,    "max_agents": 10,    "max_recall_per_min": 30},
    "pro":        {"max_events_per_day": 100000,   "max_storage_mb": 10240,   "max_agents": 50,    "max_recall_per_min": 150},
    "enterprise": {"max_events_per_day": 10000000, "max_storage_mb": 1048576, "max_agents": 10000, "max_recall_per_min": 1000},
    "admin":      {"max_events_per_day": 100000000,"max_storage_mb": 10485760,"max_agents": 100000,"max_recall_per_min": 10000},
}


@router.patch("/tenants/{owner_id}/tier")
async def change_tenant_tier(owner_id: str, body: TierChangeBody, request: Request):
    """Изменить tier владельца + автоматически обновить limits.

    Защита: admin_user НЕ может downgrade себя до free (case lockout).
    """
    admin = await require_admin(request)
    if str(admin.id) == owner_id and body.tier == "free":
        raise HTTPException(status_code=400, detail="нельзя downgrade самого себя до free")

    limits = TIER_LIMITS[body.tier]
    pool = await get_pool()
    async with pool.acquire() as conn:
        # UPSERT для случая когда owner_quotas строка ещё не создана
        await conn.execute(
            """
            INSERT INTO owner_quotas (owner_user_id, tier, note,
                                       max_events_per_day, max_storage_mb,
                                       max_agents, max_recall_per_min, updated_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (owner_user_id) DO UPDATE
                SET tier = $2, note = $3,
                    max_events_per_day = $4,
                    max_storage_mb = $5,
                    max_agents = $6,
                    max_recall_per_min = $7,
                    updated_at = NOW()
            """,
            owner_id, body.tier, body.note,
            limits["max_events_per_day"], limits["max_storage_mb"],
            limits["max_agents"], limits["max_recall_per_min"],
        )
    logger.info("admin %s changed tier of %s → %s", admin.id, owner_id, body.tier)
    return {"status": "ok", "owner_id": owner_id, "tier": body.tier, "limits": limits}


@router.post("/tenants/{owner_id}/suspend")
async def suspend_tenant(owner_id: str, request: Request):
    """Suspend owner — все его API-вызовы будут возвращать 403.

    Защита: admin НЕ может suspend себя.
    """
    admin = await require_admin(request)
    if str(admin.id) == owner_id:
        raise HTTPException(status_code=400, detail="нельзя suspend самого себя")
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE owner_quotas SET suspended = TRUE, updated_at = NOW() "
            "WHERE owner_user_id = $1::uuid",
            owner_id,
        )
    logger.info("admin %s suspended %s", admin.id, owner_id)
    return {"status": "suspended", "owner_id": owner_id, "rows": result}


@router.post("/tenants/{owner_id}/unsuspend")
async def unsuspend_tenant(owner_id: str, request: Request):
    """Снять suspend с owner."""
    admin = await require_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE owner_quotas SET suspended = FALSE, updated_at = NOW() "
            "WHERE owner_user_id = $1::uuid",
            owner_id,
        )
    logger.info("admin %s unsuspended %s", admin.id, owner_id)
    return {"status": "active", "owner_id": owner_id, "rows": result}
