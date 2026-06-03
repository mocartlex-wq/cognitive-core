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

import json
import logging
import re
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
# POST /user/rooms — создать комнату (CRUD из /ui/profile)
# PATCH /user/rooms/{id} — переименовать
# DELETE /user/rooms/{id} — удалить
# Direct DB ops (минуя rooms-service на :9098), потому что rooms таблица
# shared в cognitive_postgres. rooms-service отвечает только за messages
# /post /ask /answer (runtime ops). Для CRUD комнат — backend ходит в БД сам.
# ─────────────────────────────────────────────────────────────────────────
class CreateRoomBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    is_public: bool = Field(True)


class PatchRoomBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    is_public: bool | None = None


@router.post("/rooms")
async def create_my_room(body: CreateRoomBody, request: Request):
    """Создать новую комнату — owner становится her owner_user_id.

    Возвращает {id, api_key, name} — api_key (rk_...) можно использовать
    как X-Room-Key чтобы агенты могли join'ниться. UI показывает его в
    диалоге «Поделиться ссылкой».
    """
    user = await require_user(request)
    api_key = "rk_" + secrets.token_urlsafe(32)
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO rooms (name, description, created_by, api_key,
                                   owner_user_id, is_public, status)
                VALUES ($1, $2, $3, $4, $5::uuid, $6, 'active')
                RETURNING id::text AS id, name, api_key, created_at::text
                """,
                body.name, body.description or "", user.email,
                api_key, str(user.user_id), body.is_public,
            )
        except Exception as e:
            logger.error("create_room failed user=%s err=%s", user.user_id, e)
            raise HTTPException(status_code=500, detail=f"Не удалось создать комнату: {e}")
    logger.info("room_created user=%s room=%s name=%s", user.user_id, row["id"], body.name)
    return {
        "id": row["id"],
        "name": row["name"],
        "api_key": row["api_key"],
        "created_at": row["created_at"],
    }


@router.patch("/rooms/{room_id}")
async def patch_my_room(room_id: str, body: PatchRoomBody, request: Request):
    """Переименовать / отредактировать описание / переключить public-флаг.

    Только owner_user_id комнаты может. Возвращает обновлённую запись.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM rooms WHERE id = $1::uuid",
            room_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Комната не найдена")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваша комната")

        sets, vals = [], []
        if body.name is not None:
            sets.append(f"name = ${len(vals)+1}")
            vals.append(body.name)
        if body.description is not None:
            sets.append(f"description = ${len(vals)+1}")
            vals.append(body.description)
        if body.is_public is not None:
            sets.append(f"is_public = ${len(vals)+1}")
            vals.append(body.is_public)
        if not sets:
            return {"ok": True, "changed": 0}
        vals.append(room_id)
        await conn.execute(
            f"UPDATE rooms SET {', '.join(sets)} WHERE id = ${len(vals)}::uuid",
            *vals,
        )
    logger.info("room_patched user=%s room=%s fields=%s", user.user_id, room_id, len(sets))
    return {"ok": True, "room_id": room_id}


