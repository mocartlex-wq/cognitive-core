"""Reviewable, reversible installation of the memory adapter. Default is plan only.

Requires Python 3.11+. Does not store credentials or bypass Codex hook trust.
Existing unrelated hooks, permissions and notify commands are preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from tools.memory_hook import credential
except ModuleNotFoundError:
    from memory_hook import credential

MANAGED = ("memory_hook.py", "cogcore_checkpoint.py", "cogcore_resume.py", "cogcore_memory_sync.py")


def hook_command(python: str, script: Path, config: Path, agent: str) -> str:
    # Claude's Windows hooks can run through Git Bash. Unquoted backslashes
    # lose their separators there, even though the same argv works in cmd.exe.
    paths = [str(p).replace("\\", "/") for p in (python, script, config)]
    if any(any(char in value for char in '$`\r\n') for value in [*paths, agent]):
        raise ValueError("Hook paths must not contain shell expansion characters")
    return subprocess.list2cmdline([paths[0], "-X", "utf8", paths[1], "--config", paths[2], "--agent", agent])


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def merge_hooks(original: dict, command: str | None) -> dict:
    data = json.loads(json.dumps(original))
    hooks = data.setdefault("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            group["hooks"] = [h for h in group.get("hooks", [])
                              if not any(name in h.get("command", "") for name in MANAGED)]
            if group["hooks"]:
                kept.append(group)
        hooks[event] = kept
    if command:
        for event, timeout in (("SessionStart", 30), ("Stop", 20), ("PostToolUse", 20)):
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
    return data


def check_server(config: dict) -> None:
    # Read-only capability gate BEFORE changing any local config or files.
    for agent in config["agents"].values():
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        req = urllib.request.Request(config["endpoint"].rstrip("/") + "/mcp",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-API-Key": credential(agent)})
        with urllib.request.urlopen(req, timeout=10) as response:
            tools = json.load(response).get("result", {}).get("tools", [])
        names = {t["name"]: t for t in tools}
        remember = names.get("cognitive_remember", {}).get("inputSchema", {}).get("properties", {})
        if "cognitive_read_record" not in names or not {"checkpoint", "idempotency_key"} <= remember.keys():
            raise RuntimeError("Server memory reliability release is not active; local settings were not changed")


def atomic_json(path: Path, data: dict, stamp: str) -> str | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = str(path) + f".memory-backup-{stamp}"
        shutil.copy2(path, backup)
    temporary = path.with_name(path.name + f".memory-{stamp}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Runtime JSON without embedded credentials")
    parser.add_argument("--python", required=True, help="Stable Python 3.11+ executable")
    parser.add_argument("--destination", type=Path, required=True, help="Stable adapter directory")
    parser.add_argument("--codex-settings", type=Path, default=Path.home() / ".codex/hooks.json")
    parser.add_argument("--claude-settings", type=Path, default=Path.home() / ".claude/settings.json")
    parser.add_argument("--legacy-settings", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = read_json(args.config)
    for name in ("codex-desktop", "claude-code"):
        if name not in config.get("agents", {}):
            raise ValueError(f"Missing configured client: {name}")
    subprocess.run([args.python, "-c", "import sys; assert sys.version_info >= (3,11)"], check=True)
    script = args.destination.resolve() / "memory_hook.py"
    plans = []
    for path, name in ((args.codex_settings, "codex-desktop"), (args.claude_settings, "claude-code")):
        command = hook_command(args.python, script, args.config.resolve(), name)
        plans.append((path, merge_hooks(read_json(path), command)))
    for path in args.legacy_settings:
        if path.exists() and path not in (args.codex_settings, args.claude_settings):
            original = read_json(path)
            merged = merge_hooks(original, None)
            if merged != original:
                plans.append((path, merged))
    if not args.apply:
        print(json.dumps({"mode": "plan", "settings": [{"path": str(p), "hooks": d.get("hooks")} for p, d in plans],
                          "copy_to": str(args.destination), "codex_trust": "Review the new definitions in Settings > Hooks after installation"}, ensure_ascii=False, indent=2))
        return 0
    check_server(config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    args.destination.mkdir(parents=True, exist_ok=True)
    backups = []
    for name in ("memory_client.py", "memory_hook.py", "memory_flush.py"):
        source, target = Path(__file__).parent / name, args.destination / name
        if source.resolve() != target.resolve():
            if target.exists():
                backup = str(target) + f".memory-backup-{stamp}"
                shutil.copy2(target, backup)
                backups.append(backup)
            shutil.copy2(source, target)
    for path, data in plans:
        backup = atomic_json(path, data, stamp)
        if backup:
            backups.append(backup)
    receipt = {"installed_at": stamp, "settings": [str(p) for p, _ in plans], "backups": backups,
               "codex_trust_pending": True, "retry_task": "Register using install_memory_retry.ps1"}
    receipt_path = Path(config["state_dir"]) / f"installation-{stamp}.json"
    atomic_json(receipt_path, receipt, stamp)
    print(json.dumps({"receipt": str(receipt_path), **receipt}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
