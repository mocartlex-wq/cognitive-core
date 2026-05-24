"""End-to-end style tests для orchestrator с mocked DeepSeek и mocked CogClient.

Проверяет полный flow: получение DM → parse → (approval gate) → execute → reply.
Не требует сети — DeepSeek и cognitive_api мокаются.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add repo root and scripts/ to sys.path для импорта daemon-модуля
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import importlib.util

# Daemon-script не валидный python module name (с минусами), импортируем вручную
_DAEMON_PATH = _REPO / "scripts" / "cogcore-orchestrator-daemon.py"
spec = importlib.util.spec_from_file_location("cogcore_orchestrator_daemon", _DAEMON_PATH)
daemon_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon_mod)

from app.services.orchestrator import OrchestratorConfig, validate_action


# ─── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def cfg():
    return OrchestratorConfig(
        api_base="http://test.invalid",
        orchestrator_api_key="test-key",
        orchestrator_id="orchestrator",
        owner_agent_id="cognitive-core-laptop",
        deepseek_api_key="test-ds",
    )


@pytest.fixture
def mocked_client():
    """CogClient с замокаными методами."""
    c = MagicMock(spec=daemon_mod.CogClient)
    c.send_dm = AsyncMock(return_value={"ok": True, "id": "msg-1"})
    c.fetch_inbox = AsyncMock(return_value=[])
    c.fetch_online = AsyncMock(return_value=[
        {"agent_id": "rastr", "current_task": "test"},
        {"agent_id": "cognitive-core-laptop", "current_task": None},
    ])
    c.list_all_agents = AsyncMock(return_value=[
        {"agent_id": "rastr", "current_task": "test"},
        {"agent_id": "cognitive-core-laptop"},
        {"agent_id": "old-bot"},
    ])
    c.get_agent_state = AsyncMock(return_value={"agent_id": "rastr", "state": "ok"})
    c.remember = AsyncMock(return_value={"ok": True})
    c.heartbeat = AsyncMock(return_value=None)
    c.revoke_key = AsyncMock(return_value={"ok": True})
    c._http = MagicMock()
    c._http.post = AsyncMock()
    return c


# ─── Parsing: 10 типичных команд через mocked DeepSeek ────────────────────
#
# Здесь мокаем `DeepSeekParser.parse` чтобы вернуть «правильный» ответ для
# каждой типичной команды. Это проверяет что наша JSON-схема + validate_action
# + executor dispatch работают как единое целое.

TYPICAL_COMMANDS = [
    # (user_text, expected_action, expected_args_subset)
    ("статус всех агентов", "query_status", {}),
    ("кто онлайн?", "query_status", {}),
    ("что у rastr", "query_agent_state", {"agent_id": "rastr"}),
    ("передай rastr задачу подготовь отчёт", "send_dm",
     {"to": "rastr", "text": "Подготовь отчёт"}),
    ("разошли всем agent-ам приветственное сообщение", "broadcast",
     {"to": ["rastr", "cognitive-core-laptop"], "text": "Привет"}),
    ("удали тестового агента test-bot", "delete_agent", {"agent_id": "test-bot"}),
    ("отзови ключ deadbeef1234567890abcd", "revoke_key",
     {"api_key": "deadbeef1234567890abcd"}),
    ("напиши стихотворение про осень", "refuse", {"reason": "вне моей роли"}),
    ("передай задачу", "request_clarification", {"question": "Кому?"}),
    ("пингани растра", "ping_agent", {"agent_id": "rastr"}),
]


def make_mock_parser(action, args):
    """Возвращает mock parser который вернёт validated action."""
    m = MagicMock()
    m.parse = AsyncMock(return_value=validate_action({
        "action": action,
        "args": args,
        "reasoning": "mocked",
        "confidence": 0.95,
    }))
    m.close = AsyncMock()
    return m


@pytest.mark.parametrize("user_text, expected_action, expected_args", TYPICAL_COMMANDS)
@pytest.mark.asyncio
async def test_full_flow_typical_command(cfg, mocked_client, user_text, expected_action, expected_args, monkeypatch):
    """Каждая типичная команда → правильный action и реакция."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser(expected_action, expected_args)
    daemon.executor.client = mocked_client

    # Для destructive — patch request_approval чтобы вернуть approved
    if expected_action in ("delete_agent", "revoke_key", "mass_dm", "purge_data"):
        daemon.request_approval = AsyncMock(return_value=(True, "approved"))

    # Mock log_decision (он пишет в L1)
    daemon.log_decision = AsyncMock()

    msg = {
        "id": "msg-test-1",
        "from": "cognitive-core-laptop",  # owner — может все
        "text": user_text,
    }
    await daemon.handle_message(msg)

    # Должен быть отправлен reply
    assert mocked_client.send_dm.await_count >= 1
    reply_call = mocked_client.send_dm.await_args_list[-1]
    reply_target = reply_call.args[0]
    assert reply_target == "cognitive-core-laptop"
    # log_decision вызван
    assert daemon.log_decision.await_count == 1