@router.delete("/rooms/{room_id}")
async def delete_my_room(room_id: str, request: Request):
    """Удалить комнату — CASCADE снимает room_participants + room_messages.

    Только owner_user_id может. Возвращает {ok, deleted_messages_count}.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM rooms WHERE id = $1::uuid",
            room_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Комната не найдена")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваша комната")
        # Считаем сообщения для audit (показываем сколько было удалено)
        try:
            msgs = await conn.fetchval(
                "SELECT COUNT(*) FROM room_messages WHERE room_id = $1::uuid",
                room_id,
            ) or 0
        except Exception:
            msgs = 0
        # Удаляем (если CASCADE отсутствует — сначала зависимости)
        try:
            await conn.execute("DELETE FROM room_messages WHERE room_id = $1::uuid", room_id)
            await conn.execute("DELETE FROM room_participants WHERE room_id = $1::uuid", room_id)
            await conn.execute("DELETE FROM rooms WHERE id = $1::uuid", room_id)
        except Exception as e:
            logger.error("delete_room failed user=%s room=%s err=%s", user.user_id, room_id, e)
            raise HTTPException(status_code=500, detail=f"Не удалось удалить: {e}")
    logger.info("room_deleted user=%s room=%s msgs_cleaned=%s", user.user_id, room_id, msgs)
    return {"ok": True, "room_id": room_id, "deleted_messages_count": msgs}


# ─────────────────────────────────────────────────────────────────────────
# GET  /user/rooms/{room_id}/detail — owner-view одной комнаты (M3 UI)
# POST /user/rooms/{room_id}/post   — написать в комнату от своего имени
#
# Owner видит свою комнату через SESSION (require_user) — НЕ через X-Room-Key.
# Owner не внешний агент: room-key нужен только агентам для room_join.
# Direct DB (минуя rooms-service :9098) — rooms/* таблицы shared в
# cognitive_postgres, как и остальной room-CRUD выше.
# Вставлять ПОСЛЕ delete_my_room (перед блоком /user/agents).
# ─────────────────────────────────────────────────────────────────────────
class PostRoomMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=4000)


@router.get("/rooms/{room_id}/detail")
async def get_my_room_detail(room_id: str, request: Request):
    """Полная карточка комнаты для owner-view (/ui/room?id=...).

    Возвращает api_key (для приглашения агентов), список участников и
    последние 50 сообщений треда. Только owner_user_id комнаты видит детали:
    404 если комнаты нет, 403 если она не принадлежит этому пользователю.

    room_participants / room_messages могут быть пустыми (никто ещё не
    join'нулся / не писал) — в этом случае возвращаем [] не падая.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow(
            """
            SELECT id::text AS id, name, api_key,
                   COALESCE(is_public, TRUE) AS is_public,
                   owner_user_id::text AS owner_user_id, created_at
              FROM rooms WHERE id = $1::uuid
            """,
            room_id,
        )
        if not room:
            raise HTTPException(status_code=404, detail="Комната не найдена")
        if str(room["owner_user_id"]) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваша комната")

        participants: list[dict[str, Any]] = []
        try:
            prows = await conn.fetch(
                """
                SELECT p.agent_id, COALESCE(p.role, 'member') AS role,
                       p.joined_at, p.last_seen_at, s.agent_label
                  FROM room_participants p
                  LEFT JOIN agent_states s ON s.agent_id = p.agent_id
                 WHERE p.room_id = $1::uuid
                 ORDER BY p.joined_at ASC NULLS LAST
                 LIMIT 500
                """,
                room_id,
            )
            for p in prows:
                d = dict(p)
                for k in ("joined_at", "last_seen_at"):
                    if isinstance(d.get(k), datetime):
                        d[k] = d[k].isoformat()
                # display_name: красивое имя (agent_label) если задано, иначе agent_id.
                # Чинит рассогласование «профиль: Растр, комната: dsdsd».
                d["display_name"] = d.get("agent_label") or d.get("agent_id")
                participants.append(d)
        except Exception as e:
            logger.info("room_detail participants_skip room=%s err=%s", room_id, e)

        messages: list[dict[str, Any]] = []
        message_count = 0
        try:
            message_count = await conn.fetchval(
                "SELECT COUNT(*) FROM room_messages WHERE room_id = $1::uuid",
                room_id,
            ) or 0
            # Берём последние 50 (DESC), затем разворачиваем в хронологию для UI
            mrows = await conn.fetch(
                """
                SELECT m.id::text AS id, m.from_agent, m.text, m.created_at,
                       s.agent_label
                  FROM room_messages m
                  LEFT JOIN agent_states s ON s.agent_id = m.from_agent
                 WHERE m.room_id = $1::uuid
                 ORDER BY m.created_at DESC
                 LIMIT 50
                """,
                room_id,
            )
            for m in reversed(mrows):
                d = dict(m)
                if isinstance(d.get("created_at"), datetime):
                    d["created_at"] = d["created_at"].isoformat()
                # display_name: agent_label если есть; для owner:email оставляем как есть
                # (фронт сам форматирует «Вы (email)»), для агентов — красивое имя.
                fa = d.get("from_agent") or ""
                d["display_name"] = d.get("agent_label") or fa
                messages.append(d)
        except Exception as e:
            logger.info("room_detail messages_skip room=%s err=%s", room_id, e)

    created = room["created_at"]
    return {
        "id": room["id"],
        "name": room["name"],
        "api_key": room["api_key"],
        "is_public": room["is_public"],
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
        "message_count": message_count,
        "participants": participants,
        "messages": messages,
    }


