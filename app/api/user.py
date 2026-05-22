"""Endpoints для работы с собственным аккаунтом и его ресурсами.

  GET    /user/me               — текущий профиль (id, email, display_name, is_admin)
  PATCH  /user/me               — обновить display_name / avatar_url
  GET    /user/rooms            — мои комнаты (где я владелец или участник)
  GET    /user/agents           — мои помощники
  POST   /user/agents/create    — создать нового помощника (привязать к этому user)
  DELETE /user/account          — soft-delete аккаунта (30-дневная отсрочка)

Все требуют валидную сессию через require_user.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/user", tags=["user"])


# ─────────────────────────────────────────────────────────────────────────
# /user/me
# ─────────────────────────────────────────────────────────────────────────
@router.get("/me")
async def get_me(request: Request):
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT user_id::text AS user_id, email, display_name, avatar_url,
                   is_admin, email_verified, created_at, last_login_at
              FROM accounts WHERE user_id = $1::uuid
            """,
            user.user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    data = dict(row)
    for k in ("created_at", "last_login_at"):
        v = data.get(k)
        if isinstance(v, datetime):
            data[k] = v.isoformat()
    return data


class UpdateProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, max_length=80)
    avatar_url: str | None = Field(None, max_length=500)


@router.patch("/me")
async def patch_me(body: UpdateProfileBody, request: Request):
    user = await require_user(request)
    sets: list[str] = []
    args: list[Any] = []
    if body.display_name is not None:
        args.append(body.display_name.strip() or None)
        sets.append(f"display_name = ${len(args)}")
    if body.avatar_url is not None:
        url = body.avatar_url.strip() or None
        if url and not (url.startswith("https://") or url.startswith("http://")):
            raise HTTPException(status_code=400, detail="avatar_url должен начинаться с https://")
        args.append(url)
        sets.append(f"avatar_url = ${len(args)}")
    if not sets:
        return {"ok": True, "updated": 0}

    args.append(user.user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE user_id = ${len(args)}::uuid",
            *args,
        )
    return {"ok": True, "updated": len(sets)}


# ─────────────────────────────────────────────────────────────────────────
# /user/rooms — мои комнаты
# ─────────────────────────────────────────────────────────────────────────
@router.get("/rooms")
async def my_rooms(request: Request):
    """Список комнат пользователя — где он владелец ИЛИ участник.

    Возвращает пустой список если таблицы rooms ещё нет (cognitive-rooms.py
    не задеплоен — в этом случае возвращаем 200 + items=[]).
    """
    user = await require_user(request)
    pool = await get_pool()
    items: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT DISTINCT r.id::text AS id, r.name, r.created_at,
                       (r.owner_user_id = $1::uuid) AS is_owner,
                       COALESCE(r.is_public, TRUE) AS is_public
                  FROM rooms r
                  LEFT JOIN room_participants p
                         ON p.room_id = r.id
                 WHERE r.owner_user_id = $1::uuid
                    OR p.user_id = $1::uuid
                    OR p.agent_id IN (
                        SELECT agent_id FROM agent_states WHERE owner_user_id = $1::uuid
                    )
                 ORDER BY r.created_at DESC
                 LIMIT 200
                """,
                user.user_id,
            )
            for r in rows:
                d = dict(r)
                if isinstance(d.get("created_at"), datetime):
                    d["created_at"] = d["created_at"].isoformat()
                items.append(d)
        except Exception as e:
            # rooms таблица может ещё не существовать в этой БД
            logger.info("my_rooms_skip user_id=%s err=%s", user.user_id, e)

    return {"count": len(items), "items": items}


# ─────────────────────────────────────────────────────────────────────────
# /user/agents — мои помощники
# ─────────────────────────────────────────────────────────────────────────
@router.get("/agents")
async def my_agents(request: Request):
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id, current_task, project, machine, capabilities,
                   last_heartbeat_at, total_events, total_checkpoints, updated_at,
                   last_mcp_connect_at,
                   -- Presence: MCP-online если connect в последние 60 сек
                   (last_mcp_connect_at IS NOT NULL
                    AND last_mcp_connect_at > NOW() - INTERVAL '60 seconds') AS mcp_online
              FROM agent_states
             WHERE owner_user_id = $1::uuid
             ORDER BY mcp_online DESC, last_heartbeat_at DESC NULLS LAST
            """,
            user.user_id,
        )
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("last_heartbeat_at", "updated_at", "last_mcp_connect_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        items.append(d)
    return {"count": len(items), "items": items}


class CreateAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._\-]+$")
    description: str | None = Field(None, max_length=300)
    project: str | None = Field(None, max_length=64)
    machine: str | None = Field(None, max_length=128)
    capabilities: list[str] | None = None


