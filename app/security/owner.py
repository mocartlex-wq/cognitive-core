"""Tenant-isolation helper — резолвит owner_user_id из request.

Применяется в memory-endpoints для WHERE owner_user_id = $X фильтрации.

Источники (по приоритету):
  1. X-Owner-User-Id header — internal trusted call (set by _call_self
     after first resolve, avoids repeat DB lookup в цепочке tools).
  2. session cookie (для UI-endpoints) — через app.security.session.
  3. X-API-Key header → agent_keys.owner_user_id
     (для external MCP/CLI/Custom-GPT клиентов).
     ⚠️ Ключ ЕСТЬ в БД, но owner_user_id IS NULL → 403. До 2026-09-05 такой
     ключ трактовался как «admin без фильтра»: `POST /agents/register` без
     авторизации выдавал ключ с NULL-владельцем → чтение памяти ВСЕХ
     владельцев + деплой на прод (подтверждено живьём). Легитимные пути
     (claim-wizard, /user/agents/create) владельца ставят всегда.
  4. Env-ключ из AGENT_API_KEYS (config-provisioned секрет, в БД его нет):
     owner = settings.cogcore_admin_owner_user_id, если задан; иначе None —
     admin-режим без фильтра (единственный оставшийся смысл None).
"""
from __future__ import annotations

from fastapi import HTTPException, Request

ORPHAN_KEY_DETAIL = (
    "Ключ без владельца: он не привязан ни к одному аккаунту и не даёт доступа. "
    "Подключите помощника через /ui/profile → «Передать помощнику» (claim-token) "
    "или создайте ключ под своим аккаунтом."
)


async def resolve_owner_user_id(request: Request) -> str | None:
    """Резолвит owner_user_id (str UUID) из любого источника request.

    Кеширует в request.state._resolved_owner_user_id чтобы избежать
    повторных DB-запросов на цепочке вызовов одного request'а.

    Returns:
        str UUID или None (env-ключ без назначенного владельца — admin-режим).

    Raises:
        HTTPException 403 — ключ найден в agent_keys, но owner_user_id IS NULL.
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

    # 2. Session cookie (UI flow). До 2026-09-05 импортировался несуществующий
    #    `session.get_current_user`, AttributeError глотался — путь через cookie
    #    не работал НИКОГДА, владелец из UI резолвился только по X-API-Key.
    try:
        from app.security.middleware import optional_user
        user = await optional_user(request)
        if user and getattr(user, "user_id", None):
            owner = str(user.user_id)
            request.state._resolved_owner_user_id = owner
            return owner
    except Exception:
        pass

    # 3. X-API-Key → agent_keys.owner_user_id
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        row = None
        try:
            from app.db.postgres import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT owner_user_id::text AS owner_user_id FROM agent_keys "
                    "WHERE api_key = $1 AND revoked_at IS NULL LIMIT 1",
                    api_key,
                )
        except Exception:
            row = None
        if row is not None:
            row_owner = row["owner_user_id"]
            if not row_owner:
                # Ключ-сирота: в БД есть, владельца нет. Отказ, не admin.
                raise HTTPException(status_code=403, detail=ORPHAN_KEY_DETAIL)
            owner = str(row_owner)
            request.state._resolved_owner_user_id = owner
            return owner

    # 4. Env-ключ (AGENT_API_KEYS) — владелец из настроек или admin-режим.
    owner = env_agent_owner()
    request.state._resolved_owner_user_id = owner
    return owner


def env_agent_owner() -> str | None:
    """Владелец env-агентов (AGENT_API_KEYS): настройка или None (admin)."""
    try:
        from app.config import settings
        value = (settings.cogcore_admin_owner_user_id or "").strip()
    except Exception:
        value = ""
    return value or None


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
