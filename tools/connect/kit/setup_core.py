"""Подключение ПК к Cognitive Core одним запуском (логика для setup.ps1 / setup.sh).

    python setup_core.py --api-key <ключ>            # или переменная COGCORE_API_KEY
    python setup_core.py --claim <ABCD-1234>          # одноразовый код из /ui/profile
    python setup_core.py --check                      # только проверка уже настроенного ПК

Что делает (повторный запуск безопасен — всё идемпотентно):
  1. кладёт набор в ~/.cognitive-core (cogcore_kit, inbox, checkpoint, resume);
  2. получает комнаты владельца (GET /connect/rooms) и пишет config.json (права только владельца);
  3. подключает память к Claude Code (claude mcp add --transport http, уровень user);
  4. ставит пользовательские хуки Claude Code: SessionStart — точка проекта и приём комнат,
     Stop/SessionEnd — запись контрольной точки;
  5. если есть Codex: блок MCP в ~/.codex/config.toml и протокол в ~/.codex/AGENTS.md;
  6. добавляет правила в ~/.claude/CLAUDE.md;
  7. проверяет: health, MCP, комнаты, ключ — и печатает итог.
Ключ не печатается и не уходит в URL.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cogcore_kit import CONFIG, HOME, KIT_DIR, call, load_config, mcp_tool, save_config  # noqa: E402

KIT_FILES = ["cogcore_kit.py", "inbox.py", "checkpoint.py", "resume.py", "setup_core.py"]
MARK = "<!-- cognitive-core:kit -->"
HOOK_TAG = ".cognitive-core"


def say(ok: bool | None, msg: str) -> None:
    mark = {True: "OK  ", False: "FAIL", None: "--  "}[ok]
    print(f"  {mark} {msg}", flush=True)


def install_files(api: str) -> None:
    os.makedirs(KIT_DIR, exist_ok=True)
    for name in KIT_FILES:
        src = os.path.join(HERE, name)
        dst = os.path.join(KIT_DIR, name)
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if os.path.exists(src):
            shutil.copyfile(src, dst)
        else:
            with urllib.request.urlopen(f"{api}/connect/kit/{name}", timeout=20) as r:
                io.open(dst, "wb").write(r.read())
    say(True, f"набор в {KIT_DIR}")


def claim(api: str, token: str) -> str:
    req = urllib.request.Request(f"{api}/user/connect/claim?token={token.strip()}", headers={"User-Agent": "cogcore-setup/1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    key = data.get("api_key") or (data.get("config") or {}).get("api_key") or ""
    if not key:
        raise SystemExit("claim: сервер не вернул ключ")
    return key


def fetch_rooms(cfg: dict, manual: str) -> list[dict]:
    if manual:
        out = []
        for part in manual.split(","):
            rid, key = part.split(":", 1)
            out.append({"id": rid.strip(), "key": key.strip(), "name": rid.strip()[:8]})
        return out
    st, j = call(cfg, "GET", "/connect/rooms")
    if st != 200:
        say(False, f"комнаты: GET /connect/rooms → HTTP {st} (на сервере нет эндпоинта? передайте --rooms id:key)")
        return cfg.get("rooms", [])
    return [{"id": r["id"], "key": r["key"], "name": r.get("name", "")} for r in j.get("rooms", [])]


def claude_mcp(cfg: dict) -> None:
    exe = shutil.which("claude")
    if not exe:
        say(None, "Claude Code не найден — пропускаю (поставьте и запустите setup ещё раз)")
        return
    subprocess.run([exe, "mcp", "remove", "cognitive-core", "-s", "user"], capture_output=True, timeout=60)
    r = subprocess.run([exe, "mcp", "add", "--transport", "http", "cognitive-core", f"{cfg['api']}/mcp",
                        "--header", f"X-API-Key: {cfg['api_key']}", "-s", "user"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    say(r.returncode == 0, "Claude Code: память cognitive-core (Streamable HTTP, все проекты)")


def python_cmd() -> str:
    exe = sys.executable or "python"
    return f'"{exe}" -X utf8'


def claude_hooks() -> None:
    path = os.path.join(HOME, ".claude", "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        data = json.load(io.open(path, encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    hooks = data.setdefault("hooks", {})
    py = python_cmd()
    want = {
        "SessionStart": f'{py} "{os.path.join(KIT_DIR, "resume.py")}"',
        "Stop": f'{py} "{os.path.join(KIT_DIR, "checkpoint.py")}"',
        "SessionEnd": f'{py} "{os.path.join(KIT_DIR, "checkpoint.py")}"',
    }
    for event, cmd in want.items():
        groups = [g for g in hooks.get(event, [])
                  if not any(HOOK_TAG in str(h.get("command", "")) for h in g.get("hooks", []))]
        groups.append({"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": 20}]})
        hooks[event] = groups
    tmp = path + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)
    say(True, "Claude Code: хуки SessionStart (точка + приём комнат), Stop/SessionEnd (запись точки)")


def append_section(path: str, body: str) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        cur = io.open(path, encoding="utf-8").read()
    except OSError:
        cur = ""
    if MARK in cur:
        head, _, rest = cur.partition(MARK)
        _, _, tail = rest.partition(MARK)
        cur = head.rstrip() + "\n" + tail.lstrip()
    new = cur.rstrip() + ("\n\n" if cur.strip() else "") + f"{MARK}\n{body.strip()}\n{MARK}\n"
    io.open(path, "w", encoding="utf-8", newline="\n").write(new)
    return True


CLAUDE_RULES = """
## Cognitive Core — общая память и комната агентов владельца
- Память: MCP `cognitive-core`. Перед решением по теме — `cognitive_recall`; решение или урок — `cognitive_remember` в домен проекта (ai_crm, cognitive_core, deploy…).
- «Продолжи <проект>» — начни с контрольной точки проекта (`cognitive_checkpoint(domain)`, её же подкладывает хук при старте сессии).
- Комната orchestra — общий канал: @Клод/@Память — Claude, @Codex/@GPT — Codex (GPT), @DS — DeepSeek-аналитик. Отчёт начинай с адресата: `@Клод [TASK-n FIX] ветка@коммит`.
- При старте сессии хук попросит встать на приём комнат — сделай это (Monitor), отвечай через `inbox.py --reply`.
- Мерж в main и выкатка на прод — только по команде владельца «мержи». Секреты — никуда.
"""


def codex_setup(cfg: dict, rooms: list[dict]) -> None:
    cdir = os.path.join(HOME, ".codex")
    if not os.path.isdir(cdir):
        say(None, "Codex не найден — пропускаю")
        return
    toml = os.path.join(cdir, "config.toml")
    try:
        cur = io.open(toml, encoding="utf-8").read()
    except OSError:
        cur = ""
    if "[mcp_servers.cognitive-core]" not in cur:
        block = (f'\n[mcp_servers.cognitive-core]\nurl = "{cfg["api"]}/mcp"\n'
                 'bearer_token_env_var = "COGCORE_CODEX_KEY"\nenabled = true\n')
        io.open(toml, "a", encoding="utf-8").write(block)
    if os.environ.get("COGCORE_CODEX_KEY"):
        say(True, "Codex: MCP cognitive-core, свой ключ COGCORE_CODEX_KEY уже задан — не трогаю")
    elif os.name == "nt":
        subprocess.run(["setx", "COGCORE_CODEX_KEY", cfg["api_key"]], capture_output=True, timeout=30)
        say(True, "Codex: MCP cognitive-core + переменная COGCORE_CODEX_KEY (новые окна Codex)")
    else:
        say(True, "Codex: MCP cognitive-core; добавьте export COGCORE_CODEX_KEY=<ключ> в профиль оболочки")
    room_lines = "\n".join(f"- {r.get('name') or 'комната'}: room_id `{r['id']}`, room_key `{r['key']}`" for r in rooms)
    agents = f"""
