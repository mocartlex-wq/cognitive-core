"""Отправка Web Push — уведомление на телефон при закрытом приложении.

Ключи VAPID берутся из окружения и здесь не генерируются: приватный ключ —
секрет, и владелец вписывает его сам. Без ключей модуль выключен целиком и
молча: не настроенный пуш обязан выглядеть как «уведомлений нет», а не как
пятисотка на каждом сообщении.

    python -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); \
               print(v.private_key_pem()); print(v.public_key_urlsafe_base64())"

Кладётся в .env как VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY / VAPID_SUBJECT.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 404/410 от push-сервиса означают «подписки больше нет»: пользователь снёс
# приложение или отозвал разрешение. Такую строку нужно удалять, а не копить —
# иначе каждое событие тратит запрос на заведомо мёртвый адрес.
GONE_STATUSES = (404, 410)


def vapid_public_key() -> str | None:
    return os.getenv("VAPID_PUBLIC_KEY") or None


def is_enabled() -> bool:
    return bool(os.getenv("VAPID_PRIVATE_KEY") and os.getenv("VAPID_PUBLIC_KEY"))


def _subject() -> str:
    # RFC 8292 требует контакт отправителя; push-сервисы браузеров без него
    # отвечают 400.
    return os.getenv("VAPID_SUBJECT") or "mailto:admin@mcp.me-ai.ru"


def _send_blocking(sub: dict[str, Any], payload: dict[str, Any]) -> int:
    from pywebpush import WebPushException, webpush

    try:
        resp = webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": _subject()},
            timeout=10,
        )
        return getattr(resp, "status_code", 201)
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", 0)
        if code in GONE_STATUSES:
            return code
        logger.info("webpush failed endpoint=%s code=%s", sub["endpoint"][:60], code)
        return code or 500


async def send_push(sub: dict[str, Any], payload: dict[str, Any]) -> int:
    """Отправить одно уведомление. Возвращает HTTP-код push-сервиса.

    pywebpush синхронный и ходит в сеть, поэтому уводим его в поток: в event
    loop он заблокировал бы обработку запросов на время до таймаута.
    """
    if not is_enabled():
        return 0
    try:
        return await asyncio.to_thread(_send_blocking, sub, payload)
    except Exception as e:  # включая отсутствие самого пакета
        logger.warning("webpush: отправка не состоялась: %s", type(e).__name__)
        return 500