# ─────────────────────────────────────────────────────────────────────────
# @mention → agent_inbox bridge для OWNER-постов в комнату.
#
# /user/rooms/{id}/post пишет room_messages НАПРЯМУЮ (см. ниже) и потому минует
# мост, который живёт в rooms-сервисе (scripts/cognitive-rooms.py post_message →
# _bridge_to_inbox). Без этого «@Агент …» от владельца НЕ доходит до суточного
# демона (он поллит только agent_inbox) → агент не отвечает. Зеркалим Case 1 из
# _bridge_to_inbox: резолвим @-упоминания room-scoped и дублируем по одному
# событию agent_inbox на получателя с тем же payload (context.via=room), на
# который завязан reverse-мост демона, постящий ответ обратно в комнату.
# Best-effort: ошибка моста НИКОГДА не ломает сам пост в комнату.
# ─────────────────────────────────────────────────────────────────────────
_MENTION_RE = re.compile(r"@([\w\-]+)", re.UNICODE)


async def _resolve_room_mentions(conn, room_id: str, text: str) -> list[str]:
    """Распарсить @-упоминания и резолвить каждое в реальный agent_id.

    Room-scoped first (совпадение по agent_id ИЛИ agent_label участника комнаты,
    регистронезависимо), fallback на глобальный agent_states. Дедуп. Нерезолвимые
    @ — отбрасываются. Зеркало _resolve_mentions_to_agents из cognitive-rooms.py.
    """
    mentions = _MENTION_RE.findall(text or "")
    if not mentions:
        return []
    rows = await conn.fetch(
        """
        SELECT p.agent_id, COALESCE(s.agent_label, '') AS label
          FROM room_participants p
          LEFT JOIN agent_states s ON s.agent_id = p.agent_id
         WHERE p.room_id = $1::uuid
        """,
        room_id,
    )
    by_id: dict[str, str] = {}
    by_label: dict[str, str] = {}
    for r in rows:
        aid = r["agent_id"]
        label = r["label"] or ""
        if aid:
            by_id[aid.lower()] = aid
            if label:
                by_label.setdefault(label.lower(), aid)
    resolved: list[str] = []
    seen: set[str] = set()
    for m in mentions:
        key = m.lower()
        aid = by_id.get(key) or by_label.get(key)
        if not aid:
            g = await conn.fetchrow(
                "SELECT agent_id FROM agent_states "
                "WHERE agent_id = $1 OR lower(agent_label) = lower($1) LIMIT 1",
                m,
            )
            if g and g["agent_id"]:
                aid = g["agent_id"]
        if aid and aid not in seen:
            seen.add(aid)
            resolved.append(aid)
    return resolved


