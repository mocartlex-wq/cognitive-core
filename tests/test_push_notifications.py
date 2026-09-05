"""Web Push: уведомление на телефон, когда приложение закрыто.

Разрыв, который это закрывает: приложение узнавало о новом сообщении только
пока открыто. Закрыл вкладку — и ответ агента ждёт, пока не заглянешь. Ровно
это и произошло 23.08, когда владелец написал «@Память привет» и не получил
ответа: живая сессия комнату не опрашивает, а заместители сняты.

Доставка сидит на pg_notify('room_event') — триггер room_msg_notify шлёт его с
апреля, и слушал его только мост в NATS. Сервис комнат не трогали.

Три вещи проверяются особо, потому что все три отказывают молча:
- в проде четыре воркера uvicorn, LISTEN получает каждый → без общей отметки
  одно сообщение даёт четыре уведомления;
- сообщения самого владельца (from_agent = owner:email) уведомлять нельзя,
  иначе телефон звонит на собственный текст;
- ненастроенные ключи обязаны выглядеть как «уведомлений нет», а не как
  пятисотка на каждом сообщении.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

ROOT = pathlib.Path(__file__).resolve().parent.parent
USER = "11111111-1111-1111-1111-111111111111"
ROOM = "22222222-2222-2222-2222-222222222222"


def _pool(*, fetch=None, fetchrow=None, execute="DELETE 1"):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch if fetch is not None else [])
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.execute = AsyncMock(return_value=execute)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, conn


def _sub(endpoint="https://push.example/aaa"):
    return {"endpoint": endpoint, "p256dh": "key", "auth": "auth"}


# ─── выключенное состояние ───────────────────────────────────────────────────

async def test_public_key_is_503_when_not_configured(monkeypatch):
    """Пустой ответ не отличить от «ключ есть, но пустой» — страница промолчит."""
    from app.api import push

    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        await push.get_vapid_public_key()
    assert e.value.status_code == 503


async def test_notify_does_nothing_without_keys(monkeypatch):
    from app.api import push

    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    with patch.object(push, "get_pool", AsyncMock()) as gp:
        sent = await push.notify_user(USER, title="t", body="b")
    assert sent == 0
    gp.assert_not_called(), "без ключей в базу ходить незачем"


async def test_missing_table_is_survivable(monkeypatch):
    """Миграция не накачена — не пятисотка, а ноль отправленных."""
    from app.api import push

    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    pool, conn = _pool()
    conn.fetch = AsyncMock(side_effect=Exception('relation "push_subscriptions" does not exist'))
    with patch.object(push, "get_pool", AsyncMock(return_value=pool)):
        assert await push.notify_user(USER, title="t", body="b") == 0


# ─── подписка ────────────────────────────────────────────────────────────────

async def test_resubscribe_updates_instead_of_duplicating():
    """Иначе одно событие приходит на телефон столько раз, сколько подписок."""
    from app.api import push

    pool, conn = _pool()
    who = MagicMock()
    who.user_id = USER
    body = push.SubscribeBody(endpoint="https://push.example/aaa" + "x" * 10,
                              p256dh="p" * 20, auth="auth123")
    with patch.object(push, "require_user", AsyncMock(return_value=who)), \
         patch.object(push, "get_pool", AsyncMock(return_value=pool)):
        await push.subscribe(body, MagicMock())
    sql = conn.execute.call_args[0][0]
    assert "ON CONFLICT (endpoint)" in sql, (
        "без ON CONFLICT по endpoint каждая переподписка заводит новую строку"
    )
    assert "DO UPDATE" in sql


async def test_unsubscribe_is_scoped_to_owner():
    """Чужой endpoint не должен сниматься по одному знанию строки."""
    from app.api import push

    pool, conn = _pool(execute="DELETE 0")
    who = MagicMock()
    who.user_id = USER
    body = push.SubscribeBody(endpoint="https://push.example/" + "b" * 10,
                              p256dh="p" * 20, auth="auth123")
    with patch.object(push, "require_user", AsyncMock(return_value=who)), \
         patch.object(push, "get_pool", AsyncMock(return_value=pool)):
        out = await push.unsubscribe(body, MagicMock())
    sql, *params = conn.execute.call_args[0]
    assert "user_id" in sql and USER in params
    assert out["removed"] is False, "DELETE 0 — значит строка не наша, так и отвечаем"


# ─── рассылка ────────────────────────────────────────────────────────────────

async def test_dead_subscriptions_are_removed(monkeypatch):
    """410 — подписки больше нет. Копить их значит тратить запрос на каждую."""
    from app.api import push

    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    pool, conn = _pool(fetch=[_sub("https://push.example/dead"),
                              _sub("https://push.example/live")])

    async def _send(sub, _payload):
        return 410 if sub["endpoint"].endswith("dead") else 201

    with patch.object(push, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(push, "send_push", _send):
        sent = await push.notify_user(USER, title="t", body="b")
    assert sent == 1
    sql, *params = conn.execute.call_args[0]
    assert "DELETE FROM push_subscriptions" in sql
    assert params[0] == ["https://push.example/dead"]


# ─── события комнат ──────────────────────────────────────────────────────────

def _event(from_agent="dsdsd", message_id="m1"):
    return json.dumps({"event": "message", "room_id": ROOM, "message_id": message_id,
                       "from_agent": from_agent, "text": "готово"})


async def test_own_message_does_not_notify():
    """from_agent = owner:email — это написал сам владелец из /chat."""
    from app.api import push

    with patch.object(push, "notify_user", AsyncMock()) as n, \
         patch.object(push, "_already_handled", AsyncMock(return_value=False)):
        await push._handle_event(_event(from_agent="owner:mocartlex@gmail.com"))
    n.assert_not_called()


async def test_agent_message_notifies_room_owner():
    from app.api import push

    pool, _ = _pool(fetchrow={"owner": USER, "name": "Комната"})
    with patch.object(push, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(push, "_already_handled", AsyncMock(return_value=False)), \
         patch.object(push, "notify_user", AsyncMock(return_value=1)) as n:
        await push._handle_event(_event())
    kw = n.call_args.kwargs
    assert n.call_args.args[0] == USER
    assert "dsdsd" in kw["body"] and "готово" in kw["body"]
    assert kw["tag"] == f"room-{ROOM}", (
        "без тега по комнате серия сообщений заваливает шторку вместо замены"
    )


async def test_duplicate_delivery_is_suppressed():
    """Четыре воркера получают один NOTIFY — уведомление должно быть одно."""
    from app.api import push

    with patch.object(push, "_already_handled", AsyncMock(return_value=True)), \
         patch.object(push, "notify_user", AsyncMock()) as n:
        await push._handle_event(_event())
    n.assert_not_called()


async def test_redis_outage_prefers_duplicate_over_silence():
    """Отметку взять негде — шлём. Дубль заметен, пропажа нет."""
    from app.api import push

    with patch("app.db.redis.get_redis", AsyncMock(side_effect=RuntimeError("redis off"))):
        assert await push._already_handled("m1") is False


async def test_non_message_events_are_ignored():
    from app.api import push

    with patch.object(push, "notify_user", AsyncMock()) as n:
        await push._handle_event(json.dumps({"event": "typing", "room_id": ROOM}))
        await push._handle_event("не json вовсе")
    n.assert_not_called()


async def test_room_without_owner_is_skipped():
    """Комнаты, созданные до многопользовательности, владельца не имеют."""
    from app.api import push

    pool, _ = _pool(fetchrow={"owner": None, "name": "Старая"})
    with patch.object(push, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(push, "_already_handled", AsyncMock(return_value=False)), \
         patch.object(push, "notify_user", AsyncMock()) as n:
        await push._handle_event(_event())
    n.assert_not_called()


async def test_listener_does_not_start_without_keys(monkeypatch):
    """Слушатель держит соединение из пула — без ключей он бесполезен."""
    from app.api import push

    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    with patch.object(push, "get_pool", AsyncMock()) as gp:
        await push.start_room_listener()
    gp.assert_not_called()
    assert push._listener_task is None


# ─── схема и воркер ──────────────────────────────────────────────────────────

def test_schema_mirrors_the_migration():
    """Прод катает миграции руками, поэтому таблица заводится и в init_db.

    Две копии DDL обязаны совпадать: разошлись — и свежая установка получит
    схему, которой нет ни в одной миграции.
    """
    init = (ROOT / "app" / "db" / "postgres.py").read_text(encoding="utf-8")
    mig = (ROOT / "alembic" / "versions"
           / "20260823_1600_0023_push_subscriptions.py").read_text(encoding="utf-8")
    for frag in ("endpoint TEXT PRIMARY KEY", "user_id UUID NOT NULL",
                 "p256dh TEXT NOT NULL", "auth TEXT NOT NULL"):
        assert frag in init, f"нет в init_db: {frag}"
        assert frag in mig, f"нет в миграции: {frag}"


def test_service_worker_shows_notifications():
    sw = (ROOT / "sandbox" / "sw.js").read_text(encoding="utf-8")
    assert "addEventListener('push'" in sw
    assert "showNotification" in sw
    assert "addEventListener('notificationclick'" in sw, (
        "уведомление без обработчика нажатия никуда не ведёт"
    )


def test_page_can_subscribe():
    src = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")
    assert "/push/vapid-public-key" in src and "/push/subscribe" in src
    assert "pushManager.subscribe" in src
    # Ключ приходит url-safe base64 без выравнивания; без перекодировки
    # подписка падает на InvalidCharacterError. Проверяем именно ВЫЗОВ на
    # месте подписки: первая версия проверки искала «b64ToU8» где угодно и
    # оставалась зелёной, когда перекодировку из вызова убрали, а функцию
    # оставили лежать без дела. Поймано подменой.
    assert "applicationServerKey:b64ToU8(" in src
