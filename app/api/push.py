"""Подписка браузера на уведомления и рассылка по событиям комнат.

До этого приложение узнавало о новом сообщении только пока открыто: закрыл
вкладку — и ответ агента ждёт, пока не заглянешь. Уведомление на телефон
закрывает единственный оставшийся разрыв между «агент ответил» и «я узнал».

Доставка сидит на pg_notify('room_event'), который триггер room_msg_notify
шлёт с апреля и который до сих пор слушал только мост в NATS. Ни сервис
комнат, ни его деплой трогать не пришлось.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import require_user
from app.services.webpush import GONE_STATUSES, is_enabled, send_push, vapid_public_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])


class SubscribeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(..., min_length=20, max_length=2000)
    p256dh: str = Field(..., min_length=10, max_length=500)
    auth: str = Field(..., min_length=4, max_length=200)
    user_agent: str | None = Field(None, max_length=300)


@router.get("/vapid-public-key")
async def get_vapid_public_key():
    """Публичный ключ для подписки в браузере.

    503, а не пустой ответ: страница обязана отличить «не настроено» от
    «настроено, но ключ пустой», иначе кнопка уведомлений молча не сработает.
    """
    key = vapid_public_key()
    if not key:
        raise HTTPException(status_code=503, detail="Уведомления не настроены на сервере")
    return {"key": key}


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, request: Request):
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth, user_agent)
            VALUES ($1, $2::uuid, $3, $4, $5)
            -- Повторная подписка того же устройства обновляет строку. Без
            -- этого одно событие приходило бы на телефон столько раз, сколько
            -- раз страница переподписывалась.
            ON CONFLICT (endpoint) DO UPDATE
               SET user_id = EXCLUDED.user_id,
                   p256dh = EXCLUDED.p256dh,
                   auth = EXCLUDED.auth,
                   user_agent = EXCLUDED.user_agent,
                   failures = 0
            """,
            body.endpoint, user.user_id, body.p256dh, body.auth, body.user_agent,
        )
    return {"ok": True, "enabled": is_enabled()}


@router.post("/unsubscribe")
async def unsubscribe(body: SubscribeBody, request: Request):
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Владельца проверяем в WHERE: чужой endpoint не должен сниматься по
        # одному только знанию строки.
        res = await conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = $1 AND user_id = $2::uuid",
            body.endpoint, user.user_id,
        )
    return {"ok": True, "removed": res.rsplit(" ", 1)[-1] == "1"}


@router.post("/test")
async def send_test(request: Request):
    """Отправить уведомление себе — единственный способ проверить всю цепочку.

    Цепочка длинная (ключи → подписка → push-сервис браузера → воркер), и
    отказ в любом звене выглядит одинаково: тишина.
    """
    user = await require_user(request)
    if not is_enabled():
        raise HTTPException(status_code=503, detail="Уведомления не настроены на сервере")
    sent = await notify_user(str(user.user_id), title="Cognitive Core",
                             body="Уведомления работают.", url="/chat")
    if sent == 0:
        raise HTTPException(status_code=404, detail="Нет подписок для этого пользователя")
    return {"ok": True, "sent": sent}


async def notify_user(user_id: str, *, title: str, body: str, url: str = "/chat",
                      tag: str = "cc-room") -> int:
    """Разослать по всем устройствам пользователя. Возвращает число доставленных."""
    if not is_enabled():
        return 0
    pool = await get_pool()
    # Соединение берём дважды и коротко. Отправка ходит в сеть к push-сервису
    # браузера с таймаутом 10 с на подписку; держать на это время строку из
    # пула (их всего 10) значит менять уведомление на отказ всего API.
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = $1::uuid",
                user_id,
            )
        except Exception as e:
            logger.info("push: подписки недоступны (%s)", type(e).__name__)
            return 0

    sent = 0
    dead: list[str] = []
    for r in rows:
        code = await send_push(dict(r), {"title": title, "body": body,
                                         "url": url, "tag": tag})
        if code in GONE_STATUSES:
            dead.append(r["endpoint"])
        elif 200 <= code < 300:
            sent += 1

    if dead:
        # Мёртвые адреса удаляем сразу: иначе каждое событие тратит на них
        # сетевой запрос, и их число только растёт.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ANY($1::text[])", dead)
        logger.info("push: снято мёртвых подписок: %d", len(dead))
    return sent


# ─────────────────────────────────────────────────────────────────────────
# Слушатель событий комнат
# ─────────────────────────────────────────────────────────────────────────

_listener_task: asyncio.Task | None = None
# Сообщения самого владельца уведомлять не нужно: from_agent у них — owner:email
# (см. /user/rooms/{id}/post). Иначе телефон звонит на собственный текст.
_OWNER_PREFIX = "owner:"


async def _already_handled(message_id: str) -> bool:
    """Гонка воркеров: в проде их четыре, LISTEN получает каждый.

    Без общей отметки одно сообщение даёт четыре одинаковых уведомления.
    Redis недоступен — считаем, что не обработано: лучше дубль, чем тишина.
    """
    try:
        from app.db.redis import get_redis

        redis = await get_redis()
        # SET NX возвращает None, если ключ уже есть, — значит взял кто-то другой.
        got = await redis.set(f"push:sent:{message_id}", "1", nx=True, ex=300)
        return not got
    except Exception:
        return False


async def _handle_event(payload: str) -> None:
    try:
        ev = json.loads(payload)
    except Exception:
        return
    if ev.get("event") != "message":
        return
    from_agent = ev.get("from_agent") or ""
    if from_agent.startswith(_OWNER_PREFIX):
        return
    message_id = ev.get("message_id") or ""
    if message_id and await _already_handled(message_id):
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id::text AS owner, name FROM rooms WHERE id = $1::uuid",
            ev.get("room_id"),
        )
    if not row or not row["owner"]:
        return
    text = (ev.get("text") or "").strip()
    await notify_user(
        row["owner"],
        title=row["name"] or "Комната",
        body=f"{from_agent}: {text}" if text else f"{from_agent} прислал вложение",
        url="/chat",
        # Тег по комнате: несколько сообщений подряд заменяют друг друга в
        # шторке, а не заваливают её.
        tag=f"room-{ev.get('room_id')}",
    )


async def start_room_listener() -> None:
    """Подписаться на room_event. Ошибку не поднимаем: пуш — не критичный путь."""
    global _listener_task
    if _listener_task is not None or not is_enabled():
        return

    async def _run() -> None:
        pool = await get_pool()
        conn = await pool.acquire()
        try:
            await conn.add_listener(
                "room_event",
                lambda _c, _pid, _ch, payload: asyncio.create_task(_handle_event(payload)),
            )
            logger.info("push: слушаю room_event")
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("push: слушатель room_event остановлен: %s", type(e).__name__)
        finally:
            try:
                await pool.release(conn)
            except Exception:
                pass

    _listener_task = asyncio.create_task(_run())


async def stop_room_listener() -> None:
    global _listener_task
    if _listener_task is None:
        return
    _listener_task.cancel()
    try:
        await _listener_task
    except (asyncio.CancelledError, Exception):
        pass
    _listener_task = None
