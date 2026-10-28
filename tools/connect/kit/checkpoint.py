"""Контрольная точка проекта в Cognitive Core (хук Claude Code: Stop + SessionEnd).

Сценарий владельца: «открываю Codex, говорю "продолжи CRM" — он знает, где остановились».
Для этого кто-то должен ЗАПИСАТЬ, где остановились. Модель это делает ненадёжно,
поэтому пишет хук: берёт последний ответ ассистента из транскрипта, ветку и коммит,
и сохраняет событие `CHECKPOINT <домен>` через cognitive_remember. Любой агент
владельца (Codex, Claude, DeepSeek) находит его через cognitive_recall (секция recent)
или через tools/cogcore_resume.py.

Не блокирует работу: любая ошибка → exit 0. Stop ограничен одной точкой в 15 минут
на сессию, SessionEnd пишет всегда. Журнал: %TEMP%\\cogcore_checkpoint.log.

Ставится setup.ps1 / setup.sh в пользовательские хуки ~/.claude/settings.json.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
API = os.environ.get("COGCORE_API", "https://mcp.me-ai.ru")
THROTTLE_S = 15 * 60
MAX_SUMMARY = 1500

# Фильтр шума: точка пишется только по содержательной сессии.
MIN_USER_TURNS = 2          # меньше двух реплик владельца — это справка, не работа
MIN_SUMMARY_CHARS = 200     # короткий ответ («Готово.», «Да.») — не точка
MIN_WORK_SIGNALS = 2        # признаков работы в тексте: правки, коммиты, тесты, план
WORK_WORDS = ("сдела", "исправ", "добав", "удал", "измен", "коммит", "commit", "ветк", "branch",
              "pr ", "pull request", "тест", "ошибк", "баг", "файл", "деплой", "выкат", "провер",
              "остал", "дальше", "следующ", "план", "запущ", "настро", "миграц", "таблиц", "endpoint",
              "функци", "скрипт", "хук", "сервис", "реализ", "пуш", "push", "merge", "мерж", "откат")
META_WORDS = ("контрольн", "checkpoint", "какая точка", "что тебе дали при старте", "skip")
# Сессия «только чтение» (отчёт, git status, просмотр) точку не пишет: пересказ состояния
# вытеснял бы рабочий итог (замечание Codex 26.10). Нужна правка файлов или git-действие.
MUTATING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
MUTATING_BASH = ("git commit", "git push", "git merge", "git rebase", "git cherry-pick", "alembic upgrade",
                 "npm i", "npm install", "pip install", "docker compose", "systemctl", "deploy")

DOMAIN_BY_PATH = [
    ("crm", "ai_crm"),
    ("aicrm", "ai_crm"),
    ("cognitive-core", "cognitive_core"),
    ("cc-wt", "cognitive_core"),
    ("память", "cognitive_core"),
    ("дежурный", "cognitive_core"),
]


def log(msg: str) -> None:
    try:
        with io.open(os.path.join(os.environ.get("TEMP", "."), "cogcore_checkpoint.log"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def domain_for(cwd: str) -> str:
    low = (cwd or "").lower().replace("\\", "/")
    for needle, dom in DOMAIN_BY_PATH:
        if needle in low:
            return dom
    tail = low.rstrip("/").rsplit("/", 1)[-1] or "misc"
    return "".join(ch if ch.isalnum() else "_" for ch in tail)[:40] or "misc"


def api_key() -> str:
    """Ключ из набора подключения (~/.cognitive-core/config.json или COGCORE_API_KEY)."""
    from cogcore_kit import load_config
    return load_config()["api_key"]


def read_session(transcript_path: str) -> dict:
    """Сводка транскрипта: реплики владельца, вызовы инструментов, последний содержательный ответ.

    Последний ответ короче MIN_SUMMARY_CHARS («Готово.») не берём: нужен предыдущий,
    в котором описана работа, иначе точка будет пустой.
    """
    info = {"user_turns": 0, "tool_uses": 0, "mutations": 0, "last": "", "last_long": ""}
    try:
        with io.open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                content = d.get("message", {}).get("content")
                if d.get("type") == "user":
                    if isinstance(content, str) and content.strip():
                        info["user_turns"] += 1
                    elif isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "text" for x in content):
                        info["user_turns"] += 1
                    continue
                if d.get("type") != "assistant" or not isinstance(content, list):
                    continue
                for x in content:
                    if not (isinstance(x, dict) and x.get("type") == "tool_use"):
                        continue
                    info["tool_uses"] += 1
                    name = str(x.get("name", ""))
                    cmd = str((x.get("input") or {}).get("command", "")).lower()
                    if name in MUTATING_TOOLS or (name in ("Bash", "PowerShell") and any(m in cmd for m in MUTATING_BASH)):
                        info["mutations"] += 1
                text = " ".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text").strip()
                if text:
                    info["last"] = text
                    if len(text) >= MIN_SUMMARY_CHARS:
                        info["last_long"] = text
    except Exception as e:
        log(f"transcript read failed: {e}")
    return info


def work_signals(text: str) -> int:
    low = text.lower()
    return sum(1 for w in WORK_WORDS if w in low)


def is_noise(info: dict) -> str:
    """Пустая строка — точка содержательная; иначе причина пропуска."""
    summary = info.get("last_long") or info.get("last") or ""
    if info.get("user_turns", 0) < MIN_USER_TURNS and info.get("tool_uses", 0) == 0:
        return f"справочная сессия ({info.get('user_turns', 0)} реплик, без инструментов)"
    if len(summary) < MIN_SUMMARY_CHARS:
        return f"ответ короткий ({len(summary)} символов)"
    low = summary.lower()
    if any(w in low for w in META_WORDS) and work_signals(summary) < MIN_WORK_SIGNALS + 1:
        return "разговор о самой памяти, не о работе"
    if work_signals(summary) < MIN_WORK_SIGNALS:
        return "в ответе нет признаков работы"
    if info.get("mutations", 0) == 0:
        return "сессия ничего не меняла (нет правок файлов и git-действий)"
    return ""


def same_as_last(summary: str) -> bool:
    """Та же точка, что уже записана с этой машины (Stop после SessionEnd и т.п.)."""
    import hashlib
    digest = hashlib.sha1(summary.encode("utf-8", "replace")).hexdigest()
    stamp = os.path.join(os.environ.get("TEMP", "."), "cogcore_checkpoint_last.sha1")
    try:
        if io.open(stamp, encoding="utf-8").read().strip() == digest:
            return True
    except OSError:
        pass
    try:
        io.open(stamp, "w", encoding="utf-8").write(digest)
    except OSError:
        pass
    return False


def git_state(cwd: str) -> tuple[str, str, int]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=5).stdout.strip()
        except Exception:
            return ""
    branch = run("branch", "--show-current")
    head = run("rev-parse", "--short", "HEAD")
    dirty = len([ln for ln in run("status", "--short").splitlines() if ln.strip()])
    return branch, head, dirty


def throttled(session_id: str, event: str) -> bool:
    if event != "Stop":
        return False
    stamp = os.path.join(os.environ.get("TEMP", "."), f"cogcore_checkpoint_{session_id}.stamp")
    try:
        if time.time() - os.path.getmtime(stamp) < THROTTLE_S:
            return True
    except OSError:
        pass
    try:
        io.open(stamp, "w").close()
    except OSError:
        pass
    return False


def remember(domain: str, task: str, result: str, lessons: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "cognitive_remember", "arguments": {"domain": domain, "task": task, "result": result, "lessons": lessons}}}
    req = urllib.request.Request(f"{API}/mcp", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"X-API-Key": api_key(), "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read())
    if out.get("result", {}).get("isError"):
        raise RuntimeError(str(out)[:200])


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    except Exception:
        payload = {}
    event = payload.get("hook_event_name") or ("SessionEnd" if "reason" in payload else "Stop")
    if payload.get("stop_hook_active"):
        return 0
    session_id = str(payload.get("session_id") or "nosession")
    cwd = payload.get("cwd") or os.getcwd()
    if throttled(session_id, event):
        return 0
    info = read_session(payload.get("transcript_path") or "")
    reason = is_noise(info)
    if reason:
        log(f"{event}: пропуск — {reason}")
        return 0
    summary = info["last_long"] or info["last"]
    if same_as_last(summary):
        log(f"{event}: пропуск — та же точка уже записана")
        return 0
    branch, head, dirty = git_state(cwd)
    domain = domain_for(cwd)
    task = f"CHECKPOINT {domain}" + (f" [{branch}@{head}]" if branch else "")
    folder = os.path.basename(cwd.rstrip("/\\"))
    result = (f"Где остановились ({event}, {time.strftime('%Y-%m-%d %H:%M')}, {folder}, "
              f"ветка {branch or '-'} {head or ''}, незакоммичено {dirty}):\n{summary[:MAX_SUMMARY]}")
    try:
        remember(domain, task, result, "")
        log(f"{event}: {task} ({len(result)} chars)")
    except Exception as e:
        log(f"{event}: remember failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