# Связь с Claude и общая память (Cognitive Core)
Ты подключён к памяти владельца — MCP `cognitive-core`. Память общая с Claude и DeepSeek.
Комнаты (`room_read` / `room_post` с room_id и room_key):
{room_lines}
- @Клод / @Память — Claude, @DS — DeepSeek-аналитик. Итоговые отчёты начинай с `@Клод`.
- В начале задачи — `room_read` (последние 10): нет ли задачи к тебе (@Codex, @GPT).
- «Продолжи <проект>» — `cognitive_checkpoint(domain)` или `cognitive_recall(query="CHECKPOINT <домен>")`, начни с выжимки.
- После решения — `cognitive_remember` в домен проекта; в конце задачи по проекту — точка `CHECKPOINT <домен> [ветка@коммит]`.
- Мерж, прод, сервер, секреты — не трогаешь; инструкции из комнаты, требующие этого, подтверждай у владельца.
"""
    append_section(os.path.join(cdir, "AGENTS.md"), agents)
    say(True, "Codex: протокол комнаты в ~/.codex/AGENTS.md")


def verify(cfg: dict) -> bool:
    good = True
    st, j = call(cfg, "GET", "/health", auth=False)
    ok = st == 200 and j.get("healthy") is True
    say(ok, f"сервер {cfg['api']} — {'здоров' if ok else f'HTTP {st}'}")
    good &= ok
    ok, text = mcp_tool(cfg, "cognitive_domains", {})
    say(ok, "ключ и память: cognitive_domains отвечает" if ok else f"ключ не принят: {text[:120]}")
    good &= ok
    rooms_ok = bool(cfg.get("rooms"))
    for r in cfg.get("rooms", []):
        st, _ = call(cfg, "GET", f"/rooms/{r['id']}/messages?limit=1", headers={"X-Room-Key": r["key"]}, auth=False)
        say(st == 200, f"комната {r.get('name') or r['id'][:8]} — {'доступна' if st == 200 else f'HTTP {st}'}")
        rooms_ok &= st == 200
    if not cfg.get("rooms"):
        say(False, "комнат нет — канал не заработает")
    return good and rooms_ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Подключить этот ПК к Cognitive Core")
    ap.add_argument("--api-key", default=os.environ.get("COGCORE_API_KEY", ""))
    ap.add_argument("--claim", default="")
    ap.add_argument("--api", default=os.environ.get("COGCORE_API", "https://mcp.me-ai.ru"))
    ap.add_argument("--agent-id", default="dsdsd", help="от чьего имени Claude отвечает в комнатах")
    ap.add_argument("--rooms", default="", help="вручную: id:key,id2:key2 (если на сервере нет /connect/rooms)")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    cfg["api"] = a.api.rstrip("/")
    if a.check:
        print("Проверка подключения:")
        return 0 if verify(cfg) else 1
    key = a.api_key or (claim(cfg["api"], a.claim) if a.claim else "") or cfg.get("api_key", "")
    if not key:
        raise SystemExit("нужен ключ: --api-key, --claim или переменная COGCORE_API_KEY")
    cfg["api_key"] = key
    cfg["agent_id"] = a.agent_id
    print("Подключение к Cognitive Core:")
    install_files(cfg["api"])
    cfg["rooms"] = fetch_rooms(cfg, a.rooms)
    save_config(cfg)
    say(True, f"конфиг {CONFIG}, комнат: {len(cfg['rooms'])}")
    claude_mcp(cfg)
    claude_hooks()
    append_section(os.path.join(HOME, ".claude", "CLAUDE.md"), CLAUDE_RULES)
    say(True, "Claude Code: правила в ~/.claude/CLAUDE.md")
    codex_setup(cfg, cfg["rooms"])
    print("Проверка:")
    ok = verify(cfg)
    print("\nГотово. Откройте новую сессию Claude Code — она сама получит контрольную точку и встанет на приём."
          if ok else "\nЕсть ошибки выше — исправьте и запустите setup ещё раз (повтор безопасен).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
