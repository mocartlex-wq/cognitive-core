"""Tenant-isolation helper — резолвит owner_user_id из request.

Применяется в memory-endpoints для WHERE owner_user_id = $X фильтрации.

Источники (по приоритету):
  1. X-Owner-User-Id header — internal trusted call (set by _call_self
     after first resolve, avoids repeat DB lookup в цепочке tools).
  2. session cookie (для UI-endpoints) — через app.security.session.
  3. X-API-Key header → resolve через agent_keys.owner_user_id
     (для external MCP/CLI/Custom-GPT клиентов).
  4. None — legacy env-key или нет креденшелов. Для memory-endpoints
     это означает «admin-mode», фильтр НЕ применяется (видят всё).
     Для production multi-tenant production — должно быть запрещено
     отдельным middleware quota_enforcer.
"""
from __future__ import annotations

from fastapi import Request


async def resolve_owner_user_id(request: Request) -> str | None:
    """Резолвит owner_user_id (str UUID) из любого источника request.

    Кеширует в request.state._resolved_owner_user_id чтобы избежать
    повторных DB-запросов на цепочке вызовов одного request'а.

    Returns:
        str UUID или None (legacy env-key, admin-режим — без owner-фильтра).
    """
    cached = getattr(request.state, "_resolved_owner_user_id", "SENTINEL")
    if cached != "SENTINEL":
        return cached  # cached value (может быть None)

    owner: str | None = None

    # 1. Internal call с X-Owner-User-Id header — trusted
    hdr_owner = request.headers.get("x-owner-user-id")
    if hdr_owner:
        owner = hdr_owner
        request.state._resolved_owner_user_id = owner
        return owner

    # 2. Session cookie (UI flow)
    try:
        from app.security.session import get_current_user
        user = await get_current_user(request)
        if user and getattr(user, "id", None):
            owner = str(user.id)
            request.state._resolved_owner_user_id = owner
            return owner
    except Exception:
        pass

    # 3. X-API-Key → agent_keys.owner_user_id
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        try:
            from app.db.postgres import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row_owner = await conn.fetchval(
                    "SELECT owner_user_id::text FROM agent_keys "
                    "WHERE api_key = $1 AND revoked_at IS NULL LIMIT 1",
                    api_key,
                )
            if row_owner:
                owner = row_owner
                request.state._resolved_owner_user_id = owner
                return owner
        except Exception:
            pass

    # 4. Legacy env-key — owner=None (admin mode)
    request.state._resolved_owner_user_id = None
    return None


def owner_filter_sql(owner_user_id: str | None, *, param_index: int) -> tuple[str, list]:
    """Возвращает SQL-сниппет и параметры для добавления в WHERE.

    Если owner_user_id передан — возвращает (' AND owner_user_id = $N::uuid', [owner]).
    Если None (admin) — возвращает ('', []).

    Используется чтобы консистентно добавлять owner-фильтр в любую query:

        sql = "SELECT ... FROM l3_master_knowledge WHERE domain = $1"
        params = [domain]
        clause, extra = owner_filter_sql(owner, param_index=len(params)+1)
        sql += clause
        params.extend(extra)
    """
    if owner_user_id is None:
        return "", []
    return f" AND owner_user_id = ${param_index}::uuid", [owner_user_id]