async def _bridge_owner_mentions_to_inbox(
    conn, room_id: str, from_agent: str, text: str
) -> int:
    """Мост owner-постов в L1 agent_inbox. Возвращает число получателей.

    Case 1: @-адресованное → каждому упомянутому агенту. Case 2: безадресное →
    дирижёру комнаты (conductor_agent_id), если назначен — чтобы owner получил
    ответ и без @. Payload идентичен прямому DM ({from,to,text,context}). context.via=room +
    room_id — то, на что завязан reverse-мост демона (ответ постится в комнату).
    Триггер notify_agent_inbox_after_insert на l1_raw_events сам делает NOTIFY →
    демон просыпается. Best-effort: любая ошибка глотается."""
    try:
        recipients = await _resolve_room_mentions(conn, room_id, text)
        recipients = [a for a in recipients if a != from_agent]  # no self-DM
        if recipients:
            # Case 1: @-адресованное → каждому упомянутому агенту.
            for to_agent in recipients:
                payload = {
                    "from": from_agent,
                    "to": to_agent,
                    "text": text,
                    "context": {"via": "room", "room_id": room_id},
                }
                await conn.execute(
                    "INSERT INTO l1_raw_events (source_agent, domain, raw_payload) "
                    "VALUES ($1, $2, $3::jsonb)",
                    from_agent,
                    "agent_inbox",
                    json.dumps(payload, ensure_ascii=False),
                )
            return len(recipients)
        # Case 2: безадресное (нет резолвимого @) → дирижёру комнаты, если назначен,
        # чтобы owner получил ответ и БЕЗ @. Зеркало Case 2 _bridge_to_inbox в
        # cognitive-rooms.py. Автор тут всегда owner:<email> (не агент) → анти-петля
        # по agent_states не нужна. context.unaddressed=true — пометка для дирижёра.
        conductor = await conn.fetchval(
            "SELECT conductor_agent_id FROM rooms WHERE id = $1::uuid", room_id
        )
        if conductor and conductor != from_agent:
            payload = {
                "from": from_agent,
                "to": conductor,
                "text": text,
                "context": {
                    "via": "room",
                    "room_id": room_id,
                    "unaddressed": True,
                },
            }
            await conn.execute(
                "INSERT INTO l1_raw_events (source_agent, domain, raw_payload) "
                "VALUES ($1, $2, $3::jsonb)",
                from_agent,
                "agent_inbox",
                json.dumps(payload, ensure_ascii=False),
            )
            return 1
        return 0
    except Exception as e:  # noqa: BLE001 — best-effort, не должен ломать пост
        logger.warning("owner mention-bridge failed room=%s err=%s", room_id, e)
        return 0


