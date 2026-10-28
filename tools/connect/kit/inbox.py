"""Приёмник комнат для ЖИВОЙ сессии Claude Code на любом ПК.

Запуск — фоновым наблюдателем сессии (Monitor), каждая строка stdout будит сессию:
    python -X utf8 -u ~/.cognitive-core/inbox.py
Ответ в комнату:
    python -X utf8 ~/.cognitive-core/inbox.py --reply <room_id> "текст"

Что делает:
  * при старте догоняет пропущенное: обращения к агенту за последние 12 ч, на которые
    после них ещё нет ответа этого агента и которые никто не взял заявкой;
  * каждые 4 с читает последние сообщения комнат и сверяет по id (без окна ожидания —
    long-poll терял сообщения);
  * отмечается в комнате как участник на связи (join + sync-pending), а для мостов —
    локом presence:<agent_id> в /coord, продление раз в 60 с;
  * перед выдачей берёт заявку reply:<room>:<msg> (409 — уже отвечает другой экземпляр).
Журнал решений: ~/.cognitive-core/inbox.log.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cogcore_kit import KIT_DIR, call, load_config, room_call  # noqa: E402

LOG = os.path.join(KIT_DIR, "inbox.log")
ANY_MENTION = re.compile(r"@[\w\-.:]+", re.U)
SERVICE = re.compile(r"^\s*(probe|heartbeat|ping)\b", re.I)
CATCHUP_HOURS = 12


def log(msg: str) -> None:
    try:
        os.makedirs(KIT_DIR, exist_ok=True)
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def mention_re(cfg: dict) -> re.Pattern:
    names = sorted({re.escape(m.lower()) for m in cfg["mentions"] + [cfg["agent_id"]]}, key=len, reverse=True)
    return re.compile(r"@(" + "|".join(names) + r")\w*\b", re.I | re.U)


def addressed(cfg: dict, mre: re.Pattern, m: dict) -> bool:
    text = str(m.get("text") or "")
    frm = str(m.get("from_agent") or "")
    own = {cfg["agent_id"], *cfg.get("self_ids", [])}
    if frm in own or frm.endswith("-proxy") or SERVICE.match(text):
        return False
    if mre.search(text):
        return True
    # без адресата — только реплики владельца
    return not ANY_MENTION.search(text) and frm in set(cfg.get("owner_names", ["owner", "владелец"]))


def lock(cfg: dict, resource: str, ttl: int) -> tuple[bool, str]:
    st, j = call(cfg, "POST", "/coord/lock",
                 body={"resource": resource, "holder": cfg["instance"], "ttl_seconds": ttl, "note": "live Claude session"})
    if st == 200:
        return True, ""
    det = j.get("detail") if isinstance(j, dict) else None
    return False, str(det.get("held_by") if isinstance(det, dict) else st)


def ts(m: dict) -> float:
    raw = str(m.get("created_at") or "").replace(" ", "T")
    try:
        d = dt.datetime.fromisoformat(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return 0.0


def emit(room: dict, m: dict) -> None:
    text = " ".join(str(m.get("text") or "").split())[:600]
    print(f"ROOM {room['id']} ({room.get('name', '')}) | {m.get('from_agent')} | msg {str(m.get('id'))[:8]} | {text}", flush=True)


def reply(cfg: dict, room_id: str, text: str) -> int:
    room = next((r for r in cfg["rooms"] if r["id"] == room_id or r.get("name") == room_id), None)
    if not room:
        print(f"нет комнаты {room_id} в {KIT_DIR}/config.json")
        return 2
    st, _ = room_call(cfg, room, "POST", "/post", body={"from_agent": cfg["agent_id"], "text": text})
    print("posted" if st == 200 else f"post failed {st}")
    return 0 if st == 200 else 1


def main() -> int:
    cfg = load_config()
    if len(sys.argv) >= 4 and sys.argv[1] == "--reply":
        return reply(cfg, sys.argv[2], sys.argv[3])
    if not cfg["api_key"] or not cfg["rooms"]:
        print("inbox: нет ключа или комнат — запустите setup (см. README набора)", flush=True)
        return 2
    mre = mention_re(cfg)
    seen: set[str] = set()
    now = time.time()
    caught = 0
    for room in cfg["rooms"]:
        room_call(cfg, room, "POST", "/join", body={"agent_id": cfg["agent_id"], "platform": cfg["instance"]})
        st, j = room_call(cfg, room, "GET", "/messages?limit=40")
        msgs = sorted((j.get("messages") or []) if st == 200 else [], key=ts)
        for i, m in enumerate(msgs):
            mid = str(m.get("id"))
            seen.add(mid)
            if now - ts(m) > CATCHUP_HOURS * 3600 or not addressed(cfg, mre, m):
                continue
            answered = any(str(n.get("from_agent")) == cfg["agent_id"] for n in msgs[i + 1:])
            if answered:
                continue
            ok, held = lock(cfg, f"reply:{room['id']}:{mid}", 300)
            if ok:
                caught += 1
                log(f"CATCHUP {mid[:8]} from={m.get('from_agent')}")
                emit(room, m)
    print(f"inbox: на связи как {cfg['agent_id']} ({cfg['instance']}), комнат {len(cfg['rooms'])}, "
          f"догнал {caught}, журнал {LOG}", flush=True)
    log(f"start pid={os.getpid()} seen={len(seen)} caught={caught}")
    last_beat = 0.0
    while True:
        if time.time() - last_beat > 60:
            ok, held = lock(cfg, f"presence:{cfg['agent_id']}", 150)
            if not ok:
                log(f"presence held by {held}")
            for room in cfg["rooms"]:
                room_call(cfg, room, "GET", f"/sync-pending?agent_id={cfg['agent_id']}")
            last_beat = time.time()
        for room in cfg["rooms"]:
            try:
                st, j = room_call(cfg, room, "GET", "/messages?limit=10")
            except Exception as e:  # сеть: следующий круг
                log(f"read {room['id'][:8]} failed: {e}")
                continue
            if st != 200:
                log(f"read {room['id'][:8]} HTTP {st}")
                continue
            for m in sorted(j.get("messages") or [], key=ts):
                mid = str(m.get("id"))
                if mid in seen:
                    continue
                seen.add(mid)
                if not addressed(cfg, mre, m):
                    continue
                ok, held = lock(cfg, f"reply:{room['id']}:{mid}", 300)
                if not ok:
                    log(f"claimed-by-other {mid[:8]}: {held}")
                    continue
                log(f"DELIVER {mid[:8]} from={m.get('from_agent')}")
                emit(room, m)
        time.sleep(4)


if __name__ == "__main__":
    sys.exit(main())
