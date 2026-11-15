import hashlib
import json

import pytest

from tools.memory_client import MemoryOutbox
from tools.memory_hook import enqueue_note, project_for, resume_context


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_codex_apply_patch_command_queues_full_memory_source(tmp_path, monkeypatch, newline):
    from tools import memory_hook

    path = tmp_path / ".claude" / "memory" / "work.md"
    path.parent.mkdir(parents=True)
    raw = "Последнее состояние. Доказательство и следующий шаг.\n".encode()
    path.write_bytes(raw)
    event = {
        "hook_event_name": "PostToolUse", "tool_name": "apply_patch",
        "cwd": str(tmp_path), "session_id": "codex-session",
        "tool_input": {"command": newline.join([
            "*** Begin Patch", "*** Update File: .claude/memory/work.md",
            "@@", "+Последнее состояние. Доказательство и следующий шаг.", "*** End Patch",
        ])},
    }
    monkeypatch.setattr(memory_hook, "credential", lambda agent: "test-only")
    monkeypatch.setattr(MemoryOutbox, "drain", lambda self, **kwargs: self.status())
    config = {
        "state_dir": str(tmp_path / "queue"), "endpoint": "https://example.invalid",
        "agents": {"codex-desktop": {}},
        "projects": [{"path": str(tmp_path), "domain": "cognitive_core"}],
    }
    output = memory_hook.run_hook(config, "codex-desktop", event)
    assert "systemMessage" in output
    box = MemoryOutbox(tmp_path / "queue" / "outbox.sqlite3", config["endpoint"], "test-only")
    with box.connect() as db:
        rows = db.execute("SELECT body FROM deliveries").fetchall()
    assert len(rows) == 1
    body = json.loads(rows[0][0])
    assert body["domain"] == "cognitive_core"
    payload = body["payload"]
    assert payload["kind"] == "memory_document"
    assert payload["result"] == raw.decode()
    assert payload["source_path"] == str(path)
    assert payload["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["session_id"] == "codex-session"
    assert "checkpoint" not in payload


@pytest.mark.parametrize("tool_input", [True, ["not a patch"], {"file_path": 42}])
def test_memory_paths_ignore_malformed_tool_input(tool_input):
    from tools.memory_hook import memory_file_paths

    assert memory_file_paths({"tool_name": "apply_patch", "tool_input": tool_input}) == []


def test_shell_command_is_not_interpreted_as_apply_patch(tmp_path):
    from tools.memory_hook import memory_file_paths

    path = tmp_path / ".claude" / "memory" / "work.md"
    path.parent.mkdir(parents=True)
    path.write_text("Existing source", encoding="utf-8")
    event = {"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {
        "command": "*** Begin Patch\n*** Update File: .claude/memory/work.md\n*** End Patch",
    }}
    assert memory_file_paths(event) == []


def test_memory_file_full_body_and_provenance(tmp_path):
    import json

    from tools.memory_hook import enqueue_memory_file, memory_file_paths
    path = tmp_path / ".claude" / "projects" / "D--------" / "memory" / "work.md"
    path.parent.mkdir(parents=True)
    body = "Факты и доказательства. " * 500 + "Конечный следующий шаг"
    path.write_text(body, encoding="utf-8")
    event = {"hook_event_name": "PostToolUse", "cwd": str(tmp_path), "session_id": "s", "tool_input": {"file_path": str(path)}}
    assert memory_file_paths(event) == [path]
    box = MemoryOutbox(tmp_path / "q.db", "https://example.invalid", "test-only")
    enqueue_memory_file(box, path, event, {"domain": "cognitive_core"}, "producer")
    enqueue_memory_file(box, path, event, {"domain": "cognitive_core"}, "producer")
    assert box.status()["pending"] == 1
    with box.connect() as db:
        payload = json.loads(db.execute("SELECT body FROM deliveries").fetchone()[0])["payload"]
    assert payload["result"] == body
    assert payload["source_path"] == str(path) and payload["workspace"] == str(tmp_path)
    assert "checkpoint" not in payload


def test_explicit_cwd_resolves_collapsed_cyrillic_projects():
    projects = [{"path": "D:/ИИ/Принтер", "domain": "printer"}, {"path": "D:/ИИ/Квадрик", "domain": "uav"}]
    assert project_for("D:\\ИИ\\Принтер\\models", projects)["domain"] == "printer"
    assert project_for("D:/ИИ/Квадрик", projects)["domain"] == "uav"
    assert project_for("D:/ИИ/ПринтерДругой", projects) is None
    assert project_for("D------------", projects) is None


def test_stop_is_durable_idempotent_session_note_not_completed(tmp_path):
    box = MemoryOutbox(tmp_path / "queue.db", "https://example.invalid", "fake-test-key")
    event = {"session_id": "session-a", "turn_id": "turn-a", "cwd": str(tmp_path), "hook_event_name": "Stop",
             "last_assistant_message": "Готово. Сервис работает, запись доставлена. " * 4}
    project = {"domain": "cognitive_core"}
    first = enqueue_note(box, event, project, "codex-desktop")
    assert enqueue_note(box, event, project, "codex-desktop") == first
    assert box.status()["pending"] == 1
    with box.connect() as db:
        import json
        payload = json.loads(db.execute("SELECT body FROM deliveries").fetchone()[0])["payload"]
    assert payload["kind"] == "session_note"
    assert "checkpoint" not in payload and "status" not in payload
    assert enqueue_note(box, {**event, "session_id": "session-b"}, project, "codex-desktop") != first
    assert enqueue_note(box, {**event, "last_assistant_message": "<heartbeat>unchanged</heartbeat>" * 4}, project, "codex-desktop") is None


def test_resume_does_not_pick_single_global_latest_or_invent_state():
    empty = resume_context({"frame": {"recent": [{"content": {"result": "done"}}]}}, "D:/work", {"pending": 1, "failed": 0})
    assert "не найдена" in empty and "ждут подтверждения 1" in empty
    multiple = resume_context({"frame": {"current": [{"id": "task-a"}, {"id": "task-b"}]}}, "D:/work", {"pending": 0, "failed": 0})
    assert "task-a" in multiple and "task-b" in multiple and "несколько направлений" in multiple