@router.post("/rooms/{room_id}/post")
async def post_my_room_message(room_id: str, body: PostRoomMessageBody, request: Request):
    """Owner пишет в свою комнату от своего имени (from_agent = owner:email).

    INSERT в room_messages триггерит pg_notify('room_event') → агенты в
    комнате получают сообщение через NATS/SSE как обычное. Только
    owner_user_id комнаты может (404 / 403 как в detail).
    """
    user = await require_user(request)
    from_agent = f"owner:{user.email}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM rooms WHERE id = $1::uuid",
            room_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Комната не найдена")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваша комната")
        try:
            message_id = await conn.fetchval(
                """
                INSERT INTO room_messages (room_id, from_agent, text, msg_type)
                VALUES ($1::uuid, $2, $3, 'message')
                RETURNING id::text
                """,
                room_id, from_agent, body.text,
            )
        except Exception as e:
            logger.error("post_room_message failed user=%s room=%s err=%s",
                         user.user_id, room_id, e)
            raise HTTPException(status_code=500, detail=f"Не удалось отправить: {e}")
        # Мост @-упоминаний в agent_inbox (внутри того же conn) — будит демона,
        # чтобы агент ответил В КОМНАТЕ. Best-effort, не ломает пост.
        bridged = await _bridge_owner_mentions_to_inbox(
            conn, room_id, from_agent, body.text
        )
    logger.info("room_message_posted user=%s room=%s msg=%s mention_bridged=%s",
                user.user_id, room_id, message_id, bridged)
    return {"ok": True, "message_id": message_id}


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
            SELECT agent_id, agent_label, current_task, project, machine, capabilities,
                   last_heartbeat_at, total_events, total_checkpoints, updated_at,
                   last_mcp_connect_at, last_mcp_disconnect_at,
                   first_mcp_connect_at,
                   machine_fingerprint, machine_label,
                   status, created_at,
                   -- Presence: MCP-online если connect в последние 60 сек
                   (last_mcp_connect_at IS NOT NULL
                    AND last_mcp_connect_at > NOW() - INTERVAL '60 seconds') AS mcp_online,
                   -- PR #35: pending_claim TTL — секунд до auto-delete
                   GREATEST(0, EXTRACT(EPOCH FROM (created_at + INTERVAL '10 minutes' - NOW()))::int) AS pending_ttl_sec
              FROM agent_states
             WHERE owner_user_id = $1::uuid
             ORDER BY (status = 'pending_claim') DESC,  -- pending наверху
                      machine_fingerprint NULLS LAST,
                      mcp_online DESC,
                      last_heartbeat_at DESC NULLS LAST
            """,
            user.user_id,
        )
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("last_heartbeat_at", "updated_at", "last_mcp_connect_at",
                  "last_mcp_disconnect_at", "first_mcp_connect_at", "created_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        items.append(d)

    # v3: группируем по machine_fingerprint в отдельной структуре для UI
    # (агенты без fp идут в группу «legacy» — те что без installer-а)
    machines: dict[str, dict[str, Any]] = {}
    for item in items:
        fp = item.get("machine_fingerprint") or "_no_machine"
        if fp not in machines:
            machines[fp] = {
                "machine_fingerprint": item.get("machine_fingerprint"),
                "machine_label": item.get("machine_label") or "Без машины",
                "agents": [],
                "any_online": False,
            }
        machines[fp]["agents"].append(item)
        if item.get("mcp_online"):
            machines[fp]["any_online"] = True

    return {
        "count": len(items),
        "items": items,  # backward compat — flat list
        "machines": list(machines.values()),  # v3 — grouped by machine
    }


class CreateAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[\w.\-]+$")
    description: str | None = Field(None, max_length=300)
    project: str | None = Field(None, max_length=64)
    machine: str | None = Field(None, max_length=128)
    capabilities: list[str] | None = None
    machine_fingerprint: str | None = Field(None, min_length=8, max_length=32, pattern=r"^[a-f0-9]+$")
    machine_label: str | None = Field(None, max_length=128)


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
                    (agent_id, owner_user_id, project, machine, capabilities, notes,
                     machine_fingerprint, machine_label)
                VALUES ($1, $2::uuid, $3, $4, $5::jsonb, $6, $7, $8)
                """,
                body.agent_id,
                user.user_id,
                body.project,
                body.machine,
                _json.dumps(body.capabilities or [], ensure_ascii=False),
                body.description,
                body.machine_fingerprint,
                body.machine_label,
            )
            await conn.execute(
                """
                INSERT INTO agent_keys (api_key, agent_id, description, owner_user_id)
                VALUES ($1, $2, $3, $4::uuid)
                """,
                api_key, body.agent_id, body.description, user.user_id,
            )

    # v3+: lifecycle event — owner видит «agent_id зарегистрирован» в списке
    # событий, можно использовать для billing/audit (когда какой agent был создан)
    try:
        import json as _j
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO l1_raw_events (source_agent, domain, raw_payload) VALUES ($1, $2, $3::jsonb)",
                body.agent_id, "agent_lifecycle",
                _j.dumps({
                    "event": "agent_created",
                    "task": "agent зарегистрирован",
                    "project": body.project or "?",
                    "machine": body.machine,
                    "description": body.description,
                }, ensure_ascii=False),
            )
    except Exception:
        pass  # lifecycle event — не критично если упало

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


