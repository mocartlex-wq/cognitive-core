"""Per-owner quota enforcement (PR #23 multi-tenant).

Используется в write-endpoints (events, media uploads, agents/create) для
проверки что текущий owner не превысил free/pro/enterprise tier лимиты.

Hot path — lookup из owner_quotas table (PK по owner_user_id, мгновенно).
Кеш в request.state на время одного request'а.

Usage в endpoint:
    from app.services.quota_enforcer import enforce_event_quota, enforce_agent_quota

    @router.post("/events")
    async def create_event(...):
        await enforce_event_quota(request)  # raises HTTPException 429 если over
        ...
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.db.postgres import get_pool
from app.security.owner import resolve_owner_user_id


async def _get_quota(request: Request, owner_user_id: str) -> dict | None:
    """Возвращает owner_quotas row как dict, или None если нет (legacy)."""
    cached = getattr(request.state, "_owner_quota", None)
    if cached is not None and cached.get("_owner") == owner_user_id:
        return cached
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT max_events_per_day, max_storage_mb, max_agents,
                   max_recall_per_min, events_today, storage_mb_now,
                   agents_count, tier, suspended
              FROM owner_quotas
             WHERE owner_user_id = $1::uuid
            """,
            owner_user_id,
        )
    if not row:
        return None
    data = dict(row)
    data["_owner"] = owner_user_id
    request.state._owner_quota = data
    return data


async def _check_suspended(quota: dict | None) -> None:
    if quota and quota.get("suspended"):
        raise HTTPException(
            status_code=403,
            detail="owner account suspended — обратитесь к админу",
        )


async def enforce_event_quota(request: Request) -> None:
    """Проверка перед INSERT в l1_raw_events."""
    owner = await resolve_owner_user_id(request)
    if owner is None:
        return  # admin/legacy — bypass
    quota = await _get_quota(request, owner)
    if quota is None:
        return  # row не создан (странно, но не блокируем)
    await _check_suspended(quota)
    if quota["events_today"] >= quota["max_events_per_day"]:
        raise HTTPException(
            status_code=429,
            detail=(
                f"events quota exceeded: {quota['events_today']}/{quota['max_events_per_day']} "
                f"today (tier={quota['tier']}). Reset в 00:00 UTC. "
                f"Upgrade tier для увеличения лимита."
            ),
            headers={"Retry-After": "3600"},
        )


async def enforce_agent_quota(request: Request) -> None:
    """Проверка перед созданием нового agent (user.py:_create_agent_core)."""
    owner = await resolve_owner_user_id(request)
    if owner is None:
        return
    quota = await _get_quota(request, owner)
    if quota is None:
        return
    await _check_suspended(quota)
    if quota["agents_count"] >= quota["max_agents"]:
        raise HTTPException(
            status_code=429,
            detail=(
                f"agents quota exceeded: {quota['agents_count']}/{quota['max_agents']} "
                f"(tier={quota['tier']}). Удалите ненужных или upgrade tier."
            ),
        )


async def enforce_storage_quota(request: Request, extra_mb: float = 0.0) -> None:
    """Проверка перед media upload — учитывает extra_mb (planned add)."""
    owner = await resolve_owner_user_id(request)
    if owner is None:
        return
    quota = await _get_quota(request, owner)
    if quota is None:
        return
    await _check_suspended(quota)
    projected = quota["storage_mb_now"] + extra_mb
    if projected > quota["max_storage_mb"]:
        raise HTTPException(
            status_code=429,
            detail=(
                f"storage quota exceeded: {quota['storage_mb_now']:.1f}+{extra_mb:.1f}MB "
                f"> {quota['max_storage_mb']}MB (tier={quota['tier']}). "
                f"Удалите старые медиа или upgrade tier."
            ),
        )


async def get_owner_usage_summary(request: Request) -> dict | None:
    """UI helper — возвращает usage для profile.html «Использование» card."""
    owner = await resolve_owner_user_id(request)
    if owner is None:
        return None
    quota = await _get_quota(request, owner)
    if quota is None:
        return None
    return {
        "tier": quota["tier"],
        "events": {
            "used": quota["events_today"],
            "max": quota["max_events_per_day"],
            "pct": round(100 * quota["events_today"] / max(1, quota["max_events_per_day"]), 1),
        },
        "storage_mb": {
            "used": round(quota["storage_mb_now"], 2),
            "max": quota["max_storage_mb"],
            "pct": round(100 * quota["storage_mb_now"] / max(1, quota["max_storage_mb"]), 1),
        },
        "agents": {
            "used": quota["agents_count"],
            "max": quota["max_agents"],
            "pct": round(100 * quota["agents_count"] / max(1, quota["max_agents"]), 1),
        },
        "suspended": quota.get("suspended", False),
    }
