"""Присутствие агентов в комнате (Фаза 3 «штаб»).

Владелец не видел, кто из флота реально на связи: список участников отдавался
без признака присутствия, а строка статуса всегда врала «агенты на связи».
Здесь закрепляем контракт: participant получает online + last_seen, а интерфейс
показывает точку на чипе и число «N на связи», не перебивая индикатор печати.

Без БД: пул подменяется моком (как tests/test_owner_gate.py), UI проверяется по
исходнику страницы.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import user as user_mod

OWNER = "35cc4c15-0054-477d-ad35-a7872fff7b71"
WEBCHAT = (pathlib.Path(__file__).resolve().parents[1] / "sandbox" / "webchat.html").read_text(encoding="utf-8")
SRC = pathlib.Path(user_mod.__file__).read_text(encoding="utf-8")


def _pool(room, participants, messages, count=0):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=room)
    conn.fetch = AsyncMock(side_effect=[participants, messages])
    conn.fetchval = AsyncMock(return_value=count)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool


ROOM = {
    "id": "r1", "name": "штаб", "api_key": "rk_x", "is_public": True,
    "owner_user_id": OWNER, "created_at": None,
    "conductor_agent_id": None, "room_mode": "plain",
}


def _participant(**over):
    base = {
        "agent_id": "dsdsd", "role": "member", "joined_at": None, "last_seen_at": None,
        "agent_label": "Память", "auto_respond": False, "standin_enabled": False,
        "last_seen": None, "online": False,
    }
    base.update(over)
    return base


async def _detail(participants):
    pool = _pool(ROOM, participants, [])
    with patch.object(user_mod, "require_user",
                      AsyncMock(return_value=SimpleNamespace(user_id=OWNER, email="o@e"))), \
         patch.object(user_mod, "get_pool", AsyncMock(return_value=pool)):
        return await user_mod.get_my_room_detail("r1", MagicMock())


class TestPresenceMapping:
    @pytest.mark.asyncio
    async def test_online_agent_marked_and_last_seen_iso(self):
        seen = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        d = await _detail([_participant(online=True, last_seen=seen)])
        p = d["participants"][0]
        assert p["online"] is True
        # datetime обязан уехать строкой: JSON-ответ иначе не сериализуется.
        assert p["last_seen"] == seen.isoformat()
        assert isinstance(p["last_seen"], str)

    @pytest.mark.asyncio
    async def test_offline_agent_keeps_null_last_seen(self):
        d = await _detail([_participant(online=False, last_seen=None)])
        p = d["participants"][0]
        assert p["online"] is False
        assert p["last_seen"] is None

    @pytest.mark.asyncio
    async def test_presence_does_not_disturb_existing_fields(self):
        d = await _detail([_participant(online=True, agent_label="Память")])
        p = d["participants"][0]
        assert p["display_name"] == "Память"
        assert p["agent_id"] == "dsdsd"
        assert "standin" in p or "auto_respond" in p


class TestPresenceSQL:
    def test_uses_both_liveness_marks(self):
        # Агент может быть жив по любой из двух отметок; одной недостаточно.
        assert "GREATEST(s.last_mcp_connect_at, s.last_heartbeat_at)" in SRC

    def test_window_is_300_seconds(self):
        assert "INTERVAL '300 seconds'" in SRC

    def test_online_never_null(self):
        # Без COALESCE агент без отметок дал бы online=None, и UI показал бы
        # «неизвестно» как «онлайн».
        i = SRC.index("AS online")
        assert "COALESCE(" in SRC[i - 320:i]

    def test_no_new_tables_used(self):
        i = SRC.index("AS online")
        block = SRC[i - 600:i]
        assert "room_presence" not in block and "presence_table" not in block


class TestPresenceUI:
    def test_chip_shows_dot_for_online(self):
        assert "p.online?'<i class=\"pdot\"></i>':''" in WEBCHAT
        assert ".rcp-chip .pdot{" in WEBCHAT

    def test_dedup_key_includes_presence(self):
        # Иначе бар не перерисуется, когда агент ожил: ключ не изменится.
        assert "+':'+(p.online?1:0)" in WEBCHAT

    def test_status_line_counts_online(self):
        assert "function updatePresence(" in WEBCHAT
        assert "' на связи'" in WEBCHAT or "+' на связи'" in WEBCHAT

    def test_typing_wins_over_presence(self):
        # applyPresence обязан уступать индикатору печати.
        i = WEBCHAT.index("function applyPresence(")
        body = WEBCHAT[i:i + 260]
        assert "if(typingLabels.length) return;" in body

    def test_presence_updated_from_paint_after_typing(self):
        i_t = WEBCHAT.index("updateTypingBar(d && d.typing);")
        i_p = WEBCHAT.index("updatePresence(d && d.participants);")
        assert i_t < i_p, "печать должна применяться раньше присутствия"

    def test_recipients_call_kept(self):
        # Контракт, на который опирается tests/test_single_room_ui.py.
        assert "renderRecipients(d && d.participants)" in WEBCHAT
        assert 'id="rcpBar"' in WEBCHAT

    def test_inline_script_parses(self, tmp_path):
        body = WEBCHAT.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        f = tmp_path / "t.js"
        # new Function, а не node --check: в скрипте страницы есть return верхнего уровня.
        f.write_text("new Function(" + json.dumps(body) + ");", encoding="utf-8")
        r = subprocess.run(["node", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