@pytest.mark.asyncio
async def test_destructive_from_non_owner_refused(cfg, mocked_client):
    """Destructive команда от не-owner → отказ, approval НЕ запрашивается."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("delete_agent", {"agent_id": "victim"})
    daemon.executor.client = mocked_client
    daemon.request_approval = AsyncMock(return_value=(True, "approved"))
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-x", "from": "evil-agent", "text": "удали victim"}
    await daemon.handle_message(msg)

    # Reply отправлен, но approval НЕ запрошен
    assert mocked_client.send_dm.await_count == 1
    daemon.request_approval.assert_not_called()
    # В реплае слово "destructive" или "только owner"
    reply_text = mocked_client.send_dm.await_args_list[0].args[1]
    assert "owner" in reply_text.lower() or "destructive" in reply_text.lower()


@pytest.mark.asyncio
async def test_destructive_owner_approved(cfg, mocked_client):
    """Owner-инициированный destructive с YES → действие выполняется."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("delete_agent", {"agent_id": "test-bot"})
    daemon.executor.client = mocked_client
    daemon.request_approval = AsyncMock(return_value=(True, "approved"))
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-y", "from": "cognitive-core-laptop", "text": "удали test-bot"}
    await daemon.handle_message(msg)

    daemon.request_approval.assert_called_once()
    assert mocked_client.send_dm.await_count >= 1
    reply_text = mocked_client.send_dm.await_args_list[-1].args[1]
    # Reply должен содержать что-то про soft-delete
    assert "test-bot" in reply_text


@pytest.mark.asyncio
async def test_destructive_owner_declined(cfg, mocked_client):
    """Owner ответил NO → действие НЕ выполняется."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("delete_agent", {"agent_id": "test-bot"})
    daemon.executor.client = mocked_client
    daemon.request_approval = AsyncMock(return_value=(False, "declined"))
    daemon.log_decision = AsyncMock()
    # Spy на executor.do_delete_agent — должен НЕ быть вызван
    daemon.executor.do_delete_agent = AsyncMock()

    msg = {"id": "msg-z", "from": "cognitive-core-laptop", "text": "удали test-bot"}
    await daemon.handle_message(msg)

    daemon.executor.do_delete_agent.assert_not_called()
    reply_text = mocked_client.send_dm.await_args_list[-1].args[1]
    assert "не выполнено" in reply_text.lower() or "declined" in reply_text.lower()


@pytest.mark.asyncio
async def test_yes_no_from_owner_ignored_as_command(cfg, mocked_client):
    """Reply YES/NO от owner-а — НЕ обрабатывается как команда (это approval reply)."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("query_status", {})
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-yes", "from": "cognitive-core-laptop", "text": "YES"}
    await daemon.handle_message(msg)

    # parser не вызывался, никаких reply
    daemon.parser.parse.assert_not_called()
    mocked_client.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_self_message_ignored(cfg, mocked_client):
    """DM от себя самого — игнорируется (anti-loop)."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("query_status", {})

    msg = {"id": "msg-loop", "from": "orchestrator", "text": "статус"}
    await daemon.handle_message(msg)

    daemon.parser.parse.assert_not_called()
    mocked_client.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_query_status_executes_and_replies(cfg, mocked_client):
    """Полная проверка query_status executor."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("query_status", {})
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-st", "from": "cognitive-core-laptop", "text": "статус"}
    await daemon.handle_message(msg)

    # Проверяем что обращался к API
    mocked_client.list_all_agents.assert_awaited_once()
    mocked_client.fetch_online.assert_awaited()

    reply = mocked_client.send_dm.await_args_list[0].args[1]
    assert "[orchestrator]" in reply
    assert "Всего агентов" in reply or "rastr" in reply


