"""Общая часть набора подключения Cognitive Core: конфиг и HTTP.

Конфиг одного ПК — ~/.cognitive-core/config.json (пишет setup.ps1 / setup.sh):
  {
    "api": "https://mcp.me-ai.ru",
    "api_key": "<агентский ключ владельца>",
    "agent_id": "dsdsd",                      # от чьего имени отвечать в комнатах
    "instance": "claude-live:<имя ПК>",       # чей это экземпляр (для заявок)
    "mentions": ["память", "dsdsd", "клод", "claude"],
    "rooms": [{"id": "...", "key": "rk_...", "name": "orchestra"}]
  }
Ключ можно не хранить в файле: переменная окружения COGCORE_API_KEY важнее.
Секретов в коде нет; файлы набора одинаковы на всех ПК.
"""
from __future__ import annotations

import io
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
KIT_DIR = os.path.join(HOME, ".cognitive-core")
CONFIG = os.path.join(KIT_DIR, "config.json")


def load_config() -> dict:
    cfg: dict = {}
    try:
        with io.open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("api", os.environ.get("COGCORE_API", "https://mcp.me-ai.ru"))
    cfg["api_key"] = os.environ.get("COGCORE_API_KEY") or cfg.get("api_key") or _key_from_claude_json()
    cfg.setdefault("agent_id", "dsdsd")
    cfg.setdefault("instance", f"claude-live:{socket.gethostname() or 'pc'}")
    cfg.setdefault("mentions", ["память", "памяти", "dsdsd", "клод", "клода", "клоду", "claude"])
    cfg.setdefault("rooms", [])
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(KIT_DIR, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def _key_from_claude_json() -> str:
    try:
        with io.open(os.path.join(HOME, ".claude.json"), encoding="utf-8") as f:
            j = json.load(f)
        return j["mcpServers"]["cognitive-core"]["headers"]["X-API-Key"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def call(cfg: dict, method: str, path: str, *, body: dict | None = None, headers: dict | None = None,
         timeout: int = 15, auth: bool = True) -> tuple[int, dict]:
    h = {"Content-Type": "application/json"}
    if auth and cfg.get("api_key"):
        h["X-API-Key"] = cfg["api_key"]
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(cfg["api"] + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def room_call(cfg: dict, room: dict, method: str, path: str, *, body: dict | None = None, timeout: int = 15):
    return call(cfg, method, f"/rooms/{urllib.parse.quote(room['id'])}{path}", body=body,
                headers={"X-Room-Key": room["key"]}, timeout=timeout, auth=False)


def mcp_tool(cfg: dict, name: str, arguments: dict, timeout: int = 20) -> tuple[bool, str]:
    st, j = call(cfg, "POST", "/mcp", body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}},
                 headers={"Accept": "application/json"}, timeout=timeout)
    res = j.get("result") or {}
    text = ((res.get("content") or [{}])[0] or {}).get("text", "")
    return st == 200 and not res.get("isError"), text
