#!/usr/bin/env python3
"""Cognitive Core — minimal multi-agent orchestrator (C2 MVP).

Цель → DeepSeek декомпозирует на <=3 подзадачи по ролям → каждую роль отыгрывает
DeepSeek-персона → DeepSeek синтезирует итог → всё постится в комнату проекта.
Координационный субстрат — rooms REST (:9098). Роли пока DeepSeek-персоны; C3
заменит их реальными подключёнными агентами через room_ask (тот же протокол).

Спека: memory/orchestration_spec_2026-05-29.md (DeepSeek-reviewed). Соответствие:
  • <=3 подзадачи, без рекурсии;
  • синтез ВКЛЮЧАЕТ исходную цель + все Q/A (не плоский);
  • DeepSeek-сбой не роняет процесс (постит ошибку, идёт дальше);
  • идемпотентность в --watch через файл seen-id;
  • персона-ответ синхронный (long-poll cascade исключён — реальных агентов в MVP нет).

Usage:
  one-shot (тест):  python cognitive-orchestrator.py --room <id> --key <key> --goal "сделай X"
  из комнаты:       python cognitive-orchestrator.py --room <id> --key <key>   # берёт @orchestrator пост
  daemon:           python cognitive-orchestrator.py --room <id> --watch 20    # key из env ORCH_ROOM_KEY
"""
import argparse
import json
import os
import sys
import time
from urllib import request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from delegate_deepseek import call_deepseek  # noqa: E402  (читает DEEPSEEK_API_KEY из .env)

ROOMS_BASE = os.environ.get("ROOMS_BASE_LOCAL", "http://127.0.0.1:9098")
MAX_SUBTASKS = 3
AGENT_ID = "orchestrator"
SEEN_PATH = os.environ.get("ORCH_SEEN_PATH", os.path.expanduser("~/.cognitive-orchestrator-seen.json"))


def rooms_req(method, path, key, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(ROOMS_BASE + path, data=data, method=method, headers={
        "X-Room-Key": key, "X-Agent-Id": AGENT_ID, "Content-Type": "application/json",
    })
    with request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(room, key, text):
    try:
        rooms_req("POST", f"/rooms/{room}/post", key, {"text": text, "from_agent": AGENT_ID})
    except Exception as e:
        print(f"post failed: {e}", file=sys.stderr)


def decompose(goal):
    r = call_deepseek(
        system=("Ты — оркестратор команды ИИ-агентов. Разбей цель на 2-3 подзадачи, каждой "
                "назначь роль (designer / developer / content / analyst). Верни СТРОГО JSON: "
                '{"subtasks":[{"role":"...","task":"..."}]}. Максимум 3 подзадачи.'),
        user=goal, temperature=0.2, json_mode=True,
    )
    if not r.get("ok"):
        return None, r.get("error")
    subs = (r.get("data") or {}).get("subtasks", [])
    return (subs[:MAX_SUBTASKS] if isinstance(subs, list) else []), None


def role_answer(role, task, goal):
    r = call_deepseek(
        system=(f"Ты — роль «{role}» в команде ИИ-агентов под общей целью. Выполни свою "
                "подзадачу кратко и по делу (до 8 строк), без воды."),
        user=f"Общая цель: {goal}\nТвоя подзадача: {task}",
        temperature=0.4, json_mode=False,
    )
    return r.get("data") if r.get("ok") else f"[DeepSeek error: {r.get('error')}]"


def synthesize(goal, qa):
    block = "\n\n".join(f"[{role}] {task}\n-> {ans}" for role, task, ans in qa)
    r = call_deepseek(
        system=("Собери ответы ролей в единый связный результат по ИСХОДНОЙ цели. "
                "Кратко, структурно, без повторов."),
        user=f"Исходная цель: {goal}\n\nОтветы ролей:\n{block}",
        temperature=0.3, json_mode=False,
    )
    return r.get("data") if r.get("ok") else f"[synthesis error: {r.get('error')}]"


def latest_goal(room, key):
    msgs = rooms_req("GET", f"/rooms/{room}/messages?since_seconds=86400&limit=50", key).get("messages", [])
    for m in reversed(msgs):
        text = (m.get("text") or "")
        if "@orchestrator" in text.lower() and m.get("from_agent") != AGENT_ID:
            return text.lower().replace("@orchestrator", "").strip() or text.strip(), m.get("id")
    return None, None


def run(room, key, goal):
    print(f"goal: {goal[:120]}")
    post(room, key, f"🧩 orchestrator принял цель: «{goal[:200]}». Декомпозирую…")
    subs, err = decompose(goal)
    if not subs:
        post(room, key, f"⚠️ orchestrator: декомпозиция не удалась (DeepSeek: {err}).")
        return 1
    post(room, key, "🧩 Подзадачи:\n" + "\n".join(
        f"  {i+1}. [{s.get('role','agent')}] {s.get('task','')}" for i, s in enumerate(subs)))
    qa = []
    for s in subs:
        role, task = s.get("role", "agent"), s.get("task", "")
        ans = role_answer(role, task, goal)
        qa.append((role, task, ans))
        post(room, key, f"💬 [{role}] {ans}")
    post(room, key, f"✅ Итог по цели:\n{synthesize(goal, qa)}")
    print("OK: orchestration complete")
    return 0


def _load_seen():
    try:
        return set(json.load(open(SEEN_PATH, encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(seen):
    try:
        json.dump(sorted(seen), open(SEEN_PATH, "w", encoding="utf-8"))
    except Exception as e:
        print(f"seen save failed: {e}", file=sys.stderr)


def watch(room, key, interval):
    seen = _load_seen()
    print(f"watching room {room} every {interval}s (seen={len(seen)})")
    while True:
        try:
            goal, mid = latest_goal(room, key)
            if goal and mid and mid not in seen:
                run(room, key, goal)
                seen.add(mid)
                _save_seen(seen)
        except Exception as e:
            print(f"watch tick error: {e}", file=sys.stderr)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--key", default=os.environ.get("ORCH_ROOM_KEY", ""))
    ap.add_argument("--goal", default=None)
    ap.add_argument("--watch", type=int, default=0, help="poll interval sec (daemon mode)")
    a = ap.parse_args()
    if not a.key:
        print("room key required (--key or env ORCH_ROOM_KEY)", file=sys.stderr)
        return 2
    if a.watch:
        watch(a.room, a.key, a.watch)
        return 0
    goal = a.goal
    if not goal:
        goal, _ = latest_goal(a.room, a.key)
        if not goal:
            print("no @orchestrator goal found in room", file=sys.stderr)
            return 1
    return run(a.room, a.key, goal)


if __name__ == "__main__":
    sys.exit(main())
