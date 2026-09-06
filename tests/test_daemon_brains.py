"""Один инструментный цикл для всех мозгов заместителя
(план «связь owner↔флот», Фаза 5а, 2026-09-05).

До этого только DeepSeek-персона умела вызывать инструменты. Каналы `managed`
(настоящий Claude по API) и `custom_llm` (GPT/DeepSeek/любой OpenAI-совместимый)
отвечали одним ходом: без истории комнаты, без cognitive_recall/remember, а
managed — ещё и на снятой модели claude-3-5-sonnet-20241022. «Одна память на
весь флот» была мифом. Здесь закрепляем:

  • Anthropic-цикл: tool_use → execute_tool → tool_result → финальный текст;
  • OpenAI-цикл параметризован провайдером (base_url/api_key/model);
  • handle_managed / handle_custom_llm постят ответ в комнату с меткой модели;
  • cognitive_remember — второй инструмент общей памяти;
  • routine не fire-and-forget: таймаут → запасной ответ.

Сеть подменяется, БД не нужна.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cognitive-agent-runtime.py"
ROOM = "14d88eec-0549-433b-a8e8-fdde68862ee4"


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    d = tmp_path_factory.mktemp("rt-brains")
    os.environ["COGCORE_RUNTIME_LOG"] = str(d / "rt.log")
    os.environ["COGCORE_RUNTIME_HISTORY"] = str(d)
    spec = importlib.util.spec_from_file_location("cogcore_rt_brains", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _persona(daemon, **over):
    p = daemon.default_persona("dsdsd", "Память")
    p.update(over)
    return p


def _room_msg(text="привет, что помнишь про деплой?"):
    return {"id": "m-100", "from": "owner", "text": text,
            "context": {"via": "room", "room_id": ROOM}}


@pytest.fixture
def quiet(daemon, monkeypatch):
    """Комната без истории, ключи резолвятся, посты перехватываются."""
    posted = {}
    monkeypatch.setattr(daemon, "room_recent_context", lambda msg, limit=12: "")
    monkeypatch.setattr(daemon, "resolve_agent_key", lambda a: "agent-key")
    monkeypatch.setattr(daemon, "resolve_room_key", lambda r: "rk")
    monkeypatch.setattr(daemon, "post_to_room",
                        lambda room_id, frm, text, history, model=None: posted.update(
                            room=room_id, frm=frm, text=text, model=model) or "reply-1")
    monkeypatch.setattr(daemon, "send_dm", lambda *a, **k: "dm-1")
    return posted


# ─── Anthropic-цикл ───────────────────────────────────────────────────────

def test_anthropic_tool_schema_conversion(daemon):
    tools = daemon._anthropic_tools(daemon.get_tools_for_persona(_persona(daemon)))
    names = {t["name"] for t in tools}
    assert {"cognitive_recall", "cognitive_remember"} <= names
    for t in tools:
        assert set(t) == {"name", "description", "input_schema"}
        assert t["input_schema"]["type"] == "object"


def test_anthropic_loop_executes_tool_then_answers(daemon, quiet, monkeypatch):
    calls = []

    def fake_post(payload, api_key, timeout=60):
        calls.append(payload)
        if len(calls) == 1:
            assert payload["model"] == "claude-sonnet-5"
            assert any(t["name"] == "cognitive_recall" for t in payload["tools"])
            return {"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Сейчас посмотрю."},
                {"type": "tool_use", "id": "tu1", "name": "cognitive_recall",
                 "input": {"query": "деплой", "domain": "deploy"}},
            ]}
        # второй ход: должен прийти tool_result с тем же id
        last = payload["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
        assert last["content"][0]["tool_use_id"] == "tu1"
        assert "recall-hit" in last["content"][0]["content"]
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Деплой идёт через auto-deploy.sh."}]}

    monkeypatch.setattr(daemon, "_anthropic_post", fake_post)
    monkeypatch.setattr(daemon, "execute_tool", lambda name, args: f"recall-hit:{name}:{args.get('query')}")
    reply = daemon.anthropic_reply_with_tools(_persona(daemon), _room_msg(), api_key="sk", model=None)
    assert reply == "Деплой идёт через auto-deploy.sh."
    assert len(calls) == 2


def test_anthropic_retired_model_falls_back_once(daemon, quiet, monkeypatch):
    import io
    import urllib.error
    seen = []

    def fake_post(payload, api_key, timeout=60):
        seen.append(payload["model"])
        if payload["model"] == "claude-3-5-sonnet-20241022":
            raise urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":{"type":"not_found_error","message":"model: claude-3-5-sonnet-20241022"}}'))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ок"}]}

    monkeypatch.setattr(daemon, "_anthropic_post", fake_post)
    reply = daemon.anthropic_reply_with_tools(_persona(daemon), _room_msg(), api_key="sk",
                                              model="claude-3-5-sonnet-20241022")
    assert reply == "ок"
    assert seen == ["claude-3-5-sonnet-20241022", daemon.ANTHROPIC_FALLBACK_MODEL]


def test_managed_posts_to_room_with_model_label(daemon, quiet, monkeypatch):
    monkeypatch.setattr(daemon, "anthropic_reply_with_tools",
                        lambda persona, msg, api_key, model=None, label="managed": "ответ Claude")
    persona = _persona(daemon, wake_channel="managed", channel_config={"api_key": "sk"})
    history = daemon.HistoryStore("dsdsd")
    sid = daemon.handle_managed(persona, _room_msg(), history)
    assert sid == "reply-1"
    assert quiet["room"] == ROOM and quiet["text"] == "ответ Claude"
    assert quiet["model"] == "managed:" + daemon.ANTHROPIC_DEFAULT_MODEL


# ─── OpenAI-совместимый цикл (GPT / DeepSeek / …) ─────────────────────────

def test_custom_llm_uses_shared_tool_loop(daemon, quiet, monkeypatch):
    calls = []

    def fake_http_post(url, payload, headers=None, timeout=15):
        calls.append((url, payload, headers))
        if len(calls) == 1:
            assert url == "https://api.proxyapi.ru/openai/v1/chat/completions"
            assert headers["Authorization"] == "Bearer gpt-key"
            assert any(t["function"]["name"] == "cognitive_remember" for t in payload["tools"])
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "cognitive_remember",
                    "arguments": '{"domain":"crm","task":"итог","result":"решили X"}'}}]}}]}
        assert payload["messages"][-1]["role"] == "tool" and payload["messages"][-1]["tool_call_id"] == "c1"
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Записал: решили X."}}]}

    monkeypatch.setattr(daemon, "http_post", fake_http_post)
    executed = {}
    monkeypatch.setattr(daemon, "execute_tool", lambda name, args: executed.update(name=name, args=args) or "Записано")
    persona = _persona(daemon, wake_channel="custom_llm",
                       channel_config={"base_url": "https://api.proxyapi.ru/openai/v1",
                                       "api_key": "gpt-key", "model": "gpt-5.5"})
    sid = daemon.handle_custom_llm(persona, _room_msg("зафиксируй: решили X"), daemon.HistoryStore("dsdsd"))
    assert sid == "reply-1"
    assert executed == {"name": "cognitive_remember", "args": {"domain": "crm", "task": "итог", "result": "решили X"}}
    assert quiet["model"] == "gpt-5.5" and quiet["text"] == "Записал: решили X."


def test_room_context_is_prepended(daemon, monkeypatch):
    monkeypatch.setattr(daemon, "resolve_room_key", lambda r: "rk")
    monkeypatch.setattr(daemon, "http_get", lambda url, headers=None, timeout=10: {"messages": [
        {"id": "m-1", "from_agent": "owner", "display_name": "Владелец", "text": "первое", "created_at": "2026-09-05T10:00:00"},
        {"id": "m-100", "from_agent": "owner", "text": "текущее", "created_at": "2026-09-05T10:01:00"},
    ]})
    ctx = daemon.room_recent_context(_room_msg())
    assert "Владелец: первое" in ctx
    assert "текущее" not in ctx, "текущее сообщение не дублируется в контексте"


# ─── cognitive_remember ───────────────────────────────────────────────────

def test_remember_tool_writes_owner_scoped_event(daemon, monkeypatch):
    sent = {}
    monkeypatch.setattr(daemon, "resolve_agent_key", lambda a: "agent-key")
    monkeypatch.setattr(daemon, "http_post", lambda url, payload, headers=None, timeout=15: sent.update(
        url=url, payload=payload, headers=headers) or {"id": "evt-1234"})
    daemon._CURRENT_AGENT = "dsdsd"
    try:
        out = daemon.tool_cognitive_remember("crm", "итог встречи", result="решили X", lessons="не спешить")
    finally:
        daemon._CURRENT_AGENT = None
    assert out.startswith("Записано в память: domain=crm")
    assert sent["url"].endswith("/events") and sent["headers"] == {"X-API-Key": "agent-key"}
    assert sent["payload"]["domain"] == "crm" and sent["payload"]["source_agent"] == "dsdsd"
    assert daemon.tool_cognitive_remember("Bad Domain!", "x").startswith("ERROR")


# ─── routine: таймаут → запасной ответ ────────────────────────────────────

def test_routine_timeout_triggers_fallback(daemon, quiet, monkeypatch):
    persona = _persona(daemon, wake_channel="claude_routine",
                       channel_config={"fire_url": "https://api.anthropic.com/v1/claude_code/routines/x/fire",
                                       "token": "t", "api_key": "sk"})
    history = daemon.HistoryStore("dsdsd")
    history.data["routine_pending"] = {"m-100": {
        "fired_at": daemon.time.time() - daemon.ROUTINE_REPLY_TIMEOUT_SEC - 5,
        "session": "sess1", "msg": _room_msg()}}
    monkeypatch.setattr(daemon, "live_agent_active", lambda *a, **k: False)
    called = {}
    monkeypatch.setattr(daemon, "handle_managed", lambda p, m, h: called.update(msg=m["id"]) or "reply-1")
    n = daemon.check_routine_timeouts(persona, history)
    assert n == 1 and called["msg"] == "m-100"
    assert history.data["routine_pending"] == {}


def test_routine_answered_itself_no_fallback(daemon, quiet, monkeypatch):
    persona = _persona(daemon, wake_channel="claude_routine", channel_config={"api_key": "sk"})
    history = daemon.HistoryStore("dsdsd")
    history.data["routine_pending"] = {"m-100": {
        "fired_at": daemon.time.time() - daemon.ROUTINE_REPLY_TIMEOUT_SEC - 5,
        "session": "sess1", "msg": _room_msg()}}
    monkeypatch.setattr(daemon, "live_agent_active", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "handle_managed", lambda p, m, h: pytest.fail("fallback не должен звать managed"))
    assert daemon.check_routine_timeouts(persona, history) == 0
    assert history.data["routine_pending"] == {}
