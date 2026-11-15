from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tools.memory_client import MemoryOutbox


def queue(tmp_path, key="one"):
    box = MemoryOutbox(tmp_path / "queue.sqlite3", "http://127.0.0.1:8761", key)
    box.enqueue(source_agent="producer", domain="cognitive_core",
                payload={"task": "Проверка записи", "result": "доказательство сохранено"}, delivery_key="first")
    return box


def test_failed_delivery_survives_fresh_process(tmp_path, monkeypatch):
    box = queue(tmp_path)
    def fail(_):
        raise TimeoutError("network unavailable")
    monkeypatch.setattr(box, "_send", fail)
    assert box.drain()["pending"] == 1
    code = "from tools.memory_client import MemoryOutbox; import json,sys; print(json.dumps(MemoryOutbox(sys.argv[1], 'http://127.0.0.1:8761', 'one').status()))"
    result = subprocess.run([sys.executable, "-c", code, str(box.path)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["pending"] == 1
    restarted = MemoryOutbox(box.path, box.endpoint, "one")
    rid = str(uuid4())
    monkeypatch.setattr(restarted, "_send", lambda _: rid)
    assert restarted.drain(force=True)["acked"] == 1
    assert restarted.receipt("first")["record_id"] == rid
    assert restarted.drain()["acked"] == 1


def test_queue_does_not_cross_account_scope_and_rejects_changed_payload(tmp_path):
    box = queue(tmp_path)
    assert MemoryOutbox(box.path, box.endpoint, "another-account").status()["pending"] == 0
    assert MemoryOutbox(box.path, "https://different.example", "one").status()["pending"] == 0
    with pytest.raises(ValueError, match="different content"):
        box.enqueue(source_agent="producer", domain="cognitive_core", payload={"task": "changed"}, delivery_key="first")
    assert box.status()["pending"] == 1
