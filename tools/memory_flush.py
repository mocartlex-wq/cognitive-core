"""Bounded retry worker for the existing local outbox; no new memory is created."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from tools.memory_hook import run_hook
except ModuleNotFoundError:
    from memory_hook import run_hook


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    result = {}
    for name in config["agents"]:
        try:
            result[name] = run_hook(config, name, {}, flush=True)
        except Exception as exc:
            result[name] = {"error_type": type(exc).__name__}
    path = Path(config["state_dir"]) / "delivery-health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return int(any(v.get("error_type") or v.get("failed") for v in result.values()))


if __name__ == "__main__":
    raise SystemExit(main())
