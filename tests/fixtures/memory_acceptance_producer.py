"""Disposable two-process producer to exercise offline queue and lost ack."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tools.memory_client import MemoryOutbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["offline", "recover", "lost-ack", "drain"])
    parser.add_argument("--state", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18766")
    args = parser.parse_args()
    directory = Path(args.state)
    directory.mkdir(parents=True, exist_ok=True)
    box = MemoryOutbox(directory / "producer.sqlite3", args.endpoint, "qa-producer-only")
    statefile = directory / "producer.json"
    if args.phase == "offline":
        payload = {"task": "Проверка Кедра", "result": "Проверка Кедра завершена по первым измерениям.", "checkpoint": {
            "task_id": "kedr-acceptance", "session_id": "producer-session-a", "status": "completed",
            "next_step": "Передать результат на независимую проверку",
            "evidence": ["qa://kedr/initial-receipt.json"],
            "observed_at": datetime.now(timezone.utc).isoformat(), "workspace": "qa://kedr/workspace",
        }}
        box.enqueue(source_agent="qa-producer", domain="cognitive_core", payload=payload, delivery_key="kedr-initial")
        def offline(_):
            raise TimeoutError("simulated network outage")
        box._send = offline
        result = box.drain(force=True)
        assert result["pending"] == 1
        statefile.write_text(json.dumps({"initial": payload}, ensure_ascii=False), encoding="utf-8")
    elif args.phase == "recover":
        result = box.drain(force=True)
        assert box.receipt("kedr-initial")["status"] == "acked", result
    elif args.phase == "lost-ack":
        old = box.receipt("kedr-initial")["record_id"]
        payload = {"task": "Проверка Кедра", "result": "Повторная проверка обнаружила блокировку. Готовность отозвана. " * 180 + "Конечное доказательство KEDR-47.",
                   "checkpoint": {"task_id": "kedr-acceptance", "session_id": "producer-session-b", "status": "blocked",
                                  "next_step": "Исправить обработку Enter, затем повторить проверку KEDR-47 в браузере",
                                  "evidence": ["qa://kedr/evidence/KEDR-47.json"],
                                  "observed_at": datetime.now(timezone.utc).isoformat(),
                                  "workspace": "qa://kedr/workspace", "supersedes": old}}
        box.enqueue(source_agent="qa-producer", domain="cognitive_core", payload=payload, delivery_key="kedr-reopened")
        send = box._send
        def lost_ack(body):
            send(body)
            raise TimeoutError("simulated loss after successful commit")
        box._send = lost_ack
        result = box.drain(force=True)
        assert box.receipt("kedr-reopened")["status"] == "pending", result
    else:
        result = box.drain(force=True)
        assert box.receipt("kedr-reopened")["status"] == "acked", result
    result["phase"] = args.phase
    result["initial_receipt"] = box.receipt("kedr-initial")
    result["reopened_receipt"] = box.receipt("kedr-reopened")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