@router.get("/agents/{agent_id}/events")
async def agent_events(agent_id: str, request: Request, limit: int = 20):
    """Список последних L1-событий конкретного помощника.

    Owner может развернуть card агента → увидеть что он делал. Используется
    для:
    - Audit log (история действий)
    - Billing readiness — счётчик event-ов под подписку/тариф
    - Debug — понять чем агент занят

    Owner-check: проверяем что agent принадлежит user'у.
    """
    user = await require_user(request)
    limit = max(1, min(int(limit), 100))
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")
        rows = await conn.fetch(
            """
            SELECT id::text AS id, domain, raw_payload, timestamp::text AS timestamp
              FROM l1_raw_events
             WHERE source_agent = $1
             ORDER BY timestamp DESC
             LIMIT $2
            """,
            agent_id, limit,
        )
        items = [dict(r) for r in rows]

        # Synthetic «agent_created» event для legacy агентов без lifecycle-записи.
        # Берётся из agent_states.updated_at (когда впервые зарегистрирован).
        # Cмотрим есть ли уже lifecycle event с domain=agent_lifecycle — если нет,
        # добавляем синтетический в конец списка.
        has_lifecycle = any(
            (i.get("domain") == "agent_lifecycle") for i in items
        )
        if not has_lifecycle:
            meta = await conn.fetchrow(
                """
                SELECT updated_at::text AS created, machine_label, project, notes
                  FROM agent_states WHERE agent_id = $1
                """,
                agent_id,
            )
            if meta:
                items.append({
                    "id": "synthetic-lifecycle",
                    "domain": "agent_lifecycle",
                    "raw_payload": {
                        "event": "agent_registered",
                        "task": "agent зарегистрирован",
                        "project": meta["project"] or "—",
                        "machine": meta["machine_label"] or "—",
                        "description": meta["notes"] or None,
                        "synthetic": True,
                    },
                    "timestamp": meta["created"],
                })

    return {
        "agent_id": agent_id,
        "count": len(items),
        "limit": limit,
        "events": items,
    }