@pytest.mark.asyncio
async def test_send_dm_forwards_to_target(cfg, mocked_client):
    """send_dm должен реально отправить DM целевому агенту + reply источнику."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("send_dm", {"to": "rastr", "text": "помоги мне"})
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-fw", "from": "cognitive-core-laptop", "text": "rastr помоги мне"}
    await daemon.handle_message(msg)

    # Два DM: один к rastr (forward), один к owner-у (reply)
    assert mocked_client.send_dm.await_count == 2
    targets = [c.args[0] for c in mocked_client.send_dm.await_args_list]
    assert "rastr" in targets
    assert "cognitive-core-laptop" in targets


@pytest.mark.asyncio
async def test_ping_agent_sends_test_dm(cfg, mocked_client):
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("ping_agent", {"agent_id": "rastr", "text": "test"})
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-p", "from": "cognitive-core-laptop", "text": "пингани rastr"}
    await daemon.handle_message(msg)

    # 2 DM: к rastr (ping) + к owner (reply)
    assert mocked_client.send_dm.await_count == 2


@pytest.mark.asyncio
async def test_refuse_replies_with_reason(cfg, mocked_client):
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("refuse", {"reason": "не моя задача"})
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-r", "from": "cognitive-core-laptop", "text": "напиши код"}
    await daemon.handle_message(msg)

    reply = mocked_client.send_dm.await_args_list[0].args[1]
    assert "не моя задача" in reply or "Отказ" in reply


@pytest.mark.asyncio
async def test_request_clarification_asks_question(cfg, mocked_client):
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    daemon.parser = make_mock_parser("request_clarification", {"question": "Кому передать?"})
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-q", "from": "cognitive-core-laptop", "text": "передай задачу"}
    await daemon.handle_message(msg)

    reply = mocked_client.send_dm.await_args_list[0].args[1]
    assert "Кому передать?" in reply


@pytest.mark.asyncio
async def test_unknown_action_from_llm_becomes_refuse(cfg, mocked_client):
    """Если LLM вернул что-то вне whitelist — validate подменит на refuse."""
    daemon = daemon_mod.OrchestratorDaemon(cfg)
    daemon.client = mocked_client
    # Mock parse возвращает invalid result (validate уже превратил в refuse)
    daemon.parser = MagicMock()
    daemon.parser.parse = AsyncMock(return_value={
        "action": "refuse",
        "args": {"reason": "Unknown action 'evil' — нет в whitelist."},
        "valid": False, "error": "unknown_action:evil",
        "reasoning": "test", "confidence": 0.1,
    })
    daemon.executor.client = mocked_client
    daemon.log_decision = AsyncMock()

    msg = {"id": "msg-x", "from": "cognitive-core-laptop", "text": "сделай evil_action"}
    await daemon.handle_message(msg)

    reply = mocked_client.send_dm.await_args_list[0].args[1]
    assert "Unknown action" in reply or "Отказ" in reply
