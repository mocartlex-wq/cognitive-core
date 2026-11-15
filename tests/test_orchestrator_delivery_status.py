"""Legacy task delivery and coordination isolation, without a DB, LLM or network."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import queue
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cognitive-orchestrator.py"
TASK_ID = "7c825b14-f6dd-43e3-91e1-d798bafcfe76"


class FakeConnection:
    """Record DB writes and supply a row without reimplementing task state logic."""

    def __init__(self):
        self.row = {
            "task_id": TASK_ID,
            "request_text": "Проверь результат задачи",
            "context": {},
            "cascading_depth": 0,
            "user_id": "test-owner",
            "status": "queued",
            "trace_events": [],
            "final_answer": None,
        }
        self.rows = []
        self.calls = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return copy.deepcopy(self.row)

    def fetchall(self):
        return copy.deepcopy(self.rows)

    def commit(self):
        self.commits += 1

    def completion(self):
        writes = [params for sql, params in self.calls if "SET status = %s, final_answer = %s" in sql]
        assert len(writes) == 1, self.calls
        status, answer, proxy, task_id = writes[0]
        assert task_id == TASK_ID
        return status, answer, proxy


@pytest.fixture
def harness(monkeypatch):
    # psycopg3 is a host-service dependency, absent from the API test environment.
    # Stub only its import surface and fail closed if a test reaches real I/O.
    driver = types.ModuleType("psycopg")
    driver.connect = Mock(side_effect=AssertionError("unexpected database access"))
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", driver)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    spec = importlib.util.spec_from_file_location("legacy_orchestrator_delivery_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.urllib.request, "urlopen", Mock(side_effect=AssertionError("unexpected network access")))
    connection = FakeConnection()
    monkeypatch.setattr(module, "db", lambda: connection)
    events = []
    monkeypatch.setattr(module, "publish_event", lambda task_id, event: events.append(copy.deepcopy(event)))
    for name, value in {
        "list_active_capabilities": [{"agent_id": "executor-a", "capability": "general"}],
        "route_task": (["executor-a"], "matched skill"),
        "dispatch_task": "message-a",
        "collect_responses": {"executor-a": {"text": "Проверка пройдена", "received_at": "2026-11-15T12:00:00Z"}},
        "_recall_for_user": "",
        "deepseek_chat": "Сводный ответ",
    }.items():
        monkeypatch.setattr(module, name, Mock(return_value=value))
    return types.SimpleNamespace(module=module, db=connection, events=events)


def assert_terminal(harness, expected):
    status, answer, proxy = harness.db.completion()
    assert status == expected
    # Legacy clients close their stream on type=completed, including failures.
    assert harness.events[-1]["type"] == "completed"
    assert harness.events[-1]["status"] == expected
    return answer, proxy


def test_no_executor_is_failed_without_dispatch_or_synthesis(harness):
    harness.module.route_task.return_value = ([], "no available executor")
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "failed")
    assert "не выполнена" in answer.lower()
    assert "помощник" in answer.lower()
    harness.module.dispatch_task.assert_not_called()
    harness.module.collect_responses.assert_not_called()
    harness.module.deepseek_chat.assert_not_called()


def test_total_delivery_failure_is_failed(harness):
    harness.module.route_task.return_value = (["executor-a", "executor-b"], "two skills")
    harness.module.dispatch_task.return_value = None
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "failed")
    assert "не удалось доставить" in answer.lower()
    assert harness.module.dispatch_task.call_count == 2
    assert [event["agent"] for event in harness.events if event["type"] == "dispatch_failed"] == ["executor-a", "executor-b"]
    harness.module.collect_responses.assert_not_called()
    harness.module.deepseek_chat.assert_not_called()


@pytest.mark.parametrize("responses", [{}, {"executor-a": {"text": " \n "}}, {"executor-a": {"text": "[from executor-a]\n"}}])
def test_no_meaningful_reply_keeps_consultation_but_fails_task(harness, responses):
    harness.module.collect_responses.return_value = responses
    harness.module.deepseek_chat.return_value = "Рекомендую проверить журнал."
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "failed")
    assert proxy is True
    assert "не подтверждено" in answer.lower()
    assert "консультаци" in answer.lower()
    assert "Рекомендую проверить журнал." in answer
    assert any(event["type"] == "proxy_fallback" for event in harness.events)
    prompt = harness.module.deepseek_chat.call_args.args[0][0]["content"]
    assert "не утверждай" in prompt.lower()
    # Keep the original replies for diagnostics, even when their text is empty.
    saved = [params for sql, params in harness.db.calls if "SET responses = %s::jsonb" in sql]
    assert json.loads(saved[0][0]) == responses


def test_no_reply_and_failed_consultation_still_terminates_failed(harness):
    harness.module.collect_responses.return_value = {}
    harness.module.deepseek_chat.return_value = ""
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "failed")
    assert "не удалось получить ответ" in answer.lower()
    assert proxy is True


def test_executor_answer_still_completes_legacy_consultation(harness):
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "completed")
    assert answer == "Сводный ответ"
    assert proxy is False


def test_answer_survives_synthesis_failure(harness):
    harness.module.deepseek_chat.return_value = ""
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "completed")
    assert "Проверка пройдена" in answer
    assert proxy is False


def test_partial_delivery_only_waits_for_delivered_agents(harness):
    harness.module.route_task.return_value = (["executor-a", "executor-b"], "two skills")
    harness.module.dispatch_task.side_effect = ["message-a", None]
    harness.module.run_orchestration(TASK_ID)

    assert_terminal(harness, "completed")
    assert harness.module.collect_responses.call_args.args[1:3] == (["executor-a"], {"executor-a": "message-a"})


def test_routing_exception_is_failed(harness):
    harness.module.route_task.side_effect = RuntimeError("routing unavailable")
    harness.module.run_orchestration(TASK_ID)

    answer, proxy = assert_terminal(harness, "failed")
    assert "routing unavailable" in answer


@pytest.mark.parametrize("marker", [True, "true"])
def test_coordination_tasks_are_never_started_or_mutated_by_legacy(harness, marker):
    harness.db.row["context"] = {"coordination_v1": marker}
    harness.module.run_orchestration(TASK_ID)

    assert len(harness.db.calls) == 1
    assert harness.db.calls[0][0].startswith("SELECT ")
    assert harness.db.commits == 0
    assert harness.events == []
    harness.module.route_task.assert_not_called()
    harness.module.dispatch_task.assert_not_called()


@pytest.mark.parametrize("context", [None, {}, {"coordination_v1": False}])
def test_old_rows_remain_eligible(harness, context):
    harness.db.row["context"] = context
    harness.module.run_orchestration(TASK_ID)
    assert_terminal(harness, "completed")


def make_handler(harness, path, body=None):
    handler = object.__new__(harness.module.OrchHandler)
    handler.path = path
    raw = json.dumps(body or {}).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler._write_sse = Mock()
    return handler


def test_coordination_tasks_excluded_in_history_query(harness, monkeypatch):
    monkeypatch.setattr(harness.module, "extract_user", lambda _handler: "test-owner")
    response = Mock()
    monkeypatch.setattr(harness.module, "json_response", response)
    make_handler(harness, "/orchestrator/tasks").do_GET()

    sql, params = harness.db.calls[0]
    assert "context->>'coordination_v1'" in sql
    assert "IS DISTINCT FROM 'true'" in sql  # NULL contexts must remain visible.
    assert params == ("test-owner", 20)
    assert response.call_args.args[1:] == (200, {"tasks": []})


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_coordination_detail_and_stream_are_hidden(harness, monkeypatch, suffix):
    harness.db.row["context"] = {"coordination_v1": True}
    response = Mock()
    monkeypatch.setattr(harness.module, "json_response", response)
    monkeypatch.setattr(harness.module, "subscribe", Mock(side_effect=AssertionError("must not subscribe")))
    handler = make_handler(harness, f"/orchestrator/tasks/{TASK_ID}{suffix}")
    handler.do_GET()

    assert response.call_args.args[1] == 404
    handler._write_sse.assert_not_called()
    handler.send_response.assert_not_called()


def test_coordination_parent_cannot_spawn_legacy_child(harness, monkeypatch):
    harness.db.row["context"] = {"coordination_v1": True}
    thread = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(harness.module.threading, "Thread", thread)

    assert harness.module.create_child_task(TASK_ID, "child request") is None
    assert len(harness.db.calls) == 1
    assert harness.db.commits == 0
    thread.assert_not_called()


def test_legacy_ask_rejects_reserved_coordination_marker(harness, monkeypatch):
    monkeypatch.setattr(harness.module, "extract_user", lambda _handler: "test-owner")
    response = Mock()
    monkeypatch.setattr(harness.module, "json_response", response)
    monkeypatch.setattr(harness.module.threading, "Thread", Mock(side_effect=AssertionError("must not spawn")))
    handler = make_handler(harness, "/orchestrator/ask", {"request": "work", "context": {"coordination_v1": True}})
    handler.do_POST()

    assert response.call_args.args[1] == 400
    assert harness.db.calls == []


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_sse_replay_has_actual_terminal_status(harness, status):
    harness.db.row.update(status=status, final_answer="Итог")
    handler = make_handler(harness, f"/orchestrator/tasks/{TASK_ID}/stream")
    handler.sse_stream(TASK_ID)

    event = handler._write_sse.call_args.args[0]
    assert event["type"] == "completed"
    assert event["status"] == status
    assert event["preview"] == "Итог"


def test_live_failure_event_closes_sse_stream(harness, monkeypatch):
    harness.module._complete(TASK_ID, "Недоставка", proxy=True, status="failed")
    events = queue.Queue()
    events.put(harness.events[-1])
    monkeypatch.setattr(harness.module, "subscribe", lambda _task_id: events)
    unsubscribe = Mock()
    monkeypatch.setattr(harness.module, "unsubscribe", unsubscribe)
    handler = make_handler(harness, f"/orchestrator/tasks/{TASK_ID}/stream")
    handler.sse_stream(TASK_ID)

    assert handler._write_sse.call_args.args[0]["status"] == "failed"
    unsubscribe.assert_called_once_with(TASK_ID, events)
