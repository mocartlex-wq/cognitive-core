"""Набор подключения ПК (tools/connect/kit): адресация, догон, идемпотентность установщика.

Сценарий владельца 28.10.2026: «с любого ПК присоединиться к памяти, и всё работало».
Набор не должен ни дублировать хуки и правила при повторном запуске, ни отвечать
на чужие и свои сообщения, ни терять обращения, пришедшие, пока ПК был выключен.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "tools" / "connect" / "kit"


@pytest.fixture
def kit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("COGCORE_API_KEY", raising=False)
    monkeypatch.delenv("COGCORE_CODEX_KEY", raising=False)
    monkeypatch.syspath_prepend(str(KIT))
    for name in ("cogcore_kit", "inbox", "setup_core", "checkpoint", "resume"):
        sys.modules.pop(name, None)
    mods = {n: importlib.import_module(n) for n in ("cogcore_kit", "inbox", "setup_core")}
    return mods, tmp_path


CFG = {"agent_id": "dsdsd", "mentions": ["память", "клод", "claude"], "owner_names": ["owner"]}


def test_addressing_rules(kit):
    mods, _ = kit
    ib = mods["inbox"]
    mre = ib.mention_re(CFG)
    yes = [{"from_agent": "codex-desktop", "text": "@Клод WAKE-4: поздоровайся"},
           {"from_agent": "codex-desktop", "text": "@Клоду передай"},
           {"from_agent": "deepseek", "text": "вывод для @Память"},
           {"from_agent": "owner", "text": "что там с кадастром?"}]
    no = [{"from_agent": "dsdsd", "text": "@Клод это я сам"},
          {"from_agent": "codex-desktop", "text": "итог без адресата"},
          {"from_agent": "owner", "text": "@Codex сделай"},
          {"from_agent": "x-proxy", "text": "@Клод"},
          {"from_agent": "owner", "text": "probe: канал"}]
    assert all(ib.addressed(CFG, mre, m) for m in yes)
    assert not any(ib.addressed(CFG, mre, m) for m in no)


def test_catchup_delivers_only_unanswered_recent(kit, monkeypatch, capsys):
    mods, tmp = kit
    ib = mods["inbox"]
    now = time.time()
    iso = lambda s: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - s))  # noqa: E731
    room = {"id": "r1", "key": "rk", "name": "orchestra"}
    msgs = [{"id": "a", "from_agent": "codex-desktop", "text": "@Клод старое", "created_at": iso(20 * 3600)},
            {"id": "b", "from_agent": "codex-desktop", "text": "@Клод отвечено", "created_at": iso(3600)},
            {"id": "c", "from_agent": "dsdsd", "text": "ответ", "created_at": iso(3500)},
            {"id": "d", "from_agent": "codex-desktop", "text": "@Клод пропущено", "created_at": iso(600)}]
    cfg = {**CFG, "api": "x", "api_key": "k", "instance": "t", "rooms": [room]}
    monkeypatch.setattr(ib, "load_config", lambda: cfg)
    monkeypatch.setattr(ib, "room_call", lambda c, r, m, p, body=None, timeout=15: (200, {"messages": msgs}) if "messages" in p else (200, {}))
    locked = []
    monkeypatch.setattr(ib, "lock", lambda c, res, ttl: (locked.append(res) or True, ""))
    monkeypatch.setattr(ib.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(sys, "argv", ["inbox.py"])
    monkeypatch.setattr(ib, "LOG", str(tmp / "inbox.log"))
    with pytest.raises(SystemExit):
        ib.main()
    out = capsys.readouterr().out
    assert "msg d" in out and "msg a" not in out and "msg b" not in out
    assert "reply:r1:d" in locked and "догнал 1" in out


def test_setup_hooks_and_rules_are_idempotent(kit):
    mods, tmp = kit
    sc = mods["setup_core"]
    settings = tmp / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.py"}]}]}}), encoding="utf-8")
    sc.claude_hooks()
    sc.claude_hooks()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "guard.py", "чужие хуки не тронуты"
    for ev in ("SessionStart", "Stop", "SessionEnd"):
        assert len(data["hooks"][ev]) == 1, f"{ev}: повтор не дублирует"
    rules = tmp / ".claude" / "CLAUDE.md"
    rules.write_text("# мои правила\n", encoding="utf-8")
    sc.append_section(str(rules), "секция v1")
    sc.append_section(str(rules), "секция v2")
    text = rules.read_text(encoding="utf-8")
    assert text.startswith("# мои правила") and text.count(sc.MARK) == 2 and "v2" in text and "v1" not in text


def test_codex_key_is_never_overwritten(kit, monkeypatch):
    mods, tmp = kit
    sc = mods["setup_core"]
    (tmp / ".codex").mkdir()
    monkeypatch.setenv("COGCORE_CODEX_KEY", "own-key")
    calls = []
    monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: calls.append(a))
    sc.codex_setup({"api": "https://x", "api_key": "new"}, [{"id": "r1", "key": "rk", "name": "orchestra"}])
    assert not calls, "setx не вызывается, если свой ключ Codex уже есть"
    agents = io.open(tmp / ".codex" / "AGENTS.md", encoding="utf-8").read()
    assert "r1" in agents and "@Клод" in agents
    toml = io.open(tmp / ".codex" / "config.toml", encoding="utf-8").read()
    sc.codex_setup({"api": "https://x", "api_key": "new"}, [])
    assert io.open(tmp / ".codex" / "config.toml", encoding="utf-8").read() == toml, "блок MCP не дублируется"


def test_config_file_is_private_and_key_env_wins(kit, monkeypatch):
    mods, tmp = kit
    ck = mods["cogcore_kit"]
    ck.save_config({"api": "https://x", "api_key": "file-key", "rooms": []})
    assert ck.load_config()["api_key"] == "file-key"
    monkeypatch.setenv("COGCORE_API_KEY", "env-key")
    assert ck.load_config()["api_key"] == "env-key"
    if os.name != "nt":
        assert oct(os.stat(ck.CONFIG).st_mode & 0o777) == "0o600"
