"""Один UI на комнаты: /ui/room = /chat (план «связь owner↔флот», Фаза 1).

room.html (опрос раз в 5с, без SW/push) и webchat.html (long-poll /wait, PWA,
push) разошлись: typing-бар и вставка из буфера были только в первом, живая
подписка и уведомления — только во втором. Теперь всё в webchat.html, а
/ui/room отдаёт его же. Проверяем по исходникам, без сервера.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEBCHAT = (ROOT / "sandbox" / "webchat.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def _route_body(path: str) -> str:
    m = re.search(rf'@app\.get\("{re.escape(path)}"\)\s*\nasync def \w+\(\):(.*?)(?=\n@app\.|\Z)', MAIN, re.S)
    assert m, f"нет маршрута {path}"
    return m.group(1)


def test_ui_room_serves_webchat():
    assert '_html("webchat.html")' in _route_body("/ui/room")
    assert '_html("room.html")' in _route_body("/ui/room-legacy")


def test_webchat_has_typing_status_from_detail():
    assert "updateTypingBar(d && d.typing)" in WEBCHAT
    assert "печатает…" in WEBCHAT


def test_webchat_has_recipient_chips_and_mention_insert():
    assert 'id="rcpBar"' in WEBCHAT
    assert "renderRecipients(d && d.participants)" in WEBCHAT
    assert "function insertMention" in WEBCHAT


def test_webchat_paste_uploads_files():
    assert "box.addEventListener('paste'" in WEBCHAT
    assert "clipboardData.files" in WEBCHAT


def test_webchat_opens_room_from_query():
    """Старые ссылки /ui/room?id=… и новые /chat?room=… ведут в нужную комнату."""
    assert "get('room')" in WEBCHAT and "get('id')" in WEBCHAT


def test_webchat_keeps_live_subscription_not_interval_poll():
    assert "/wait?since_seconds=" in WEBCHAT
    assert "serviceWorker.register('/sw.js'" in WEBCHAT
