"""Закалка демона заместителей по ревью 2026-09-06 (после плана v2).

Семь находок ревью: «ядовитое» сообщение ронял весь проход и seen_ids не
сохранялись; LISTEN-поток висел вечно на полуоткрытом сокете и не экранировал
пароль в DSN; Anthropic-цикл убирал `tools` после лимита (400 от API) и держал
max_tokens=1024 (пустой ответ у Claude 5 → тихий откат на DeepSeek);
залежавшийся routine_pending давал запасной ответ на многочасовое сообщение;
room_recent_context падал в DEBUG; main-цикл падал на null в poll_interval.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types

import pytest

DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cognitive-agent-runtime.py"
ROOM = "b48286a2-e5f2-451f-b773-4617d112d7c0"


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    d = tmp_path_factory.mktemp("rt-hard")
    os.environ["COGCORE_RUNTIME_LOG"] = str(d / "rt.log")
    os.environ["COGCORE_RUNTIME_HISTORY"] = str(d)
    spec = importlib.util.spec_from_file_location("cogcore_rt_hard", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _persona(daemon, **over):
    p = daemon.default_persona("dsdsd", "Память")
    p.update(over)
    return p


def _room_msg(text="привет, что помнишь про деплой?", mid="m-100"):
    return {"id": mid, "from": "owner", "text": text,
            "context": {"via": "room", "room_id": ROOM}}


@pytest.fixture
def quiet(daemon, monkeypatch):
    posted = {}
    monkeypatch.setattr(daemon, "room_recent_context", lambda msg, limit=12: "")
    monkeypatch.setattr(daemon, "resolve_agent_key", lambda a: "agent-key")
    monkeypatch.setattr(daemon, "resolve_room_key", lambda r: "rk")
    monkeypatch.setattr(daemon, "post_to_room",
                        lambda room_id, frm, text, history, model=None: posted.update(
                            room=room_id, frm=frm, text=text, model=model) or "reply-1")
    monkeypatch.setattr(daemon, "send_dm", lambda *a, **k: "dm-1")
    return posted


# ─── HIGH: одно исключение не роняет проход и не теряет seen_ids ─────────
def test_poison_message_does_not_drop_seen_ids(daemon, quiet, monkeypatch):
    persona = _persona(daemon)
    m1 = _room_msg("сломай меня", mid="m-poison")
    m2 = _room_msg("а это нормальное", mid="m-ok")
    monkeypatch.setattr(daemon, "load_inbox", lambda pid, since_minutes=60: [m1, m2])
    handled = []

    def boom(text, pid, label):
        if "сломай" in text:
            raise RuntimeError("poison")
        handled.append(text)
        return True  # «адресовано другому» → тихий выход; важен только seen

    monkeypatch.setattr(daemon, "addressed_to_others", boom)
    saves = []
    real_save = daemon.HistoryStore.save
    monkeypatch.setattr(daemon.HistoryStore, "save", lambda self: saves.append(1) or real_save(self))
    daemon.process_persona(persona)
    assert handled == ["а это нормальное"]
    assert len(saves) == 1
    h = daemon.HistoryStore(persona["persona_id"])
    assert h.already_seen("m-poison") and h.already_seen("m-ok")


# ─── MEDIUM: tools остаются после лимита, tool_choice=none ───────────────
def test_anthropic_keeps_tools_after_budget_with_tool_choice_none(daemon, quiet, monkeypatch):
    payloads = []

    def fake_post(payload, api_key, timeout=60):
        payloads.append(payload)
        if payload.get("tool_choice") == {"type": "none"}:
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Готово."}]}
        return {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": f"tu{len(payloads)}", "name": "cognitive_recall", "input": {"query": "x"}},
        ]}

    monkeypatch.setattr(daemon, "_anthropic_post", fake_post)
    monkeypatch.setattr(daemon, "execute_tool", lambda name, args: "hit")
    reply = daemon.anthropic_reply_with_tools(_persona(daemon), _room_msg(), api_key="sk", model=None)
    assert reply == "Готово."
    assert all("tools" in p for p in payloads), "tools пропали из запроса"
    assert payloads[-1]["tool_choice"] == {"type": "none"}
    assert "tool_choice" not in payloads[0]


# ─── MEDIUM: пол max_tokens под thinking Claude 5 ────────────────────────
def test_anthropic_max_tokens_floor(daemon, quiet, monkeypatch):
    seen = []

    def fake_post(payload, api_key, timeout=60):
        seen.append(payload["max_tokens"])
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ок"}]}

    monkeypatch.setattr(daemon, "_anthropic_post", fake_post)
    for cfg in ({"max_tokens": 300}, {"max_tokens": 9000}, {}):
        daemon.anthropic_reply_with_tools(_persona(daemon, llm_settings=cfg), _room_msg(), api_key="sk", model=None)
    assert seen == [daemon.ANTHROPIC_MIN_MAX_TOKENS, 9000, daemon.ANTHROPIC_MIN_MAX_TOKENS]
    assert daemon.ANTHROPIC_MIN_MAX_TOKENS >= 4096


# ─── MEDIUM: залежавшийся routine_pending снимается молча ────────────────
def test_routine_stale_pending_dropped_without_fallback(daemon, quiet, monkeypatch):
    persona = _persona(daemon, wake_channel="claude_routine", channel_config={"api_key": "sk"})
    history = daemon.HistoryStore("dsdsd")
    history.data["routine_pending"] = {"m-old": {
        "fired_at": daemon.time.time() - daemon.ROUTINE_REPLY_TIMEOUT_SEC * 4 - 5,
        "session": "sess1", "msg": _room_msg()}}
    monkeypatch.setattr(daemon, "live_agent_active", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "handle_managed", lambda p, m, h: pytest.fail("запасной ответ на старьё"))
    monkeypatch.setattr(daemon, "handle_llm_reply", lambda p, m, h: pytest.fail("запасной ответ на старьё"))
    assert daemon.check_routine_timeouts(persona, history) == 0
    assert history.data["routine_pending"] == {}


# ─── LOW: провал истории комнаты виден, но не спамит ─────────────────────
def test_room_context_failure_is_visible_but_rate_limited(daemon, monkeypatch, caplog):
    monkeypatch.setattr(daemon, "resolve_room_key", lambda r: "rk")

    def dead(*a, **k):
        raise RuntimeError("503")

    monkeypatch.setattr(daemon.urllib.request, "urlopen", dead)
    daemon._CTX_WARNED.clear()
    with caplog.at_level("WARNING", logger=daemon.log.name):
        assert daemon.room_recent_context(_room_msg()) == ""
        assert daemon.room_recent_context(_room_msg()) == ""
    warns = [r for r in caplog.records if "room_recent_context failed" in r.getMessage()]
    assert len(warns) == 1 and warns[0].levelname == "WARNING"


# ─── HIGH: DSN экранирует пароль, keepalive'ы включены ───────────────────
def test_dsn_quotes_password_and_enables_keepalives(daemon, monkeypatch):
    class R:
        def __init__(self, out):
            self.stdout = out

    def fake_run(cmd, **kw):
        return R("p@ss%w:rd\n") if "printenv" in cmd else R("172.18.0.5\n")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    dsn = daemon._build_pg_dsn()
    assert "p%40ss%25w%3Ard@172.18.0.5:5432" in dsn
    assert "keepalives=1" in dsn and "keepalives_idle=" in dsn


# ─── HIGH: LISTEN-поток пробует соединение в тишине ──────────────────────
def test_listener_probes_connection_when_idle(daemon, monkeypatch):
    events = []

    class FakeConn:
        probes = 0

        def execute(self, sql):
            events.append(sql)
            if sql == "SELECT 1":
                self.probes += 1
                if self.probes == 2:
                    raise OSError("connection lost")

        def notifies(self, timeout=None):
            events.append(("notifies", timeout))
            return iter(())  # тишина

        def close(self):
            events.append("close")

    fake = types.ModuleType("psycopg")
    fake.connect = lambda *a, **k: FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setattr(daemon, "_build_pg_dsn", lambda: "postgresql://x")

    def fake_sleep(sec):
        raise KeyboardInterrupt  # первый backoff = выход из бесконечного цикла

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.wake_listener()
    assert ("notifies", daemon.LISTEN_PROBE_SEC) in events
    assert events.count("SELECT 1") == 2
    assert "close" in events
    assert daemon._LISTEN_STATE["active"] is False


# ─── LOW: мусор в poll_interval не роняет main-цикл ──────────────────────
def test_as_int_tolerates_garbage(daemon):
    assert daemon._as_int(None, 5) == 5
    assert daemon._as_int("7", 5) == 7
    assert daemon._as_int("abc", 5) == 5
    assert daemon._as_int(0, 5) == 5
    assert daemon._as_int(3, 5) == 3
