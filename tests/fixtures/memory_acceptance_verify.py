"""Verify the disposable acceptance endpoint; uses synthetic QA credentials only."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:18766")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.endpoint.startswith("http://127.0.0.1:"):
        raise ValueError("Acceptance verifier is restricted to loopback QA")
    evidence = {}

    def request(path, key=None, body=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = key
        req = urllib.request.Request(args.endpoint + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def mcp(name, arguments, key="qa-consumer-only"):
        return request("/mcp", key, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})[1]["result"]

    result = mcp("cognitive_recall", {"query": "Где остановились с проверкой Кедра, что дальше и где подтверждение?",
                                      "domain": "cognitive_core", "include_tools": False})
    point = result["structuredContent"]["frame"]["current"][0]
    assert point["checkpoint"]["status"] == "blocked"
    record_id = point["id"]
    full = mcp("cognitive_read_record", {"record_id": record_id})["structuredContent"]
    assert full["is_latest"] and not full["truncated"]
    assert full["content"]["result"].endswith("Конечное доказательство KEDR-47.")
    assert "payload" not in full
    evidence["latest"] = full
    query = "Где остановились с проверкой НесуществующийЖуравль987, что дальше и где подтверждение?"
    frame = mcp("cognitive_recall", {"query": query, "include_tools": False})["structuredContent"]["frame"]
    assert not frame["task_state_found"] and not frame["recent"]
    evidence["unknown"] = frame
    assert request("/events/" + record_id)[0] == 401
    assert request("/events/" + record_id, "qa-outsider-only")[0] == 404
    assert mcp("cognitive_read_record", {"record_id": record_id}, "qa-outsider-only")["isError"]
    body = {"source_agent": "qa-producer", "domain": "cognitive_core", "idempotency_key": "qa-verifier-1",
            "payload": {"task": "Transport verification", "result": "This is only a QA transport receipt"}}
    first = request("/events", "qa-producer-only", body)
    second = request("/events", "qa-producer-only", body)
    assert first[0] == second[0] == 200 and first[1]["id"] == second[1]["id"]
    body["payload"]["result"] = "Conflicting body"
    assert request("/events", "qa-producer-only", body)[0] == 409
    body["payload"]["checkpoint"] = {"status": "completed"}
    assert request("/events", "qa-producer-only", body)[0] == 422
    evidence["checks"] = ["latest reopened state", "full uncut source without duplicate payload", "unknown framed query empty",
                          "anonymous 401", "other owner 404 and MCP error", "duplicate same ID", "conflict 409", "invalid checkpoint 422"]
    state = args.output.parent / "adapter-qa"
    state.mkdir(parents=True, exist_ok=True)
    config = {"endpoint": args.endpoint, "state_dir": str(state), "agents": {"qa-producer": {}},
              "projects": [{"path": "qa://kedr/workspace", "domain": "cognitive_core"}]}
    config_path = state / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    command = [sys.executable, "-X", "utf8", "tools/memory_hook.py", "--agent", "qa-producer", "--config", str(config_path)]
    env = {**os.environ, "COGCORE_API_KEY": "qa-producer-only"}
    note = {"hook_event_name": "Stop", "session_id": "qa-hook-session", "turn_id": "qa-hook-turn", "cwd": "qa://kedr/workspace",
            "last_assistant_message": "Проверена автоматическая запись ответа сессии. Это тест доставки, а не решение о готовности Кедра. " * 3}
    stopped = subprocess.run(command, input=json.dumps(note), capture_output=True, encoding="utf-8", env=env, check=True)
    assert json.loads(stopped.stdout) == {}
    started = subprocess.run(command, input=json.dumps({"hook_event_name": "SessionStart", "cwd": note["cwd"], "session_id": "qa-new-process"}),
                             capture_output=True, encoding="utf-8", env=env, check=True)
    context = json.loads(started.stdout)["hookSpecificOutput"]["additionalContext"]
    assert record_id in context and '"status": "blocked"' in context and "KEDR-47" in context
    evidence["adapter_resume"] = context
    evidence["checks"].append("Stop adapter delivery and SessionStart restore in separate processes")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": len(evidence["checks"]), "checks": evidence["checks"], "evidence": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
