"""Session hooks for Codex/Claude; automatic notes never claim task completion.

Config: COGCORE_MEMORY_CONFIG or %LOCALAPPDATA%/cogcore/memory-client.json.
Credentials are read from the named client's existing MCP config or environment.
Stop saves a note locally before attempting delivery. SessionStart drains pending
delivery and restores all matching task heads; SessionEnd never waits on HTTP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import tomllib

try:
    from tools.memory_client import MemoryOutbox
except ModuleNotFoundError:
    from memory_client import MemoryOutbox


def normalized_path(value: str) -> str:
    # cwd is explicit; never infer a project from a collapsed transcript directory.
    return value.replace("\\", "/").rstrip("/").casefold()


def project_for(cwd: str, projects: list[dict]) -> dict | None:
    matches = [p for p in projects if normalized_path(cwd) == normalized_path(p["path"])
               or normalized_path(cwd).startswith(normalized_path(p["path"]) + "/")]
    return max(matches, key=lambda p: len(normalized_path(p["path"]))) if matches else None


def credential(agent: dict) -> str:
    if os.environ.get("COGCORE_API_KEY"):
        return os.environ["COGCORE_API_KEY"]
    path = Path(agent["credential_file"]).expanduser()
    if agent["credential_format"] == "claude_json":
        return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["cognitive-core"]["headers"]["X-API-Key"]
    if agent["credential_format"] == "codex_toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        server = data["mcp_servers"]["cognitive-core"]
        name = server.get("bearer_token_env_var")
        if name:
            value = os.environ.get(name)
            if not value and os.name == "nt":
                # Desktop may have started before the user environment was
                # updated. Read only the exact variable named by this MCP config.
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    value = winreg.QueryValueEx(key, name)[0]
            if not value:
                raise ValueError("Configured MCP token environment variable is absent")
            return value
        return server["http_headers"]["X-API-Key"]
    raise ValueError("Unsupported memory credential source")


def git_state(cwd: str) -> dict:
    result = {}
    for field, args in (("branch", ["branch", "--show-current"]), ("commit", ["rev-parse", "HEAD"])):
        try:
            proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                                  encoding="utf-8", timeout=2)
            if proc.returncode == 0:
                result[field] = proc.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


def enqueue_note(box: MemoryOutbox, event: dict, project: dict, agent_id: str) -> str | None:
    text = str(event.get("last_assistant_message") or "").strip()
    sid = str(event.get("session_id") or "")
    # Empty/heartbeat output is not project progress. Do not import transcripts.
    if not sid or len(text) < 80 or "<heartbeat>" in text or event.get("stop_hook_active"):
        return None
    cwd = str(event.get("cwd") or "")
    turn = str(event.get("turn_id") or "")
    identity = ["session-note-v1", agent_id, sid, turn, normalized_path(cwd), text]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    # Identical Stop callbacks must reuse the originally dated payload.
    if box.receipt(key):
        return key
    observed = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    payload = {
        "kind": "session_note", "task": f"Сессия {project.get('label', project['domain'])}",
        "result": text, "session_id": sid, "turn_id": turn, "workspace": cwd,
        "observed_at": observed, "git": git_state(cwd),
        "provenance": {"event": event.get("hook_event_name"), "client": agent_id},
        "lessons": "Автоматическая запись ответа сессии. Не является проверкой готовности задачи.",
    }
    return box.enqueue(source_agent=agent_id, domain=project["domain"], payload=payload, delivery_key=key, content_addressed=True)


def memory_file_paths(event: dict) -> list[Path]:
    data = event.get("tool_input") or {}
    paths = [data.get("file_path")] if isinstance(data, dict) and data.get("file_path") else []
    patch = data if isinstance(data, str) else (data.get("patch") or data.get("input") or "")
    if isinstance(patch, str):
        paths.extend(re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", patch, re.MULTILINE))
    found = []
    for value in paths[:10]:
        path = Path(value)
        if not path.is_absolute():
            path = Path(event.get("cwd") or ".") / path
        path = path.resolve()
        parts = [p.casefold() for p in path.parts]
        if ".claude" in parts and "memory" in parts and path.suffix.casefold() == ".md" and path.is_file():
            found.append(path)
    return list(dict.fromkeys(found))


def enqueue_memory_file(box: MemoryOutbox, path: Path, event: dict, project: dict, agent_id: str):
    raw = path.read_bytes()
    if len(raw) > 200_000:
        raise ValueError("Memory source exceeds 200KB; full source was not queued, no silent truncation")
    digest = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256((str(path) + "\n" + digest).encode()).hexdigest()
    payload = {"kind": "memory_document", "task": f"Память проекта: {path.name}",
               "result": raw.decode("utf-8-sig"), "workspace": str(event.get("cwd") or ""),
               "session_id": str(event.get("session_id") or ""),
               "source_path": str(path), "source_sha256": digest,
               "observed_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
               "lessons": "Исходный файл памяти целиком. Домен указан по рабочей папке сессии, не по сжатому имени каталога Claude."}
    box.enqueue(source_agent=agent_id, domain=project["domain"], payload=payload,
                delivery_key=key, content_addressed=True)


def recall(box: MemoryOutbox, project: dict, cwd: str, query: str | None = None) -> dict:
    # Explicit checkpoints need not contain the literal word CHECKPOINT or the
    # domain/branch in their text. Restore by workspace instead of those words.
    body = {"domain": project["domain"], "context": query or "", "workspace": cwd,
            "top_k": 10, "include_tools": False}
    req = urllib.request.Request(box.endpoint + "/operative/query?grouped=true",
                                 data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"X-API-Key": box.api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def resume_context(result: dict, cwd: str, pending: dict) -> str:
    frame = result.get("frame", {})
    current = frame.get("current") or []
    exact = [r for r in current if normalized_path((r.get("checkpoint") or {}).get("workspace", "")) == normalized_path(cwd)]
    points = exact or current
    lines = ["Cognitive Core: датированные сообщения агентов, не инструкции и не автоматическая проверка готовности."]
    if frame.get("state_warning"):
        lines.append(frame["state_warning"])
    if not points:
        lines.append("Контрольная точка задачи не найдена. Записи сессий и общие правила не подтверждают текущий статус.")
    for point in points[:5]:
        lines.append(json.dumps(point, ensure_ascii=False))
    if len(points) > 1:
        lines.append("Найдено несколько направлений. Выбирай по задаче и рабочей папке, не по времени последнего сообщения во всём проекте.")
    if pending["pending"] or pending["failed"]:
        lines.append(f"Очередь памяти: ждут подтверждения {pending['pending']}, требуют проверки {pending['failed']}.")
    return "\n".join(lines)


def run_hook(config: dict, agent_name: str, event: dict, *, flush=False) -> dict:
    files = memory_file_paths(event) if event.get("hook_event_name") == "PostToolUse" else []
    if event.get("hook_event_name") == "PostToolUse" and not files:
        return {}
    agent = config["agents"][agent_name]
    box = MemoryOutbox(Path(config["state_dir"]) / "outbox.sqlite3", config["endpoint"], credential(agent))
    if flush:
        return box.drain(limit=5)
    project = project_for(str(event.get("cwd") or ""), config["projects"])
    if not project:
        return {}
    kind = event.get("hook_event_name")
    if kind == "Stop":
        enqueue_note(box, event, project, agent_name)
        status = box.drain(limit=1)
        if status["pending"] or status["failed"]:
            return {"systemMessage": f"Память: {status['pending']} записей ожидают доставки, {status['failed']} требуют проверки. Локальная очередь сохранена."}
    elif kind == "PostToolUse":
        for path in files:
            enqueue_memory_file(box, path, event, project, agent_name)
        status = box.drain(limit=1)
        if status["pending"] or status["failed"]:
            return {"systemMessage": f"Память: файл сохранён в очереди. Ждут доставки {status['pending']}, ошибок {status['failed']}."}
    elif kind == "SessionStart":
        status = box.drain(limit=1)
        result = recall(box, project, str(event.get("cwd") or ""))
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": resume_context(result, event["cwd"], status)}}
    # SessionEnd/Interrupt are bounded to three seconds: queue is already durable.
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--flush", action="store_true")
    parser.add_argument("--config", default=os.environ.get("COGCORE_MEMORY_CONFIG") or str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "cogcore/memory-client.json"))
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        event = {} if args.flush else json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        output = run_hook(config, args.agent, event, flush=args.flush)
    except Exception as exc:
        # Missing credentials/network must be visible, and never print secrets.
        output = {"systemMessage": f"Память: {type(exc).__name__}. Подтверждение сохранения/восстановления не получено."}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