@router.get("/usage")
async def get_usage(request: Request):
    """PR #23 multi-tenant: per-owner usage summary для profile.html карточки.

    Возвращает tier + (events/storage/agents): used/max/pct.
    Если owner_quotas строка не существует — fallback к defaults free-tier.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tier, max_events_per_day, max_storage_mb, max_agents,
                   max_recall_per_min, events_today, storage_mb_now,
                   agents_count, suspended
              FROM owner_quotas
             WHERE owner_user_id = $1::uuid
            """,
            user.user_id,
        )
    if not row:
        # Триггер ensure_owner_quota должен был создать строку при регистрации,
        # но если миграция 0007 ещё не применилась — возвращаем сырые defaults.
        return {
            "tier": "free",
            "events": {"used": 0, "max": 10000, "pct": 0},
            "storage_mb": {"used": 0, "max": 1024, "pct": 0},
            "agents": {"used": 0, "max": 10, "pct": 0},
            "suspended": False,
            "note": "owner_quotas row не найдена (migration 0007 не применена?)",
        }
    return {
        "tier": row["tier"],
        "events": {
            "used": row["events_today"],
            "max": row["max_events_per_day"],
            "pct": round(100 * row["events_today"] / max(1, row["max_events_per_day"]), 1),
        },
        "storage_mb": {
            "used": round(float(row["storage_mb_now"]), 2),
            "max": row["max_storage_mb"],
            "pct": round(100 * float(row["storage_mb_now"]) / max(1, row["max_storage_mb"]), 1),
        },
        "agents": {
            "used": row["agents_count"],
            "max": row["max_agents"],
            "pct": round(100 * row["agents_count"] / max(1, row["max_agents"]), 1),
        },
        "suspended": bool(row["suspended"]),
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


class PatchAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_label: str | None = Field(None, max_length=128)
    description: str | None = Field(None, max_length=300)
    project: str | None = Field(None, max_length=64)
    # Per-agent переименование (отображаемое имя помощника, НЕ agent_id).
    agent_label: str | None = Field(None, max_length=128)
    # «Перенос» помощника в другую машину-группу: задаём целевые
    # machine_fingerprint + machine_label именно этому агенту (не broadcast).
    # move_fingerprint="" (пустая строка) → перенести в «Без машины» (NULL).
    move_fingerprint: str | None = Field(None, max_length=32)
    move_label: str | None = Field(None, max_length=128)


@router.patch("/agents/{agent_id}")
async def patch_agent(agent_id: str, body: PatchAgentBody, request: Request):
    """Переименовать машину / помощника / описание, или перенести в др. машину.

    machine_label — общий атрибут группы агентов с одинаковым machine_fingerprint
    (UI карандаш висит на шапке машины). При изменении machine_label
    обновляем ВСЕ строки этого fingerprint у этого owner-а, иначе grouping
    в /agents показывает разный label у разных агентов одной машины (race).
    agent_label / description / project — per-agent, обновляется только этот row.
    move_fingerprint/move_label — «перенос» этого агента в другую машину-группу
    (per-agent, не broadcast). Пустой move_fingerprint → группа «Без машины».
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id::text AS owner, machine_fingerprint "
            "FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(row["owner"]) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")
        fingerprint = row["machine_fingerprint"]

        rows_changed = 0

        # 0a) agent_label → per-agent (переименование конкретного помощника)
        if body.agent_label is not None:
            res = await conn.execute(
                "UPDATE agent_states SET agent_label = $1, updated_at = NOW() "
                "WHERE agent_id = $2",
                body.agent_label.strip() or None, agent_id,
            )
            try:
                rows_changed += int(res.split()[-1])
            except (ValueError, IndexError):
                pass

        # 0b) перенос в другую машину-группу (per-agent)
        if body.move_fingerprint is not None:
            target_fp = body.move_fingerprint.strip() or None
            if target_fp is not None:
                # валидируем: целевая группа должна принадлежать этому owner-у
                # (нельзя «перенести» в чужой fingerprint). Берём её label если
                # move_label не задан явно.
                tgt = await conn.fetchrow(
                    "SELECT machine_label FROM agent_states "
                    "WHERE owner_user_id = $1 AND machine_fingerprint = $2 LIMIT 1",
                    user.user_id, target_fp,
                )
                target_label = (body.move_label.strip() if body.move_label else None) or (
                    tgt["machine_label"] if tgt else None
                ) or "Машина"
            else:
                target_label = None  # «Без машины»
            res = await conn.execute(
                "UPDATE agent_states "
                "SET machine_fingerprint = $1, machine_label = $2, updated_at = NOW() "
                "WHERE agent_id = $3 AND owner_user_id = $4",
                target_fp, target_label, agent_id, user.user_id,
            )
            try:
                rows_changed += int(res.split()[-1])
            except (ValueError, IndexError):
                pass

        # 1) machine_label → broadcast to entire machine group (если есть fingerprint)
        if body.machine_label is not None:
            if fingerprint:
                res = await conn.execute(
                    "UPDATE agent_states SET machine_label = $1, updated_at = NOW() "
                    "WHERE owner_user_id = $2 AND machine_fingerprint = $3",
                    body.machine_label, user.user_id, fingerprint,
                )
            else:
                # legacy агент без fingerprint — обновляем только его
                res = await conn.execute(
                    "UPDATE agent_states SET machine_label = $1, updated_at = NOW() "
                    "WHERE agent_id = $2",
                    body.machine_label, agent_id,
                )
            try:
                rows_changed += int(res.split()[-1])
            except (ValueError, IndexError):
                pass

        # 2) per-agent fields → только этот row
        per_agent_sets, per_agent_vals = [], []
        if body.description is not None:
            per_agent_sets.append(f"notes = ${len(per_agent_vals)+1}")
            per_agent_vals.append(body.description)
        if body.project is not None:
            per_agent_sets.append(f"project = ${len(per_agent_vals)+1}")
            per_agent_vals.append(body.project)
        if per_agent_sets:
            per_agent_sets.append("updated_at = NOW()")
            per_agent_vals.append(agent_id)
            res = await conn.execute(
                f"UPDATE agent_states SET {', '.join(per_agent_sets)} "
                f"WHERE agent_id = ${len(per_agent_vals)}",
                *per_agent_vals,
            )
            try:
                rows_changed += int(res.split()[-1])
            except (ValueError, IndexError):
                pass

    logger.info(
        "agent_patched user=%s agent=%s fp=%s rows_changed=%s",
        user.user_id, agent_id, fingerprint, rows_changed,
    )
    return {"ok": True, "agent_id": agent_id, "rows_changed": rows_changed}


@router.get("/media")
async def my_media(request: Request, limit: int = 24):
    """Список последних загрузок media владельца (через cogmedia или /ui/admin/media).

    Для admin показываем все L1 события domain=media_analysis (owner-key auth
    не имеет user_id binding, но admin владеет всем). Для не-admin —
    события где source_agent принадлежит этому user_id.
    """
    user = await require_user(request)
    limit = max(1, min(int(limit), 100))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user.is_admin:
            # Admin видит ВСЕ медиа на сервере
            rows = await conn.fetch(
                """
                SELECT id::text AS id, source_agent, raw_payload, timestamp::text AS timestamp
                  FROM l1_raw_events
                 WHERE domain = 'media_analysis'
                 ORDER BY timestamp DESC
                 LIMIT $1
                """,
                limit,
            )
        else:
            # Non-admin: только своих агентов
            rows = await conn.fetch(
                """
                SELECT e.id::text AS id, e.source_agent, e.raw_payload, e.timestamp::text AS timestamp
                  FROM l1_raw_events e
                  JOIN agent_states ast ON ast.agent_id = e.source_agent
                 WHERE e.domain = 'media_analysis'
                   AND ast.owner_user_id = $1::uuid
                 ORDER BY e.timestamp DESC
                 LIMIT $2
                """,
                user.user_id, limit,
            )
    items = []
    for r in rows:
        d = dict(r)
        payload = d.get("raw_payload")
        if isinstance(payload, str):
            try:
                import json as _j
                payload = _j.loads(payload)
            except Exception:
                payload = {}
        d["raw_payload"] = payload
        # Compact summary fields for UI
        d["media_id"] = payload.get("media_id", "?") if isinstance(payload, dict) else "?"
        d["kind"] = payload.get("kind", "?") if isinstance(payload, dict) else "?"
        d["filename"] = payload.get("filename", "?") if isinstance(payload, dict) else "?"
        d["size_bytes"] = payload.get("size_bytes", 0) if isinstance(payload, dict) else 0
        # Thumbnail URL
        if d["kind"] == "video" and isinstance(payload, dict):
            frames = payload.get("frames", [])
            d["thumbnail"] = frames[0].get("url", "") if frames else ""
        elif d["kind"] == "image" and isinstance(payload, dict):
            d["thumbnail"] = payload.get("url", "")
        else:
            d["thumbnail"] = ""  # audio has no thumb
        d["transcript"] = (payload.get("transcript") or "") if isinstance(payload, dict) else ""
        # TTL: 15 мин с момента upload. После — MinIO файлы удалены, metadata
        # остаётся. UI показывает chip «удалён» вместо thumbnail.
        d["cleaned_up"] = bool(payload.get("cleaned_up")) if isinstance(payload, dict) else False
        items.append(d)
    return {"count": len(items), "items": items, "ttl_minutes": 15}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    """Удалить помощника: revoke все api_keys + удалить agent_states.

    Hard delete — записи L1/L2 от этого agent_id остаются (история не теряется),
    но agent больше не может писать новые события (key revoked).
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")
        async with conn.transaction():
            # Revoke all api_keys (soft revoke — last_used_at + revoked_at = NOW)
            await conn.execute(
                "UPDATE agent_keys SET revoked_at = NOW() WHERE agent_id = $1 AND revoked_at IS NULL",
                agent_id,
            )
            # Hard delete agent_states row
            await conn.execute(
                "DELETE FROM agent_states WHERE agent_id = $1",
                agent_id,
            )
    logger.info("agent_deleted user=%s agent=%s", user.user_id, agent_id)
    return {"ok": True, "agent_id": agent_id, "message": "Помощник удалён, api_keys revoke'нуты"}


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