async def _create_agent_core(user, body: CreateAgentBody) -> dict:
    """Реализация регистрации помощника. Reusable из других routers
    (connect.py wizard) — без HTTP-hop, без повторного require_user.

    Параметры:
        user: объект пользователя с .user_id (из require_user / session)
        body: CreateAgentBody уже валидированный

    Returns:
        dict с полями ok, agent_id, api_key, warning.

    Raises:
        HTTPException 409 если agent_id занят.
    """
    import json as _json

    api_key = secrets.token_urlsafe(32)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Проверка уникальности
        existing = await conn.fetchval(
            "SELECT 1 FROM agent_states WHERE agent_id = $1", body.agent_id,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Помощник с id «{body.agent_id}» уже существует. Выберите другой id.",
            )

        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO agent_states
                    (agent_id, owner_user_id, project, machine, capabilities, notes)
                VALUES ($1, $2::uuid, $3, $4, $5::jsonb, $6)
                """,
                body.agent_id,
                user.user_id,
                body.project,
                body.machine,
                _json.dumps(body.capabilities or [], ensure_ascii=False),
                body.description,
            )
            await conn.execute(
                """
                INSERT INTO agent_keys (api_key, agent_id, description)
                VALUES ($1, $2, $3)
                """,
                api_key, body.agent_id, body.description,
            )

    logger.info(
        "agent_created user_id=%s agent_id=%s project=%s",
        user.user_id, body.agent_id, body.project or "?",
    )
    return {
        "ok": True,
        "agent_id": body.agent_id,
        "api_key": api_key,
        "warning": "Сохраните api_key — больше его показать нельзя.",
    }


@router.post("/agents/create")
async def create_agent(body: CreateAgentBody, request: Request):
    """Зарегистрировать нового помощника и выдать ему API key.

    Помощник сразу привязан к текущему пользователю (owner_user_id).
    Возвращает api_key один раз — после этого его нельзя увидеть снова.

    Тонкий wrapper над _create_agent_core() для reuse из connect.py wizard.
    """
    user = await require_user(request)
    return await _create_agent_core(user, body)


# ─────────────────────────────────────────────────────────────────────────
# /user/account — soft delete
# ─────────────────────────────────────────────────────────────────────────
@router.delete("/account")
async def delete_account(request: Request):
    """Soft delete аккаунта с 30-дневной отсрочкой.

    • accounts.deleted_at = NOW() — аккаунт «помечен на удаление»
    • Все сессии отозваны
    • Через 30 дней worker.py физически удалит row (CASCADE по FK)

    Безопасность: НЕ удаляем сразу, чтобы дать восстановить если случайно.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE accounts SET deleted_at = NOW() WHERE user_id = $1::uuid",
                user.user_id,
            )
            await conn.execute(
                "UPDATE sessions SET revoked = TRUE, revoked_at = NOW() "
                "WHERE user_id = $1::uuid AND NOT revoked",
                user.user_id,
            )
    logger.warning("account_soft_deleted user_id=%s email=%s", user.user_id, user.email)
    return {
        "ok": True,
        "deleted_at": datetime.utcnow().isoformat(),
        "will_be_removed_in_days": 30,
        "message": "Аккаунт помечен на удаление. Чтобы восстановить — напишите на support до окончания 30 дней.",
    }
