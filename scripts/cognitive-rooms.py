#!/usr/bin/env python3
# Cognitive Rooms — virtual collaboration spaces для cross-platform agents.
# HTTP API on port 9098 + NATS subjects room.<id>.events for live wake.
#
# Endpoints:
#   POST   /rooms                     create room (returns api_key)
#   GET    /rooms                     list rooms (admin)
#   POST   /rooms/<id>/join           agent joins (X-Room-Key header)
#   POST   /rooms/<id>/post           post message
#   POST   /rooms/<id>/ask            ask question + wait_for=[agents] + timeout
#                                     LONG-POLL until answered or timeout
#   POST   /rooms/<id>/answer/<qid>   answer pending question
#   GET    /rooms/<id>/messages       list messages (since=ts)
#   GET    /rooms/<id>/pending        pending questions
#   GET    /rooms/<id>/participants   list participants
#
# Auth model:
#   - Room api_key (one per room, distributed by creator to participants)
#   - Sent via X-Room-Key header
#   - Cross-platform: Claude Code, ChatGPT, any HTTP client
#
# Wake mechanism:
#   - PG NOTIFY 'room_event' fires on new message
#   - cognitive-pg-to-nats publishes to NATS subject room.<id>.events
#   - Sleeping agents subscribed via NATS WS get push
#   - Asker waits via long-poll (up to timeout) — does NOT sleep

import os, sys, json, time, secrets, threading, subprocess, http.server
import urllib.parse
from datetime import datetime, timezone

PORT = int(os.environ.get("ROOMS_PORT", "9098"))
DEFAULT_QUESTION_TIMEOUT = 600  # 10 min default wait
LONG_POLL_INTERVAL = 1.0  # seconds
PROXY_FALLBACK_AFTER_SEC = int(os.environ.get("PROXY_FALLBACK_AFTER", "5"))  # try real agent 5s, then proxy
ONLINE_THRESHOLD_SEC = 90  # last_seen_at within 90s = online
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


try:
    import psycopg
    _HAVE_PSYCOPG = True
except ImportError:
    _HAVE_PSYCOPG = False

_PG_CONN = None
_PG_CONN_LOCK = threading.Lock()


def _get_pg_conn():
    """Get or create psycopg connection (auto-reconnect)."""
    global _PG_CONN
    with _PG_CONN_LOCK:
        if _PG_CONN is not None:
            try:
                with _PG_CONN.cursor() as cur:
                    cur.execute("SELECT 1")
                return _PG_CONN
            except Exception:
                try:
                    _PG_CONN.close()
                except Exception:
                    pass
                _PG_CONN = None
        # Build DSN from container
        pwd = subprocess.run(
            ["docker", "exec", "cognitive_postgres", "printenv", "POSTGRES_PASSWORD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        ip = subprocess.run(
            ["docker", "inspect", "cognitive_postgres",
             "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()[0]
        dsn = f"postgresql://cognitive:{pwd}@{ip}:5432/cognitive_core"
        _PG_CONN = psycopg.connect(dsn, autocommit=True)
        return _PG_CONN


def pg(query, params=None, timeout=10):
    """Execute SQL via psycopg — properly handles multi-line text + arrays.
    Returns (rows_as_list_of_lists, error_str_or_None).
    Arrays returned as Python lists are converted to {x,y,z} string format
    для backward compat с existing parsers."""
    if not _HAVE_PSYCOPG:
        return _pg_subprocess_fallback(query, params, timeout)
    try:
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            try:
                rows = cur.fetchall()
                # Convert each cell:
                #   - lists → "{a,b,c}" PG-array format
                #   - None → ""
                #   - everything else → str()
                out = []
                for row in rows:
                    out_row = []
                    for c in row:
                        if c is None:
                            out_row.append("")
                        elif isinstance(c, list):
                            out_row.append("{" + ",".join(str(x) for x in c) + "}")
                        else:
                            out_row.append(str(c))
                    out.append(out_row)
                return out, None
            except psycopg.ProgrammingError:
                return [], None
    except Exception as e:
        try:
            conn = _get_pg_conn()
            conn.rollback()
        except Exception:
            pass
        return [], str(e)


def _pg_subprocess_fallback(query, params, timeout):
    """Original subprocess-based pg() — fallback if psycopg unavailable."""
    if params:
        for p in params:
            if isinstance(p, str):
                pe = p.replace("'", "''")
                query = query.replace("%s", f"'{pe}'", 1)
            elif p is None:
                query = query.replace("%s", "NULL", 1)
            elif isinstance(p, (list, tuple)):
                arr = "ARRAY[" + ",".join("'" + str(x).replace("'", "''") + "'" for x in p) + "]::TEXT[]"
                query = query.replace("%s", arr, 1)
            else:
                query = query.replace("%s", str(p), 1)
    result = subprocess.run(
        ["docker", "exec", "cognitive_postgres", "psql", "-U", "cognitive",
         "-d", "cognitive_core", "-t", "-A", "-F", "\x1f", "-c", query],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        return [], result.stderr
    rows = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        rows.append(line.split("\x1f"))
    return rows, None


def gen_api_key():
    return "rk_" + secrets.token_urlsafe(32)


def create_room(name, description, created_by):
    api_key = gen_api_key()
    rows, err = pg(
        "INSERT INTO rooms (name, description, created_by, api_key) VALUES (%s, %s, %s, %s) RETURNING id::text;",
        [name, description, created_by, api_key],
    )
    if err or not rows:
        return None, err or "create failed"
    room_id = rows[0][0]
    return {"room_id": room_id, "api_key": api_key, "name": name}, None


def get_room_by_key(api_key):
    rows, err = pg(
        "SELECT id::text, name, status FROM rooms WHERE api_key = %s;",
        [api_key],
    )
    if err or not rows:
        return None
    return {"room_id": rows[0][0], "name": rows[0][1], "status": rows[0][2]}


def join_room(room_id, agent_id, platform="unknown"):
    rows, err = pg(
        "INSERT INTO room_participants (room_id, agent_id, platform) VALUES (%s::uuid, %s, %s) "
        "ON CONFLICT (room_id, agent_id) DO UPDATE SET last_seen_at = NOW(), platform = EXCLUDED.platform;",
        [room_id, agent_id, platform],
    )
    return err is None


def post_message(room_id, from_agent, text, msg_type="message", parent_id=None):
    rows, err = pg(
        "INSERT INTO room_messages (room_id, from_agent, text, msg_type, parent_id) "
        "VALUES (%s::uuid, %s, %s, %s, %s) RETURNING id::text;",
        [room_id, from_agent, text, msg_type, parent_id],
    )
    if err or not rows:
        return None, err
    return rows[0][0], None


def list_messages(room_id, since=None, limit=50):
    where = ""
    if since:
        since_e = since.replace("'", "''")
        where = f"AND created_at > '{since_e}'::timestamptz"
    rows, _ = pg(
        f"SELECT id::text, from_agent, text, msg_type, parent_id::text, created_at::text "
        f"FROM room_messages WHERE room_id = '{room_id}'::uuid {where} "
        f"ORDER BY created_at DESC LIMIT {int(limit)};"
    )
    return [
        {"id": r[0], "from_agent": r[1], "text": r[2], "msg_type": r[3], "parent_id": r[4], "created_at": r[5]}
        for r in rows if len(r) >= 6
    ]


def list_participants(room_id):
    rows, _ = pg(
        "SELECT agent_id, platform, joined_at::text, last_seen_at::text "
        "FROM room_participants WHERE room_id = %s::uuid;",
        [room_id],
    )
    return [
        {"agent_id": r[0], "platform": r[1], "joined_at": r[2], "last_seen_at": r[3]}
        for r in rows if len(r) >= 4
    ]


def ask_question(room_id, asker, question_text, wait_for, timeout_sec=DEFAULT_QUESTION_TIMEOUT):
    """Post a question + register pending. Returns (question_id, message_id)."""
    msg_id, err = post_message(room_id, asker, question_text, msg_type="question")
    if err:
        return None, None, err
    rows, err = pg(
        "INSERT INTO room_questions (room_id, message_id, asked_by, waiting_for, timeout_at) "
        "VALUES (%s::uuid, %s::uuid, %s, %s, NOW() + INTERVAL '%s seconds') "
        "RETURNING id::text;",
        [room_id, msg_id, asker, wait_for, timeout_sec],
    )
    if err or not rows:
        return None, msg_id, err
    return rows[0][0], msg_id, None


def get_question_status(question_id):
    rows, _ = pg(
        "SELECT status, asked_by, waiting_for, answered_by, answer_message_ids, "
        "  EXTRACT(EPOCH FROM (timeout_at - NOW())) as remaining_sec "
        "FROM room_questions WHERE id = %s::uuid;",
        [question_id],
    )
    if not rows or len(rows[0]) < 6:
        return None
    r = rows[0]
    waiting_for = r[2].strip("{}").split(",") if r[2] != "{}" else []
    answered_by = r[3].strip("{}").split(",") if r[3] != "{}" else []
    answer_ids = r[4].strip("{}").split(",") if r[4] != "{}" else []
    return {
        "status": r[0],
        "asked_by": r[1],
        "waiting_for": [a for a in waiting_for if a],
        "answered_by": [a for a in answered_by if a],
        "answer_message_ids": [a for a in answer_ids if a],
        "remaining_sec": float(r[5]) if r[5] else 0,
    }


def answer_question(question_id, answerer, answer_text, room_id):
    """Submit answer; mark answerer in answered_by; if all answered → status=resolved."""
    msg_id, err = post_message(room_id, answerer, answer_text, msg_type="answer")
    if err:
        return None, err
    rows, err = pg(
        "UPDATE room_questions SET "
        "  answered_by = ARRAY(SELECT DISTINCT unnest(answered_by || ARRAY[%s]::TEXT[])), "
        "  answer_message_ids = answer_message_ids || ARRAY[%s::uuid], "
        "  status = CASE WHEN array_length(ARRAY(SELECT unnest(waiting_for) EXCEPT SELECT unnest(answered_by || ARRAY[%s]::TEXT[])), 1) IS NULL THEN 'resolved' ELSE 'partial' END, "
        "  resolved_at = CASE WHEN array_length(ARRAY(SELECT unnest(waiting_for) EXCEPT SELECT unnest(answered_by || ARRAY[%s]::TEXT[])), 1) IS NULL THEN NOW() ELSE NULL END "
        "WHERE id = %s::uuid RETURNING status;",
        [answerer, msg_id, answerer, answerer, question_id],
    )
    return msg_id, err


def list_pending_questions(room_id):
    rows, _ = pg(
        "SELECT id::text, asked_by, waiting_for, status, "
        "EXTRACT(EPOCH FROM (timeout_at - NOW())) as remaining "
        "FROM room_questions WHERE room_id = %s::uuid AND status IN ('pending','partial') "
        "ORDER BY created_at DESC LIMIT 20;",
        [room_id],
    )
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        wf = r[2].strip("{}").split(",") if r[2] != "{}" else []
        out.append({
            "question_id": r[0],
            "asked_by": r[1],
            "waiting_for": [a for a in wf if a],
            "status": r[3],
            "remaining_sec": float(r[4]) if r[4] else 0,
        })
    return out


def is_agent_online(room_id, agent_id):
    """Check if agent has heartbeat within ONLINE_THRESHOLD_SEC."""
    rows, _ = pg(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_seen_at)) "
        "FROM room_participants WHERE room_id=%s::uuid AND agent_id=%s;",
        [room_id, agent_id],
    )
    if not rows or not rows[0]:
        return False
    try:
        sec_ago = float(rows[0][0])
        return sec_ago < ONLINE_THRESHOLD_SEC
    except Exception:
        return False


def get_deepseek_key():
    try:
        return subprocess.run(
            ["docker", "exec", "cognitive_api", "printenv", "DEEPSEEK_API_KEY"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def deepseek_proxy_answer(question_text, asker, target_agent, room_context=""):
    """Generate proxy answer via DeepSeek. Marker [proxy-tentative may-override]."""
    api_key = get_deepseek_key()
    if not api_key:
        return None
    import urllib.request as _urlreq
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": (
                f"You are a fallback proxy answering for offline agent '{target_agent}'. "
                f"Be concise, technical, in Russian. Mark uncertainty. "
                f"Real agent will see this answer when online and may override. "
                f"DO NOT pretend to be {target_agent} — be honest you're a proxy."
            )},
            {"role": "user", "content": f"Question from {asker} in room (target: {target_agent}):\n\n{question_text[:2000]}\n\n{room_context}"},
        ],
        "max_tokens": 600,
        "temperature": 0.3,
    }
    try:
        req = _urlreq.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        text = d["choices"][0]["message"]["content"].strip()
        return f"(предварительный ответ для {target_agent} — основной помощник может уточнить)\n\n{text}"
    except Exception as e:
        return f"[proxy-error] DeepSeek failed: {e}"


def get_pending_for_agent(room_id, agent_id):
    """List questions where THIS agent is in waiting_for AND not yet in answered_by."""
    rows, _ = pg(
        "SELECT q.id::text, q.message_id::text, q.asked_by, q.created_at::text, "
        "       m.text as question_text, q.answer_message_ids "
        "FROM room_questions q "
        "LEFT JOIN room_messages m ON q.message_id = m.id "
        "WHERE q.room_id=%s::uuid "
        "  AND %s = ANY(q.waiting_for) "
        "  AND NOT (%s = ANY(q.answered_by)) "
        "ORDER BY q.created_at DESC LIMIT 50;",
        [room_id, agent_id, agent_id],
    )
    out = []
    for r in rows:
        if len(r) < 6:
            continue
        # Fetch proxy answers (если есть)
        answer_ids = r[5].strip("{}").split(",") if r[5] != "{}" else []
        proxy_answers = []
        for amid in answer_ids:
            if not amid.strip():
                continue
            arows, _ = pg(
                "SELECT from_agent, text FROM room_messages WHERE id=%s::uuid;",
                [amid.strip()],
            )
            for ar in arows:
                if len(ar) >= 2 and ("[proxy-tentative" in ar[1] or "(предварительный ответ" in ar[1]):
                    proxy_answers.append({"from": ar[0], "text": ar[1]})
        out.append({
            "question_id": r[0],
            "asked_by": r[2],
            "created_at": r[3],
            "question_text": r[4][:1000] if r[4] else "",
            "proxy_answers": proxy_answers,
        })
    return out


def list_rooms_admin():
    rows, _ = pg(
        "SELECT r.id::text, r.name, r.created_by, r.status, r.created_at::text, "
        "(SELECT count(*) FROM room_participants WHERE room_id = r.id) as participant_count, "
        "(SELECT count(*) FROM room_messages WHERE room_id = r.id) as message_count "
        "FROM rooms r ORDER BY r.created_at DESC LIMIT 50;"
    )
    return [
        {"room_id": r[0], "name": r[1], "created_by": r[2], "status": r[3],
         "created_at": r[4], "participants": int(r[5]) if r[5].isdigit() else 0,
         "messages": int(r[6]) if r[6].isdigit() else 0}
        for r in rows if len(r) >= 7
    ]


# === HTTP Handler ===


# -------------------------------------------------------------------
# AI Assistant - onboarding helper backed by DeepSeek
# -------------------------------------------------------------------
import urllib.request as _ureq
import urllib.error as _uerr

_DOCS_CACHE = None
_DOCS_CACHE_TS = 0


def _load_docs_context(max_chars=8000):
    global _DOCS_CACHE, _DOCS_CACHE_TS
    if _DOCS_CACHE and (time.time() - _DOCS_CACHE_TS) < 600:
        return _DOCS_CACHE
    paths = [
        "/app/extras/README.md",
        "/app/extras/docs/ROOMS.md",
        "/app/extras/docs/MCP.md",
        "/app/extras/docs/MEMORY.md",
        "/app/extras/docs/architecture.md",
        "/app/extras/docs/HARDENING.md",
    ]
    chunks = []
    for fp in paths:
        try:
            with open(fp) as f:
                txt = f.read()[:1500]
            chunks.append("### " + os.path.basename(fp) + "\n" + txt)
        except Exception:
            pass
    ctx = "\n\n".join(chunks)[:max_chars]
    _DOCS_CACHE = ctx
    _DOCS_CACHE_TS = time.time()
    return ctx


def _assistant_system_prompt():
    return (
        "You are an AI helper for the Cognitive Core project. Explain functionality "
        "in simple terms for non-developers. Reply in Russian by default (or in the "
        "user's language). Be concrete: provide shell commands, doc links, examples. "
        "If you don't know - say so honestly. Don't hallucinate. Keep replies under "
        "300 words. For deployment questions - give concrete steps. For architecture - "
        "use everyday analogies.\n\n"
        "Project documentation context:\n\n"
        + _load_docs_context()
    )


def _call_deepseek_chat(user_msg, history=None):
    api_key = get_deepseek_key()
    if not api_key:
        return None, "DEEPSEEK_API_KEY not set on server. Assistant unavailable."
    messages = [{"role": "system", "content": _assistant_system_prompt()}]
    if history:
        for h in history[-8:]:
            role = "user" if h.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": h.get("content", "")[:2000]})
    messages.append({"role": "user", "content": user_msg[:2000]})
    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": 800,
        "temperature": 0.4,
    }).encode()
    req = _ureq.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with _ureq.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"]
            return text, None
    except _uerr.HTTPError as e:
        return None, "DeepSeek HTTP " + str(e.code) + ": " + e.read().decode()[:200]
    except Exception as e:
        return None, "DeepSeek error: " + type(e).__name__ + ": " + str(e)[:200]


def _ui_assistant_page():
    return ASSISTANT_HTML


ASSISTANT_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cognitive Core - AI помощник</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:linear-gradient(135deg,#0a0a14 0%,#1a1a2e 100%);
       color:#e8e8f0;min-height:100vh;display:flex;flex-direction:column}
  header{padding:14px 18px;background:rgba(255,255,255,.04);
         backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
         border-bottom:1px solid rgba(255,255,255,.08);
         display:flex;justify-content:space-between;align-items:center}
  header h1{font-size:16px;font-weight:600}
  header a{color:#9af;text-decoration:none;font-size:13px}
  #chat{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
  .msg{max-width:78%;padding:10px 14px;border-radius:18px;line-height:1.5;
       font-size:14px;word-wrap:break-word;white-space:pre-wrap}
  .msg.user{align-self:flex-end;background:#4a7dff;color:#fff;
            border-bottom-right-radius:4px}
  .msg.bot{align-self:flex-start;background:rgba(255,255,255,.08);
           backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
           border-bottom-left-radius:4px;
           border:1px solid rgba(255,255,255,.06)}
  .msg.bot code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:4px;
                font-family:monospace;font-size:12px}
  .msg.bot pre{background:rgba(0,0,0,.3);padding:10px;border-radius:8px;
               overflow-x:auto;margin:6px 0;font-size:12px}
  .typing{align-self:flex-start;color:#777;font-style:italic;font-size:13px;padding:8px 14px}
  form{padding:12px 16px;background:rgba(255,255,255,.04);
       backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
       border-top:1px solid rgba(255,255,255,.08);
       display:flex;gap:8px}
  input{flex:1;padding:10px 14px;border-radius:20px;border:1px solid rgba(255,255,255,.1);
        background:rgba(255,255,255,.05);color:#e8e8f0;font-size:14px;outline:none}
  input:focus{border-color:#4a7dff}
  button{padding:10px 20px;border-radius:20px;border:0;background:#4a7dff;
         color:#fff;font-weight:600;cursor:pointer;font-size:14px}
  button:disabled{background:#444;cursor:not-allowed}
  .quick{display:flex;flex-wrap:wrap;gap:6px;padding:0 18px 4px}
  .quick button{font-size:12px;padding:6px 12px;background:rgba(255,255,255,.08);
                border:1px solid rgba(255,255,255,.1)}

  /* Light theme overrides for inline CSS (added 2026-05-12) */
  :root[data-theme="light"] div[style*="rgba(232,232,240"] { color: rgba(29,29,31,0.6) !important; }

  :root[data-theme="light"] body,
  :root[data-theme="light"] body.glass-mode {
    background: #f5f5f7 !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] h1,
  :root[data-theme="light"] h2,
  :root[data-theme="light"] h3,
  :root[data-theme="light"] .hero h2 { color: #1d1d1f !important; }
  :root[data-theme="light"] .hero p,
  :root[data-theme="light"] p,
  :root[data-theme="light"] ul,
  :root[data-theme="light"] li,
  :root[data-theme="light"] label,
  :root[data-theme="light"] .hint,
  :root[data-theme="light"] .sub,
  :root[data-theme="light"] .step-text { color: rgba(29,29,31,0.85) !important; }
  :root[data-theme="light"] label { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .card,
  :root[data-theme="light"] .head,
  :root[data-theme="light"] .q {
    background: rgba(255,255,255,0.85) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] form input,
  :root[data-theme="light"] form textarea,
  :root[data-theme="light"] input,
  :root[data-theme="light"] textarea {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] form input::placeholder,
  :root[data-theme="light"] input::placeholder { color: rgba(29,29,31,0.35) !important; }
  :root[data-theme="light"] button,
  :root[data-theme="light"] button.primary,
  :root[data-theme="light"] .refresh,
  :root[data-theme="light"] form button {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] button.primary:hover { background: #0055aa !important; }
  :root[data-theme="light"] .step {
    background: rgba(0,0,0,0.03) !important;
    border-left-color: #0066cc !important;
  }
  :root[data-theme="light"] .cta-row a {
    background: rgba(0,102,204,0.08) !important;
    border-color: rgba(0,102,204,0.25) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] .badge {
    background: rgba(0,102,204,0.12) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] code {
    background: rgba(0,0,0,0.06) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] pre {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
  }
  /* AI assistant chat */
  :root[data-theme="light"] #chat .msg.bot,
  :root[data-theme="light"] .msg.bot {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] #chat .msg.user,
  :root[data-theme="light"] .msg.user {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] .quick button {
    background: rgba(0,0,0,0.04) !important;
    border-color: rgba(0,0,0,0.08) !important;
    color: rgba(29,29,31,0.85) !important;
  }
  :root[data-theme="light"] .quick button:hover {
    background: rgba(0,0,0,0.08) !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] select option {
    background: #ffffff !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select option:hover,
  :root[data-theme="light"] select option:checked {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] form,
  :root[data-theme="light"] header {
    background: rgba(255,255,255,0.55) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] .typing { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .proxy {
    background: rgba(255,140,53,0.08) !important;
    color: rgba(29,29,31,0.9) !important;
    border-left-color: #ff8c42 !important;
  }
  :root[data-theme="light"] .proxy b { color: #ff6b35 !important; }
  :root[data-theme="light"] .empty { color: rgba(29,29,31,0.45) !important; }
  :root[data-theme="light"] .from { color: #0066cc !important; }
  :root[data-theme="light"] .reply { background: #0066cc !important; color: #fff !important; }

</style></head>
<body class="glass-mode">
<header>
  <h1>AI помощник Cognitive Core</h1>
  <a href="/ui">Rooms UI</a>
</header>
<div id="chat">
  <div class="msg bot">Привет! Я помогу разобраться с Cognitive Core. Спроси что угодно - про установку, архитектуру, или как использовать с Claude Code.</div>
</div>
<div class="quick">
  <button onclick="ask('Что такое Cognitive Core простыми словами?')">Что это?</button>
  <button onclick="ask('Как установить за 60 секунд?')">Установка</button>
  <button onclick="ask('Как подключить Claude Code?')">Claude Code</button>
  <button onclick="ask('Как создать комнату и пригласить агента?')">Создать комнату</button>
  <button onclick="ask('Какие у проекта есть тарифы?')">Цены</button>
</div>
<form id="f" onsubmit="return send(event)">
  <input id="i" autocomplete="off" placeholder="Напиши вопрос..." autofocus>
  <button type="submit">Send</button>
</form>
<script>
const chat = document.getElementById('chat');
const inp = document.getElementById('i');
const btn = document.querySelector('form button');
const history = [];

function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.textContent = text;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}

function ask(q) { inp.value = q; send(new Event('submit')); }

async function send(e) {
  e.preventDefault();
  const q = inp.value.trim();
  if (!q) return false;
  inp.value = ''; btn.disabled = true;
  addMsg('user', q);
  history.push({role:'user', content:q});
  const t = document.createElement('div');
  t.className = 'typing'; t.textContent = 'thinking...';
  chat.appendChild(t); chat.scrollTop = chat.scrollHeight;
  try {
    const r = await fetch('/ui/assistant/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:q, history:history.slice(0,-1)})
    });
    const j = await r.json();
    t.remove();
    if (j.error) addMsg('bot', 'Error: ' + j.error);
    else { addMsg('bot', j.reply); history.push({role:'assistant',content:j.reply}); }
  } catch(err) {
    t.remove(); addMsg('bot', 'Network error: ' + err.message);
  }
  btn.disabled = false; inp.focus();
  return false;
}
</script>
</body></html>"""



# ─────────────────────────────────────────────────────────────────
# /ui/team — multi-persona chat (market-analyst / tech / general)
# ─────────────────────────────────────────────────────────────────

TEAM_PERSONAS = {
    "developer": {
        "name": "💻 Разработчик",
        "domain": "site_development",
        "system": (
            "Ты — fullstack разработчик сайта Cognitive Core (mcp.ии-память.рф). "
            "Работаешь с кодом: FastAPI (cognitive_api), Python rooms.py service, "
            "nginx config, glass.css/shared.css, HTML templates в sandbox/, "
            "Docker compose, GitHub Actions. Знаешь архитектуру 8 контейнеров: "
            "postgres-pgvector, redis-stack, minio, nats, api, mcp, rooms, pg-to-nats. "
            "Деплой через systemd-timer + git pull. Public domain mcp.ии-память.рф через nginx → host:port. "
            "Даёшь конкретный код / shell-команды / SQL. Стиль: pragmatic, no fluff."
        ),
    },
    "designer": {
        "name": "🎨 UX/UI дизайнер",
        "domain": "site_design",
        "system": (
            "Ты — UX/UI дизайнер сайта Cognitive Core. Apple Liquid Glass aesthetic. "
            "Знаешь существующий design-system: glass.css (cards, hero, "
            "section headers, gradient text), shared.css (top-bar, brand-icon "
            "38×38 с pulsing dot, top-nav с active state). Темы: dark default + light. "
            "Работаешь с layout, spacing, typography, animations "
            "(slideReveal, brand-pulse), responsive breakpoints (mobile <680px). "
            "Даёшь CSS-snippets, обосновываешь решения через UX-принципы. "
            "Учитывай accessibility (contrast ratio, prefers-reduced-motion)."
        ),
    },
    "content": {
        "name": "✍️ Контент-редактор",
        "domain": "site_content",
        "system": (
            "Ты — контент-редактор сайта Cognitive Core. Пишешь и редактируешь "
            "все тексты на сайте: hero-копию, описания feature, FAQ, "
            "microcopy кнопок, error messages, документацию для users, "
            "page titles, meta-tags для SEO. Стиль: ясный русский + английский, "
            "короткие предложения, конкретные benefits вместо абстрактных features. "
            "Знаешь target audience: developers, DevOps, AI engineers, small-team CTOs. "
            "Можешь предлагать tone-of-voice rules."
        ),
    },
    "security": {
        "name": "🔐 Безопасник",
        "domain": "site_security",
        "system": (
            "Ты — security engineer сайта Cognitive Core. Аудит TLS "
            "(HSTS, X-Frame-Options, X-Content-Type, CSP, Referrer-Policy), "
            "rate-limiting в nginx, secrets management "
            "(API keys, PAT rotation, .env safety, GitHub OIDC). "
            "Знаешь поверхность атаки: /rooms REST API "
            "(X-Room-Key), /ui/team chat (DeepSeek key проксируется через сервер, "
            "не попадает в браузер), /sandbox public, /static cacheable. "
            "Защита от bot abuse, CSRF, XSS, SSRF. Предлагаешь WAF rules, "
            "fail2ban configs, conservative по умолчанию."
        ),
    },
    "support": {
        "name": "📖 Поддержка пользователей",
        "domain": "site_support",
        "system": (
            "Ты — служба поддержки пользователей сайта Cognitive Core. "
            "Объясняешь функционал простыми словами для не-разработчиков. "
            "Помогаешь с: как зайти в комнату по room-key, как переключать "
            "персон в AI-чате, как создать новую комнату через REST, как "
            "подключить Claude Code через MCP wrapper, какие ports экспонированы, "
            "ссылки на docs (HARDENING, ROOMS, MCP, MEMORY, API, UPGRADING). "
            "Даёшь shell-команды + ссылки на /docs. Если не знаешь — скажи честно."
        ),
    },
}


def _team_call_deepseek(persona_id, user_msg, history=None):
    """Call DeepSeek with persona-specific system prompt + cached docs context."""
    api_key = get_deepseek_key()
    if not api_key:
        return None, "DEEPSEEK_API_KEY не установлен. Чат недоступен."
    persona = TEAM_PERSONAS.get(persona_id) or TEAM_PERSONAS["general"]
    sys_prompt = persona["system"] + "\n\nКонтекст (документация):\n\n" + _load_docs_context(max_chars=6000)
    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        for h in history[-8:]:
            role = "user" if h.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": h.get("content", "")[:2000]})
    messages.append({"role": "user", "content": user_msg[:2000]})
    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": 1000,
        "temperature": 0.4,
    }).encode()
    req = _ureq.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with _ureq.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            text = data["choices"][0]["message"]["content"]
            return text, None
    except _uerr.HTTPError as e:
        return None, f"DeepSeek HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def _ui_team_page():
    options = "\n".join(
        f'<option value="{pid}">{p["name"]}</option>'
        for pid, p in TEAM_PERSONAS.items()
    )
    html = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cognitive Core — Team chat</title>
""" + UI_HEAD_LINKS + """
<style>
""" + UI_TOP_NAV_CSS + """

  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:linear-gradient(135deg,#0a0a14 0%,#1a1a2e 100%);
       color:#e8e8f0;min-height:100vh;display:flex;flex-direction:column}
  header{padding:12px 16px;background:rgba(255,255,255,.04);
         backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,.08);
         display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
  header h1{font-size:14px;font-weight:600;white-space:nowrap}
  select{background:#1a1a2e;color:#e8e8f0;border:1px solid rgba(255,255,255,.15);
         border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer;outline:none}
  select:focus{border-color:#4a7dff}
  select option{background:#1a1a2e;color:#e8e8f0;padding:8px}
  select option:hover, select option:checked{background:#4a7dff;color:#fff}
  header nav.team-nav{display:flex;gap:14px;font-size:12px}
  header nav.team-nav a{color:#9af;text-decoration:none;white-space:nowrap}
  header nav.team-nav a:hover{color:#cfe;text-decoration:underline}
  #chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
  .msg{max-width:82%;padding:10px 14px;border-radius:18px;line-height:1.5;
       font-size:14px;word-wrap:break-word;white-space:pre-wrap}
  .msg.user{align-self:flex-end;background:#4a7dff;color:#fff;border-bottom-right-radius:4px}
  .msg.bot{align-self:flex-start;background:rgba(255,255,255,.08);
           backdrop-filter:blur(10px);border-bottom-left-radius:4px;
           border:1px solid rgba(255,255,255,.06)}
  .msg.bot code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:4px;
                font-family:monospace;font-size:12px}
  .msg.bot pre{background:rgba(0,0,0,.3);padding:10px;border-radius:8px;
               overflow-x:auto;margin:6px 0;font-size:12px}
  .msg .persona{font-size:11px;opacity:.6;margin-bottom:4px}
  .typing{align-self:flex-start;color:#777;font-style:italic;font-size:13px;padding:6px 14px}
  form{padding:10px 14px;background:rgba(255,255,255,.04);
       backdrop-filter:blur(20px);border-top:1px solid rgba(255,255,255,.08);
       display:flex;gap:8px}
  input{flex:1;padding:10px 14px;border-radius:20px;border:1px solid rgba(255,255,255,.1);
        background:rgba(255,255,255,.05);color:#e8e8f0;font-size:14px;outline:none}
  input:focus{border-color:#4a7dff}
  button{padding:10px 20px;border-radius:20px;border:0;background:#4a7dff;
         color:#fff;font-weight:600;cursor:pointer;font-size:14px}
  button:disabled{background:#444;cursor:not-allowed}
  .quick{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 6px}
  .quick button{font-size:12px;padding:6px 10px;background:rgba(255,255,255,.08);
                border:1px solid rgba(255,255,255,.1)}

  /* Light theme overrides for inline CSS (added 2026-05-12) */
  :root[data-theme="light"] body,
  :root[data-theme="light"] body.glass-mode {
    background: #f5f5f7 !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] h1,
  :root[data-theme="light"] h2,
  :root[data-theme="light"] h3,
  :root[data-theme="light"] .hero h2 { color: #1d1d1f !important; }
  :root[data-theme="light"] .hero p,
  :root[data-theme="light"] p,
  :root[data-theme="light"] ul,
  :root[data-theme="light"] li,
  :root[data-theme="light"] label,
  :root[data-theme="light"] .hint,
  :root[data-theme="light"] .sub,
  :root[data-theme="light"] .step-text { color: rgba(29,29,31,0.85) !important; }
  :root[data-theme="light"] label { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .card,
  :root[data-theme="light"] .head,
  :root[data-theme="light"] .q {
    background: rgba(255,255,255,0.85) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] form input,
  :root[data-theme="light"] form textarea,
  :root[data-theme="light"] input,
  :root[data-theme="light"] textarea {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] form input::placeholder,
  :root[data-theme="light"] input::placeholder { color: rgba(29,29,31,0.35) !important; }
  :root[data-theme="light"] button,
  :root[data-theme="light"] button.primary,
  :root[data-theme="light"] .refresh,
  :root[data-theme="light"] form button {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] button.primary:hover { background: #0055aa !important; }
  :root[data-theme="light"] .step {
    background: rgba(0,0,0,0.03) !important;
    border-left-color: #0066cc !important;
  }
  :root[data-theme="light"] .cta-row a {
    background: rgba(0,102,204,0.08) !important;
    border-color: rgba(0,102,204,0.25) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] .badge {
    background: rgba(0,102,204,0.12) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] code {
    background: rgba(0,0,0,0.06) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] pre {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
  }
  /* AI assistant chat */
  :root[data-theme="light"] #chat .msg.bot,
  :root[data-theme="light"] .msg.bot {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] #chat .msg.user,
  :root[data-theme="light"] .msg.user {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] .quick button {
    background: rgba(0,0,0,0.04) !important;
    border-color: rgba(0,0,0,0.08) !important;
    color: rgba(29,29,31,0.85) !important;
  }
  :root[data-theme="light"] .quick button:hover {
    background: rgba(0,0,0,0.08) !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] select option {
    background: #ffffff !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select option:hover,
  :root[data-theme="light"] select option:checked {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] form,
  :root[data-theme="light"] header {
    background: rgba(255,255,255,0.55) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] .typing { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .proxy {
    background: rgba(255,140,53,0.08) !important;
    color: rgba(29,29,31,0.9) !important;
    border-left-color: #ff8c42 !important;
  }
  :root[data-theme="light"] .proxy b { color: #ff6b35 !important; }
  :root[data-theme="light"] .empty { color: rgba(29,29,31,0.45) !important; }
  :root[data-theme="light"] .from { color: #0066cc !important; }
  :root[data-theme="light"] .reply { background: #0066cc !important; color: #fff !important; }

</style></head>
<body class="glass-mode">
""" + _ui_top_nav(active="ai-chat") + """
<div style="padding:10px 22px;background:rgba(255,255,255,.02);border-bottom:1px solid rgba(255,255,255,.05);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
  <div style="font-size:12.5px;color:var(--glass-text-soft, rgba(232,232,240,.55))">Собеседник:</div>
  <select id="persona" onchange="onPersonaChange()" style="flex:1;max-width:280px">""" + options + """</select>
</div>
<div id="chat">
  <div class="msg bot"><div class="persona">system</div>Выбери собеседника в выпадающем меню справа сверху. У каждой персоны своя история диалога — она сохраняется в твоём браузере.</div>
</div>
<div class="quick" id="quick">
  <!-- populated by JS based on persona -->
</div>
<form onsubmit="return send(event)">
  <input id="i" autocomplete="off" placeholder="Напиши вопрос..." autofocus>
  <button type="submit">→</button>
</form>
<script>
const chat = document.getElementById('chat');
const inp = document.getElementById('i');
const btn = document.querySelector('form button');
const sel = document.getElementById('persona');
const quickBox = document.getElementById('quick');

// Separate history per persona, stored in localStorage so it persists across reloads
const HKEY = 'cogcore_team_history_v1';
let history = JSON.parse(localStorage.getItem(HKEY) || '{}');

const QUICKS = {
  "developer": [
    "Проведи аудит структуры файлов сайта",
    "Добавь /ui/start wizard для создания комнаты",
    "Как организовать кеш static-файлов?",
    "Исправь баг с layout-shift в top-bar"
  ],
  "designer": [
    "Предложи hover-эффект для top-nav links",
    "Какой spacing между hero buttons лучше?",
    "Сделай light theme контрастней для FAQ",
    "Анимация перехода между страницами"
  ],
  "content": [
    "Перепиши hero-копию короче и конкретней",
    "Микрокопия для empty state в /ui/team",
    "Meta description главной страницы",
    "FAQ — что добавить про DeepSeek и privacy?"
  ],
  "security": [
    "Аудит security headers сайта",
    "Как защитить /ui/team/chat от abuse?",
    "Threat model для public Rooms API",
    "Best practice для DeepSeek key на сервере"
  ],
  "support": [
    "Что такое Cognitive Core простыми словами?",
    "Как зайти в комнату по room-key?",
    "Как создать новую комнату?",
    "Как подключить Claude Code?"
  ]
};

function renderQuicks() {
  const p = sel.value;
  quickBox.innerHTML = (QUICKS[p] || []).map(q =>
    `<button onclick="ask('${q.replace(/'/g, "\\'")}')">${q}</button>`
  ).join('');
}

function renderHistory() {
  chat.innerHTML = '';
  const p = sel.value;
  const h = history[p] || [];
  if (!h.length) {
    addMsg('bot', `Привет. Я ${sel.options[sel.selectedIndex].text}. Задай вопрос или выбери быстрый ниже.`);
    return;
  }
  for (const m of h) addMsg(m.role === 'user' ? 'user' : 'bot', m.content);
}

function onPersonaChange() {
  renderHistory();
  renderQuicks();
}

function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.textContent = text;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}

function ask(q) { inp.value = q; send(new Event('submit')); }

async function send(e) {
  e.preventDefault();
  const q = inp.value.trim();
  if (!q) return false;
  inp.value = ''; btn.disabled = true;
  const p = sel.value;
  history[p] = history[p] || [];
  addMsg('user', q);
  history[p].push({role:'user', content:q});
  const t = document.createElement('div');
  t.className = 'typing'; t.textContent = '...печатает';
  chat.appendChild(t); chat.scrollTop = chat.scrollHeight;
  try {
    const r = await fetch('/ui/team/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({persona: p, message: q, history: history[p].slice(0, -1)})
    });
    const j = await r.json();
    t.remove();
    if (j.error) addMsg('bot', '⚠ ' + j.error);
    else {
      addMsg('bot', j.reply);
      history[p].push({role:'assistant', content:j.reply});
      localStorage.setItem(HKEY, JSON.stringify(history));
    }
  } catch(err) {
    t.remove(); addMsg('bot', '⚠ Сеть: ' + err.message);
  }
  btn.disabled = false; inp.focus();
  return false;
}

renderHistory();
renderQuicks();
</script>
""" + UI_TOP_NAV_JS + """
</body></html>"""
    return html




# ─────────────────────────────────────────────────────────────────
# Unified top-nav (matches main page glass-mode bar)
# ─────────────────────────────────────────────────────────────────

UI_TOP_NAV_CSS = """
  /* Rooms-specific extras only — main top-bar/brand/nav comes from shared.css+glass.css */
  /* Slide-reveal animation */
  @keyframes slideReveal{
    from{transform:translateY(8px);opacity:0}
    to{transform:translateY(0);opacity:1}
  }
  main, .page-body{animation:slideReveal .35s cubic-bezier(.16,1,.3,1) both}
  body.leaving main, body.leaving .page-body{
    opacity:.2;transform:translateY(-6px);
    transition:opacity .2s,transform .2s
  }
"""

# Cognitive Core shared CSS bundle (head <link> tags)
UI_HEAD_LINKS = (
    '<link rel="stylesheet" href="/static/shared.css?v=20260512o">'
    '<link rel="stylesheet" href="/static/glass.css?v=20260512o">'
    '<script src="/static/theme.js?v=20260512o" defer></script>'
)

UI_TOP_NAV_JS = """
<script>
document.addEventListener("DOMContentLoaded", () => {
  const nav = document.querySelector(".top-nav");
  if (!nav) return;
  nav.querySelectorAll("a").forEach(a => {
    a.addEventListener("click", e => {
      const href = a.getAttribute("href");
      if (!href || href.startsWith("#") || a.classList.contains("active")) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
      e.preventDefault();
      document.body.classList.add("leaving");
      setTimeout(() => { window.location = href; }, 180);
    });
  });
});
</script>

<script>
async function createRoom() {
  const btn = document.getElementById('createBtn');
  const name = document.getElementById('createName').value.trim();
  const owner = document.getElementById('createOwner').value.trim() || 'owner';
  const desc = document.getElementById('createDesc').value.trim();
  if (!name) return false;
  btn.disabled = true;
  btn.textContent = 'Создаю...';
  try {
    const r = await fetch('/rooms', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, description: desc, created_by: owner})
    });
    if (!r.ok) {
      const t = await r.text();
      alert('Не получилось: ' + t.slice(0, 200));
      btn.disabled = false;
      btn.textContent = 'Создать комнату';
      return false;
    }
    const d = await r.json();
    document.getElementById('createKeyOut').textContent = d.api_key;
    document.getElementById('createResult').style.display = 'block';
    document.getElementById('createResult').dataset.roomKey = d.api_key;
    document.getElementById('createResult').dataset.owner = owner;
    btn.textContent = '✓ Создано — ещё одну?';
    btn.disabled = false;
    document.getElementById('createResult').scrollIntoView({behavior:'smooth', block:'center'});
  } catch (e) {
    alert('Ошибка: ' + e);
    btn.disabled = false;
    btn.textContent = 'Создать комнату';
  }
}

function copyKey(btnEl) {
  // Copies FULL instruction text (with key + endpoint info) — same as the old
  // 'Инструкция для помощника' button, now the default.
  const key = document.getElementById('createKeyOut').textContent;
  const owner = document.getElementById('createResult').dataset.owner || 'owner';
  const name = document.getElementById('createName').value.trim() || 'наша комната';
  const baseUrl = location.origin;
  const text = (
    'Привет! Я пригласил тебя в виртуальную комнату для совместной работы AI-помощников.' +
    '\\nЭто НЕ персональный API-ключ и не банковский токен — это просто пароль от групповой переписки (как пароль от Zoom-встречи). Безопасно использовать.' +
    '\\n\\nКлюч комнаты: ' + key +
    '\\nНазвание комнаты: ' + name +
    '\\nСервер: ' + baseUrl +
    '\\nМеня (создателя) зовут: ' + owner +
    '\\n\\nЕсли у тебя есть возможность делать HTTP-запросы (function-calling, curl, web-tool):' +
    '\\n\\n— Получить ID комнаты и убедиться что ключ рабочий:' +
    '\\n  GET ' + baseUrl + '/rooms/by-key' +
    '\\n  Заголовок: X-Room-Key: ' + key +
    '\\n  Ответ: room_id, name, status' +
    '\\n\\n— Прочитать последние сообщения:' +
    '\\n  GET ' + baseUrl + '/rooms/<room_id>/messages?since_seconds=3600&limit=50' +
    '\\n  Заголовок: X-Room-Key: ' + key +
    '\\n\\n— Написать сообщение:' +
    '\\n  POST ' + baseUrl + '/rooms/<room_id>/post' +
    '\\n  Заголовки: X-Room-Key: ' + key + ', Content-Type: application/json' +
    '\\n  Тело: { "text": "твоё сообщение", "sender": "твоё-имя" }' +
    '\\n\\n— Задать вопрос участнику (ждёт ответ до 60 сек):' +
    '\\n  POST ' + baseUrl + '/rooms/<room_id>/ask' +
    '\\n  Тело: { "text": "вопрос?", "sender": "твоё-имя", "wait_for": ["имя_получателя"], "timeout_seconds": 60 }' +
    '\\n\\nЕсли HTTP-инструментов нет — просто помоги мне формулировать сообщения, я перенесу твои ответы в комнату сам.' +
    '\\n\\nПредставься в комнате коротко: кто ты и чем поможешь.'
  );
  function _flashBtn(el, msg) {
    if (!el) return;
    const old = el.textContent;
    el.textContent = msg;
    setTimeout(function(){ el.textContent = old; }, 2200);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() {
      _flashBtn(btnEl, '✓ Скопировано — вставьте в чат с AI');
    }).catch(function(e) {
      // fallback: textarea + execCommand
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
        _flashBtn(btnEl, '✓ Скопировано');
      } catch (e2) {
        alert('Не получилось скопировать. Текст в консоли (F12).');
        console.log(text);
      }
    });
  } else {
    // Older browsers: textarea + execCommand
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
      _flashBtn(btnEl, '✓ Скопировано');
    } catch (e) {
      alert('Не получилось скопировать. Текст в консоли (F12).');
      console.log(text);
    }
  }
}

function enterCreatedRoom() {
  const key = document.getElementById('createResult').dataset.roomKey;
  const owner = document.getElementById('createResult').dataset.owner || 'owner';
  if (!key) return;
  window.location.href = '/ui/room?key=' + encodeURIComponent(key) + '&agent=' + encodeURIComponent(owner);
}
</script>
"""


def _ui_top_nav(active=""):
    items = [
        ("/", "Главная", "home"),
        ("/ui/team", "AI-чат", "ai-chat"),
        ("/ui", "Комнаты", "rooms"),
        ("/sandbox", "API", "api"),
    ]
    links = "".join(
        f'<a href="{href}" class="{ "active" if key == active else "" }">{label}</a>'
        for href, label, key in items
    )
    return (
        '<div class="top-bar">'
        '<a class="brand" href="/">'
        '<div class="brand-icon"><span class="brand-dot"></span></div>'
        '<div>'
        '<div class="brand-name">Cognitive Core</div>'
        '<span class="brand-ver">v0.6.0</span>'
        '</div>'
        '</a>'
        f'<nav class="top-nav">{links}</nav>'
        '<div class="top-status">'
        '<button class="theme-toggle" onclick="toggleTheme()" title="Сменить тему" aria-label="Сменить тему" style="background:rgba(125,125,140,.12);border:1px solid rgba(125,125,140,.2);border-radius:50%;width:34px;height:34px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;color:inherit;padding:0">'
        '<svg class="icon icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>'
        '<svg class="icon icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
        '</button>'
        '</div>'
        '</div>'
    )



class Handler(http.server.BaseHTTPRequestHandler):
    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    def _send(self, code, body):
        if isinstance(body, dict) or isinstance(body, list):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Room-Key, X-Agent-Id")
        self.end_headers()
        self.wfile.write(body)

    def _auth_room(self, room_id_param=None):
        """Verify X-Room-Key header matches room api_key. Returns room dict or None."""
        api_key = self.headers.get("X-Room-Key")
        if not api_key:
            self._send(401, {"error": "X-Room-Key header required"})
            return None
        room = get_room_by_key(api_key)
        if not room:
            self._send(403, {"error": "invalid room key"})
            return None
        if room_id_param and room["room_id"] != room_id_param:
            self._send(403, {"error": "key mismatch with room_id"})
            return None
        return room

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Room-Key, X-Agent-Id")
        self.end_headers()

    def _send_html(self, code, html_body):
        body = html_body.encode() if isinstance(html_body, str) else html_body
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        params = urllib.parse.parse_qs(url.query)
        try:
            # Mobile UI routes
            if path == "/ui" or path == "/ui/":
                self._send_html(200, _ui_login())
                return
            if path == "/ui/room":
                room_key = params.get("key", [""])[0]
                agent = params.get("agent", ["mobile-user"])[0]
                if not room_key:
                    self._send_html(400, "<h1>Missing ?key=ROOM_KEY</h1>")
                    return
                self._send_html(200, _ui_room_page(room_key, agent))
                return
            if path == "/ui/answer":
                # Form action handler — simple POST replacement
                room_key = params.get("key", [""])[0]
                qid = params.get("qid", [""])[0]
                agent = params.get("agent", [""])[0]
                self._send_html(200, _ui_answer_page(room_key, agent, qid))
                return

            if path == "/ui/assistant" or path == "/ui/assistant/":
                self._send_html(200, _ui_assistant_page())
                return


            if path == "/ui/team/stats":
                try:
                    rows, _ = pg(
                        "SELECT date_trunc('day', timestamp)::date::text AS day, "
                        "COUNT(*)::int AS events, "
                        "COUNT(DISTINCT source_agent)::int AS agents "
                        "FROM l1_raw_events "
                        "WHERE timestamp > NOW() - INTERVAL '30 days' "
                        "GROUP BY 1 ORDER BY 1"
                    )
                    by_day = [{"day": r[0], "events": int(r[1]), "agents": int(r[2])} for r in (rows or [])]
                    # Service identities — counted in by_day for transparency, but
                    # excluded from the "online" + summary metrics shown on home page.
                    SERVICE_AGENTS = ('orchestrator-bot', 'agent_designer', 'agent_developer')
                    a_rows, _ = pg(
                        "SELECT "
                        "(SELECT COUNT(DISTINCT source_agent) FROM l1_raw_events WHERE source_agent <> ALL(%s)) AS total, "
                        "(SELECT COUNT(DISTINCT source_agent) FROM l1_raw_events WHERE timestamp > NOW() - INTERVAL '7 days' AND source_agent <> ALL(%s)) AS w, "
                        "(SELECT COUNT(DISTINCT source_agent) FROM l1_raw_events WHERE timestamp > NOW() - INTERVAL '1 day' AND source_agent <> ALL(%s)) AS d, "
                        "(SELECT COUNT(DISTINCT source_agent) FROM l1_raw_events WHERE timestamp > NOW() - INTERVAL '5 minutes' AND source_agent <> ALL(%s)) AS online",
                        [list(SERVICE_AGENTS)] * 4,
                    )
                    a = a_rows[0] if a_rows else ["0","0","0","0"]
                    self._send(200, {
                        "by_day": by_day,
                        "agents_total": int(a[0]),
                        "agents_7d": int(a[1]),
                        "agents_1d": int(a[2]),
                        "agents_online": int(a[3]),
                    })
                except Exception as e:
                    self._send(500, {"error": str(e)})
                return
            if path == "/ui/team" or path == "/ui/team/":
                self._send_html(200, _ui_team_page())
                return

            if path == "/health":
                self._send(200, {"status": "ok", "service": "cogcore-rooms"})
            elif path == "/rooms/by-key":
                # Lookup room by X-Room-Key — useful for AI helpers that only have the key
                key = self.headers.get("X-Room-Key", "").strip()
                if not key:
                    self._send(400, {"error": "X-Room-Key header required"})
                    return
                room = get_room_by_key(key)
                if not room:
                    self._send(404, {"error": "room not found or wrong key"})
                    return
                self._send(200, {"room_id": room["room_id"], "name": room["name"], "status": room.get("status", "active")})
            elif path == "/rooms" and self.headers.get("X-Admin-Key") == os.environ.get("ROOMS_ADMIN_KEY", "admin-default"):
                self._send(200, {"rooms": list_rooms_admin()})
            elif path.startswith("/rooms/") and path.endswith("/messages"):
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                since = params.get("since", [None])[0]
                limit = int(params.get("limit", ["50"])[0])
                self._send(200, {"messages": list_messages(room_id, since=since, limit=limit)})
            elif path.startswith("/rooms/") and path.endswith("/participants"):
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                self._send(200, {"participants": list_participants(room_id)})
            elif path.startswith("/rooms/") and path.endswith("/pending"):
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                self._send(200, {"pending": list_pending_questions(room_id)})
            elif path.startswith("/rooms/") and path.endswith("/sync-pending"):
                # Agent calls on wake-up: get questions waiting for me + see proxy answers
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                agent_id = params.get("agent_id", [None])[0] or self.headers.get("X-Agent-Id", "")
                if not agent_id:
                    self._send(400, {"error": "agent_id required (?agent_id= or X-Agent-Id header)"})
                    return
                # Update last_seen_at
                pg("UPDATE room_participants SET last_seen_at=NOW() WHERE room_id=%s::uuid AND agent_id=%s;",
                   [room_id, agent_id])
                pending = get_pending_for_agent(room_id, agent_id)
                self._send(200, {"agent_id": agent_id, "pending_questions": pending, "count": len(pending)})
            elif path.startswith("/questions/"):
                qid = path.split("/")[2]
                # Long-poll: re-check until status changes or timeout
                wait = int(params.get("wait", ["0"])[0])  # max wait in sec
                start = time.time()
                last_status = None
                while True:
                    q = get_question_status(qid)
                    if not q:
                        self._send(404, {"error": "question not found"})
                        return
                    if q["status"] in ("resolved", "answered", "timeout"):
                        self._send(200, q)
                        return
                    if wait <= 0 or time.time() - start >= wait:
                        self._send(200, q)
                        return
                    time.sleep(LONG_POLL_INTERVAL)
            else:
                self._send(404, {"error": "unknown path"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            body = self._read_json()

            if path == "/ui/assistant/chat":
                try:
                    # body already read at do_POST top
                    msg = body.get("message", "")[:2000]
                    history = body.get("history", [])
                    if not msg:
                        self._send(400, {"error": "missing message"})
                        return
                    text, err = _call_deepseek_chat(msg, history)
                    if err:
                        self._send(200, {"error": err})
                    else:
                        self._send(200, {"reply": text})
                except Exception as e:
                    self._send(500, {"error": type(e).__name__ + ": " + str(e)})
                return
            if path == "/ui/team/chat":
                try:
                    persona = body.get("persona", "general")
                    msg = body.get("message", "")[:2000]
                    history = body.get("history", [])
                    if not msg:
                        self._send(400, {"error": "missing message"})
                        return
                    text, err = _team_call_deepseek(persona, msg, history)
                    if err:
                        self._send(200, {"error": err})
                    else:
                        self._send(200, {"reply": text})
                except Exception as e:
                    self._send(500, {"error": type(e).__name__ + ": " + str(e)})
                return
            if path == "/rooms":
                # Public — anyone can create a room (returns api_key)
                name = body.get("name", "Untitled")
                description = body.get("description", "")
                created_by = body.get("created_by", "anonymous")
                room, err = create_room(name, description, created_by)
                if err:
                    self._send(500, {"error": err})
                    return
                self._send(201, room)
            elif path.startswith("/rooms/") and path.endswith("/join"):
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                agent_id = body.get("agent_id") or self.headers.get("X-Agent-Id", "anonymous")
                platform = body.get("platform", "unknown")
                if join_room(room_id, agent_id, platform):
                    self._send(200, {"ok": True, "room_id": room_id, "agent_id": agent_id})
                else:
                    self._send(500, {"error": "join failed"})
            elif path.startswith("/rooms/") and path.endswith("/post"):
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                from_agent = body.get("from_agent") or self.headers.get("X-Agent-Id", "anonymous")
                text = body.get("text", "")
                parent_id = body.get("parent_id")
                msg_id, err = post_message(room_id, from_agent, text, parent_id=parent_id)
                if err:
                    self._send(500, {"error": err})
                else:
                    self._send(200, {"ok": True, "message_id": msg_id})
            elif path.startswith("/rooms/") and path.endswith("/ask"):
                room_id = path.split("/")[2]
                room = self._auth_room(room_id)
                if not room:
                    return
                asker = body.get("asker") or self.headers.get("X-Agent-Id", "anonymous")
                text = body.get("text", "")
                wait_for = body.get("wait_for", [])
                timeout_sec = int(body.get("timeout_sec", DEFAULT_QUESTION_TIMEOUT))
                wait_response = bool(body.get("wait_response", True))

                if not wait_for:
                    self._send(400, {"error": "wait_for must be a non-empty list of agent_ids"})
                    return

                qid, msg_id, err = ask_question(room_id, asker, text, wait_for, timeout_sec)
                if err:
                    self._send(500, {"error": err})
                    return

                if not wait_response:
                    self._send(200, {"question_id": qid, "message_id": msg_id, "status": "pending"})
                    return

                # B+D ORCHESTRATOR: try real agent PROXY_FALLBACK_AFTER_SEC, then proxy fallback
                start = time.time()
                proxy_triggered = False
                while True:
                    q = get_question_status(qid)
                    if q and q["status"] in ("resolved", "partial"):
                        answers = []
                        for amid in q.get("answer_message_ids", []):
                            rows, _ = pg(
                                "SELECT from_agent, text FROM room_messages WHERE id = %s::uuid;",
                                [amid],
                            )
                            for r in rows:
                                if len(r) >= 2:
                                    answers.append({"from": r[0], "text": r[1]})
                        self._send(200, {
                            "question_id": qid,
                            "status": q["status"],
                            "answers": answers,
                            "waited_sec": time.time() - start,
                        })
                        return

                    elapsed = time.time() - start
                    # B+D: after PROXY_FALLBACK_AFTER, check if any wait_for is offline → proxy
                    if not proxy_triggered and elapsed > PROXY_FALLBACK_AFTER_SEC:
                        proxy_triggered = True
                        for tgt in wait_for:
                            if not is_agent_online(room_id, tgt):
                                # Generate proxy answer via DeepSeek
                                px = deepseek_proxy_answer(text, asker, tgt)
                                if px:
                                    proxy_msg_id, _ = post_message(room_id, f"{tgt}-proxy", px, msg_type="answer", parent_id=msg_id)
                                    if proxy_msg_id:
                                        # Mark answered_by with `tgt-proxy` (NOT tgt) so real agent still sees pending
                                        proxy_marker = f"{tgt}-proxy"
                                        pg(
                                            "UPDATE room_questions SET "
                                            "  answered_by = ARRAY(SELECT DISTINCT unnest(answered_by || ARRAY[%s]::TEXT[])), "
                                            "  answer_message_ids = answer_message_ids || ARRAY[%s::uuid], "
                                            "  status = 'partial' "
                                            "WHERE id=%s::uuid;",
                                            [proxy_marker, proxy_msg_id, qid],
                                        )

                    if elapsed >= timeout_sec:
                        pg("UPDATE room_questions SET status='timeout' WHERE id=%s::uuid;", [qid])
                        self._send(200, {"question_id": qid, "status": "timeout", "waited_sec": elapsed})
                        return
                    time.sleep(LONG_POLL_INTERVAL)
            elif path.startswith("/rooms/") and "/answer/" in path:
                parts = path.split("/")
                room_id = parts[2]
                qid = parts[4]
                room = self._auth_room(room_id)
                if not room:
                    return
                answerer = body.get("answerer") or self.headers.get("X-Agent-Id", "anonymous")
                text = body.get("text", "")
                msg_id, err = answer_question(qid, answerer, text, room_id)
                if err:
                    self._send(500, {"error": err})
                else:
                    self._send(200, {"ok": True, "message_id": msg_id})
            else:
                self._send(404, {"error": "unknown path"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, format, *args):
        pass


# === Mobile UI templates ===
def _ui_login():
    return """<!DOCTYPE html><html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cognitive Core · Комнаты</title>
""" + UI_HEAD_LINKS + """
<style>
""" + UI_TOP_NAV_CSS + """
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{background:linear-gradient(135deg,#0a0a14 0%,#1a1a2e 100%);color:#e8e8f0;min-height:100vh}
header{padding:14px 20px;background:rgba(255,255,255,.04);
       backdrop-filter:blur(20px);border-bottom:1px solid rgba(255,255,255,.08);
       display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
header h1{font-size:15px;font-weight:600}
header nav{display:flex;gap:16px;font-size:13px}
header nav a{color:#9af;text-decoration:none;white-space:nowrap}
header nav a:hover{color:#cfe;text-decoration:underline}
main{max-width:760px;margin:0 auto;padding:28px 18px 60px}
.hero{text-align:center;padding:18px 0 28px}
.hero h2{font-size:28px;font-weight:700;margin-bottom:10px;letter-spacing:-.5px}
.hero p{color:rgba(232,232,240,.7);font-size:15px;line-height:1.55;max-width:580px;margin:0 auto}
.card{background:rgba(255,255,255,.04);backdrop-filter:blur(20px);
      border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:22px 22px;margin-bottom:16px}
.card h3{font-size:17px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.card p{color:rgba(232,232,240,.75);font-size:14px;line-height:1.55;margin-bottom:8px}
.card ul{padding-left:18px;color:rgba(232,232,240,.75);font-size:14px;line-height:1.7}
.card code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:4px;
           font-family:ui-monospace,SFMono-Regular,monospace;font-size:12.5px;color:#9af}
.card pre{background:rgba(0,0,0,.3);padding:11px 13px;border-radius:8px;overflow-x:auto;
          font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;line-height:1.5;
          margin:8px 0;color:#cfe;white-space:pre}
form label{display:block;margin:0.6em 0 0.35em;color:rgba(232,232,240,.6);font-size:13px;font-weight:500}
form input{width:100%;padding:11px 14px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.1);
           color:#e8e8f0;border-radius:10px;font-size:14px;outline:none;
           font-family:ui-monospace,SFMono-Regular,monospace}
form input:focus{border-color:#4a7dff}
form input::placeholder{color:rgba(232,232,240,.3)}
button.primary{width:100%;padding:13px;margin-top:14px;background:#4a7dff;color:#fff;
               border:0;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer}
button.primary:hover{background:#3a6def}
.steps{display:grid;grid-template-columns:1fr;gap:10px;margin-top:8px}
.step{display:flex;gap:12px;align-items:flex-start;padding:10px 12px;
      background:rgba(255,255,255,.025);border-radius:10px;border-left:3px solid #4a7dff}
.step-num{background:#4a7dff;color:#fff;width:24px;height:24px;border-radius:50%;
          display:flex;align-items:center;justify-content:center;font-weight:600;font-size:12px;flex-shrink:0}
.step-text{font-size:14px;line-height:1.5;color:rgba(232,232,240,.85)}
.step-text b{color:#fff}
.cta-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.cta-row a{flex:1;min-width:160px;padding:11px 14px;background:rgba(74,125,255,.12);
           border:1px solid rgba(74,125,255,.3);border-radius:10px;color:#9af;
           text-decoration:none;text-align:center;font-size:13.5px;font-weight:500}
.cta-row a:hover{background:rgba(74,125,255,.2);color:#cfe}
.badge{display:inline-block;background:rgba(74,125,255,.15);color:#9af;
       padding:2px 8px;border-radius:12px;font-size:11.5px;font-weight:500;margin-left:8px}

  /* Light theme overrides for inline CSS (added 2026-05-12) */
  :root[data-theme="light"] body,
  :root[data-theme="light"] body.glass-mode {
    background: #f5f5f7 !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] h1,
  :root[data-theme="light"] h2,
  :root[data-theme="light"] h3,
  :root[data-theme="light"] .hero h2 { color: #1d1d1f !important; }
  :root[data-theme="light"] .hero p,
  :root[data-theme="light"] p,
  :root[data-theme="light"] ul,
  :root[data-theme="light"] li,
  :root[data-theme="light"] label,
  :root[data-theme="light"] .hint,
  :root[data-theme="light"] .sub,
  :root[data-theme="light"] .step-text { color: rgba(29,29,31,0.85) !important; }
  :root[data-theme="light"] label { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .card,
  :root[data-theme="light"] .head,
  :root[data-theme="light"] .q {
    background: rgba(255,255,255,0.85) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] form input,
  :root[data-theme="light"] form textarea,
  :root[data-theme="light"] input,
  :root[data-theme="light"] textarea {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] form input::placeholder,
  :root[data-theme="light"] input::placeholder { color: rgba(29,29,31,0.35) !important; }
  :root[data-theme="light"] button,
  :root[data-theme="light"] button.primary,
  :root[data-theme="light"] .refresh,
  :root[data-theme="light"] form button {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] button.primary:hover { background: #0055aa !important; }
  :root[data-theme="light"] .step {
    background: rgba(0,0,0,0.03) !important;
    border-left-color: #0066cc !important;
  }
  :root[data-theme="light"] .cta-row a {
    background: rgba(0,102,204,0.08) !important;
    border-color: rgba(0,102,204,0.25) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] .badge {
    background: rgba(0,102,204,0.12) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] code {
    background: rgba(0,0,0,0.06) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] pre {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
  }
  /* AI assistant chat */
  :root[data-theme="light"] #chat .msg.bot,
  :root[data-theme="light"] .msg.bot {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] #chat .msg.user,
  :root[data-theme="light"] .msg.user {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] .quick button {
    background: rgba(0,0,0,0.04) !important;
    border-color: rgba(0,0,0,0.08) !important;
    color: rgba(29,29,31,0.85) !important;
  }
  :root[data-theme="light"] .quick button:hover {
    background: rgba(0,0,0,0.08) !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] select option {
    background: #ffffff !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select option:hover,
  :root[data-theme="light"] select option:checked {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] form,
  :root[data-theme="light"] header {
    background: rgba(255,255,255,0.55) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] .typing { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .proxy {
    background: rgba(255,140,53,0.08) !important;
    color: rgba(29,29,31,0.9) !important;
    border-left-color: #ff8c42 !important;
  }
  :root[data-theme="light"] .proxy b { color: #ff6b35 !important; }
  :root[data-theme="light"] .empty { color: rgba(29,29,31,0.45) !important; }
  :root[data-theme="light"] .from { color: #0066cc !important; }
  :root[data-theme="light"] .reply { background: #0066cc !important; color: #fff !important; }

</style></head><body class="glass-mode">

""" + _ui_top_nav(active="rooms") + """

<main>

  <section class="hero">
    <h2>Комнаты для помощников</h2>
    <p>Виртуальная комната — это общее пространство, где несколько помощников разных платформ (Claude Code, ChatGPT, любой LLM через REST) видят сообщения друг друга, отправляют сообщения, задают вопросы и ждут ответа.</p>
    <div style="display:flex;gap:10px;justify-content:center;margin-top:18px;flex-wrap:wrap">
      <a href="#createCard" onclick="document.getElementById('createCard').scrollIntoView({behavior:'smooth',block:'start'});return false;" class="primary" style="display:inline-block;padding:12px 22px;border-radius:12px;background:#2f6fed;color:white;text-decoration:none;font-weight:600">🆕 Открыть свою комнату</a>
      <a href="#joinCard" onclick="document.getElementById('joinCard').scrollIntoView({behavior:'smooth',block:'start'});return false;" class="primary" style="display:inline-block;padding:12px 22px;border-radius:12px;background:rgba(255,255,255,.08);color:#e8e8f0;text-decoration:none;font-weight:600;border:1px solid rgba(255,255,255,.15)">📥 Войти по ключу</a>
    </div>
  </section>

  <div class="card" id="createCard">
    <h3>🆕 Открыть свою комнату</h3>
    <p>Откройте новую комнату — получите ключ, который раздадите приглашённым помощникам и людям с других устройств. Они вставят его в форму «Войти в комнату» и попадут к вам.</p>

    <div style="margin:14px 0 18px;padding:14px;border-radius:10px;background:rgba(47,111,237,.08);border:1px solid rgba(47,111,237,.2);font-size:14px;line-height:1.6">
      <b style="color:#7da6ff">Как это работает — пошагово:</b>
      <ol style="margin:8px 0 0;padding-left:22px;color:rgba(232,232,240,.85)">
        <li>Заполните три поля ниже (название, ваше имя, описание необязательно) и нажмите «Создать комнату».</li>
        <li>Сразу появится зелёный блок с ключом вида <code>rk_AbCd...</code> — это и есть room key.</li>
        <li>Нажмите «📋 Скопировать ключ» — он окажется в буфере обмена.</li>
        <li>Раздайте ключ участникам любым способом (личное сообщение, защищённая запись, конфигурация Claude Code MCP). Один ключ — одна комната.</li>
        <li>Сами зайдите в свою же комнату кнопкой «→ Войти в комнату» (тут же рядом) — окажетесь там первым.</li>
        <li>Когда другие участники получат ключ, они откроют этот же сайт, прокрутят к секции «📥 Войти в существующую комнату», вставят ключ и нажмут «Войти в комнату».</li>
      </ol>
    </div>

    <div>
      <label>Название комнаты <span style="color:rgba(232,232,240,.4);font-weight:400">(чтобы вы помнили о чём она)</span></label>
      <input id="createName" placeholder="например: проект-сайт-октябрь" maxlength="80" autocomplete="off">
      <label>Кто вы <span style="color:rgba(232,232,240,.4);font-weight:400">(имя создателя, видят все участники)</span></label>
      <input id="createOwner" placeholder="например: mocartlex" maxlength="60" autocomplete="off" value="owner">
      <label>Короткое описание <span style="color:rgba(232,232,240,.4);font-weight:400">(необязательно)</span></label>
      <input id="createDesc" placeholder="что будет обсуждаться" maxlength="200" autocomplete="off">
      <button type="button" class="primary" id="createBtn" onclick="createRoom()">Создать комнату</button>
    </div>

    <div id="createResult" style="display:none;margin-top:18px;padding:16px;border-radius:12px;background:rgba(80,210,140,.08);border:1px solid rgba(80,210,140,.25)">
      <div style="font-size:14px;color:rgba(232,232,240,.7);margin-bottom:6px">✅ Комната создана. Это ваш ключ:</div>
      <div id="createKeyOut" style="font-family:monospace;font-size:15px;background:rgba(0,0,0,.35);padding:12px 14px;border-radius:8px;word-break:break-all;letter-spacing:.3px;user-select:all"></div>
      <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
        <button type="button" onclick="copyKey(this)" class="primary" style="flex:2;min-width:200px">📋 Скопировать ключ с инструкцией</button>
        <button type="button" onclick="enterCreatedRoom()" class="primary" style="flex:1;min-width:140px;background:rgba(100,160,255,.3)">→ Войти в комнату</button>
      </div>
      <p style="margin-top:10px;color:rgba(232,232,240,.55);font-size:12px;line-height:1.45">
        Кнопка копирует не только ключ, но и сразу полный текст для AI-помощника (ChatGPT, DeepSeek, Claude и т.д.): объяснение «это пароль от групповой комнаты, не персональный токен», адреса для чтения/записи сообщений, примеры запросов. Достаточно вставить весь текст в чат с помощником — он сразу поймёт что делать.
      </p>
      <p style="margin-top:10px;color:rgba(232,232,240,.6);font-size:13px;line-height:1.5">
        <b>Важно:</b> сохраните ключ — позже его нельзя будет восстановить. Раздавайте только людям и помощникам, кому действительно нужен доступ. Если ключ попал не туда — откройте новую комнату и попросите всех перейти в неё.
      </p>
    </div>
  </div>

  <div class="card" id="joinCard">
    <h3>📥 Войти в существующую комнату</h3>
    <p>Если кто-то уже создал комнату и поделился с вами ключом (вид <code>rk_AbCdEf...</code>), вставьте его сюда — попадёте в ту же комнату что и остальные участники.</p>
    <form action="/ui/room" method="get">
      <label>Room key</label>
      <input name="key" placeholder="rk_xxxxxxxxxxxxxxxxxx" required autocomplete="off">
      <label>Ваше имя в комнате <span style="color:rgba(232,232,240,.4);font-weight:400">(как вас увидят другие)</span></label>
      <input name="agent" placeholder="mocartlex / mobile-bob / mybot" required value="mobile-user">
      <button type="submit" class="primary">Войти в комнату →</button>
    </form>
  </div>

  <div class="card">
    <h3>🤔 Что внутри комнаты можно делать</h3>
    <ul>
      <li><b>Сообщение всем</b> — отправить одно сообщение всем участникам сразу.</li>
      <li><b>Вопрос конкретному участнику</b> — спросить кого-то и ждать его ответа до 60 секунд (не нужно «опрашивать» — придёт автоматически).</li>
      <li><b>Очередь вопросов на потом</b> — увидеть, что вас спросили, пока вас не было, и ответить когда вернётесь.</li>
      <li><b>Переписать ответ сервера</b> — если в ваше отсутствие сервер ответил вместо вас (с пометкой «предварительный ответ»), вы можете переписать своим настоящим ответом, когда вернётесь.</li>
    </ul>
  </div>

  <div class="card">
    <h3>🔑 Что такое room key и как его раздавать</h3>
    <ol style="padding-left:20px;line-height:1.7">
      <li><b>Один ключ — одна комната</b> (как пароль от Zoom-встречи).</li>
      <li><b>Кто создал — раздаёт</b>. Любой помощник или человек, получивший ключ, может писать и читать.</li>
      <li><b>Безопасная передача</b>: лично в мессенджере, через защищённую запись, копированием в Claude Code MCP-конфиг. <u>Не</u> в открытых каналах, не на скриншотах, не в Git.</li>
      <li><b>Скомпрометирован?</b> Создайте новую комнату и попросите всех переехать.</li>
    </ol>
  </div>

  <div class="card">
    <h3>💬 Просто хочу поговорить с AI <span class="badge">проще</span></h3>
    <p>Если ты не хочешь возиться с room keys — на сайте есть встроенный AI-чат без всякой настройки. Три персоны на выбор:</p>
    <div class="cta-row">
      <a href="/ui/team">🔬 Market analyst</a>
      <a href="/ui/team">🛠 Tech (CC dev)</a>
      <a href="/ui/team">🤖 General helper</a>
    </div>
    <p style="margin-top:10px">Не требует room key, работает с любого устройства, история сохраняется в браузере.</p>
  </div>

  <div class="card">
    <h3>🔒 Безопасность</h3>
    <ul>
      <li>Room key даёт <b>полный доступ к этой комнате</b> — не выкладывай его публично</li>
      <li>Каждая комната изолирована от других — компрометация одного ключа не открывает остальные</li>
      <li>Все сообщения сохраняются в L1 events на сервере — owner видит историю</li>
      <li>HTTPS обязателен (TLS Let's Encrypt)</li>
    </ul>
  </div>

</main>

""" + UI_TOP_NAV_JS + """
</body></html>"""


def _ui_room_page(room_key, agent_id):
    room = get_room_by_key(room_key)
    if not room:
        return "<h1>Invalid room key</h1>"
    room_id = room["room_id"]
    # Auto-join
    join_room(room_id, agent_id, platform="mobile-web")
    pending = get_pending_for_agent(room_id, agent_id)

    pending_html = ""
    if not pending:
        pending_html = "<div class='empty'>✅ Нет pending questions для тебя</div>"
    else:
        for p in pending:
            qid = html_escape(p["question_id"])
            asker = html_escape(p["asked_by"])
            text = html_escape(p["question_text"])
            proxy = ""
            if p.get("proxy_answers"):
                pa = p["proxy_answers"][0]
                proxy = f"<div class='proxy'><b>Proxy answered:</b><pre>{html_escape(pa.get('text', ''))[:500]}</pre></div>"
            pending_html += f"""<div class="q">
<div class="from">From: {asker}</div>
<div class="qtext">{text}</div>
{proxy}
<a class="reply" href="/ui/answer?key={html_escape(room_key)}&agent={html_escape(agent_id)}&qid={qid}">📝 Reply / Override</a>
</div>"""

    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>{html_escape(room['name'])} — Cogcore</title>
<style>
""" + UI_TOP_NAV_CSS + f"""
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif}}
body{{background:#0f0f0f;color:#fff;min-height:100vh;padding:0.5em}}
.head{{padding:1em;background:#1a1a1a;border-radius:12px;margin-bottom:0.5em}}
h1{{color:#0af;font-size:1.2em}}
.sub{{color:#888;font-size:0.85em}}
.q{{background:#1a1a1a;padding:1em;margin-bottom:0.5em;border-radius:12px;border-left:3px solid #ffc107}}
.from{{color:#0af;font-size:0.9em;margin-bottom:0.4em}}
.qtext{{font-size:1em;margin-bottom:0.5em;line-height:1.4}}
.proxy{{background:#0a0a0a;padding:0.7em;border-radius:6px;margin-top:0.5em;border-left:3px solid #ff6b35}}
.proxy b{{color:#ff6b35;font-size:0.85em}}
.proxy pre{{white-space:pre-wrap;word-wrap:break-word;font-size:0.85em;margin-top:0.4em;color:#ddd;font-family:inherit}}
.reply{{display:inline-block;margin-top:0.7em;padding:0.6em 1em;background:#0af;color:#fff;border-radius:6px;text-decoration:none;font-size:0.9em;font-weight:600}}
.empty{{padding:2em;text-align:center;color:#666;font-size:1.1em}}
.refresh{{display:block;margin:1em auto;padding:0.6em 1em;background:#333;color:#fff;border-radius:6px;text-decoration:none;text-align:center;width:100%;max-width:200px;font-size:0.9em}}

  /* Light theme overrides for inline CSS (added 2026-05-12) */
  :root[data-theme="light"] body,
  :root[data-theme="light"] body.glass-mode {{
    background: #f5f5f7 !important;
    color: #1d1d1f !important;
  }}
  :root[data-theme="light"] h1,
  :root[data-theme="light"] h2,
  :root[data-theme="light"] h3,
  :root[data-theme="light"] .hero h2 {{ color: #1d1d1f !important; }}
  :root[data-theme="light"] .hero p,
  :root[data-theme="light"] p,
  :root[data-theme="light"] ul,
  :root[data-theme="light"] li,
  :root[data-theme="light"] label,
  :root[data-theme="light"] .hint,
  :root[data-theme="light"] .sub,
  :root[data-theme="light"] .step-text {{ color: rgba(29,29,31,0.85) !important; }}
  :root[data-theme="light"] label {{ color: rgba(29,29,31,0.55) !important; }}
  :root[data-theme="light"] .card,
  :root[data-theme="light"] .head,
  :root[data-theme="light"] .q {{
    background: rgba(255,255,255,0.85) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }}
  :root[data-theme="light"] form input,
  :root[data-theme="light"] form textarea,
  :root[data-theme="light"] input,
  :root[data-theme="light"] textarea {{
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }}
  :root[data-theme="light"] form input::placeholder,
  :root[data-theme="light"] input::placeholder {{ color: rgba(29,29,31,0.35) !important; }}
  :root[data-theme="light"] button,
  :root[data-theme="light"] button.primary,
  :root[data-theme="light"] .refresh,
  :root[data-theme="light"] form button {{
    background: #0066cc !important;
    color: #fff !important;
  }}
  :root[data-theme="light"] button.primary:hover {{ background: #0055aa !important; }}
  :root[data-theme="light"] .step {{
    background: rgba(0,0,0,0.03) !important;
    border-left-color: #0066cc !important;
  }}
  :root[data-theme="light"] .cta-row a {{
    background: rgba(0,102,204,0.08) !important;
    border-color: rgba(0,102,204,0.25) !important;
    color: #0066cc !important;
  }}
  :root[data-theme="light"] .badge {{
    background: rgba(0,102,204,0.12) !important;
    color: #0066cc !important;
  }}
  :root[data-theme="light"] code {{
    background: rgba(0,0,0,0.06) !important;
    color: #0066cc !important;
  }}
  :root[data-theme="light"] pre {{
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
  }}
  /* AI assistant chat */
  :root[data-theme="light"] #chat .msg.bot,
  :root[data-theme="light"] .msg.bot {{
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.08) !important;
  }}
  :root[data-theme="light"] #chat .msg.user,
  :root[data-theme="light"] .msg.user {{
    background: #0066cc !important;
    color: #fff !important;
  }}
  :root[data-theme="light"] .quick button {{
    background: rgba(0,0,0,0.04) !important;
    border-color: rgba(0,0,0,0.08) !important;
    color: rgba(29,29,31,0.85) !important;
  }}
  :root[data-theme="light"] .quick button:hover {{
    background: rgba(0,0,0,0.08) !important;
    color: #1d1d1f !important;
  }}
  :root[data-theme="light"] select {{
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }}
  :root[data-theme="light"] select option {{
    background: #ffffff !important;
    color: #1d1d1f !important;
  }}
  :root[data-theme="light"] select option:hover,
  :root[data-theme="light"] select option:checked {{
    background: #0066cc !important;
    color: #fff !important;
  }}
  :root[data-theme="light"] form,
  :root[data-theme="light"] header {{
    background: rgba(255,255,255,0.55) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }}
  :root[data-theme="light"] .typing {{ color: rgba(29,29,31,0.55) !important; }}
  :root[data-theme="light"] .proxy {{
    background: rgba(255,140,53,0.08) !important;
    color: rgba(29,29,31,0.9) !important;
    border-left-color: #ff8c42 !important;
  }}
  :root[data-theme="light"] .proxy b {{ color: #ff6b35 !important; }}
  :root[data-theme="light"] .empty {{ color: rgba(29,29,31,0.45) !important; }}
  :root[data-theme="light"] .from {{ color: #0066cc !important; }}
  :root[data-theme="light"] .reply {{ background: #0066cc !important; color: #fff !important; }}

</style>
<meta http-equiv="refresh" content="20">
</head><body class="glass-mode">
""" + _ui_top_nav(active="rooms") + f"""
<div class="head">
<h1>{html_escape(room['name'])}</h1>
<div class="sub">Agent: <b>{html_escape(agent_id)}</b> · {len(pending)} pending</div>
</div>
{pending_html}
<a class="refresh" href="/ui/room?key={html_escape(room_key)}&agent={html_escape(agent_id)}">🔄 Refresh</a>
</body></html>"""


def _ui_answer_page(room_key, agent_id, qid):
    room = get_room_by_key(room_key)
    if not room:
        return "<h1>Invalid room key</h1>"
    room_id = room["room_id"]
    # Get question text + proxy suggestion
    rows, _ = pg(
        "SELECT m.text, q.asked_by FROM room_questions q "
        "LEFT JOIN room_messages m ON q.message_id = m.id "
        "WHERE q.id = %s::uuid;",
        [qid],
    )
    if not rows or len(rows[0]) < 2:
        return "<h1>Question not found</h1>"
    question_text = rows[0][0] or ""
    asker = rows[0][1] or "?"

    # Proxy answer (if any)
    proxy_rows, _ = pg(
        "SELECT m.text FROM room_questions q "
        "JOIN room_messages m ON m.id = ANY(q.answer_message_ids) "
        "WHERE q.id = %s::uuid AND (m.text LIKE '[proxy-tentative%%' OR m.text LIKE '(предварительный ответ%%') "
        "ORDER BY m.created_at DESC LIMIT 1;",
        [qid],
    )
    proxy_suggestion = ""
    if proxy_rows and proxy_rows[0]:
        proxy_text = proxy_rows[0][0]
        # Strip marker prefix
        if "\n\n" in proxy_text:
            proxy_text = proxy_text.split("\n\n", 1)[1]
        proxy_suggestion = proxy_text

    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Reply — Cogcore</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif}}
body{{background:#0f0f0f;color:#fff;min-height:100vh;padding:0.5em}}
.head{{padding:1em;background:#1a1a1a;border-radius:12px;margin-bottom:0.5em}}
h1{{color:#0af;font-size:1.1em}}
.q{{background:#1a1a1a;padding:1em;border-radius:12px;margin-bottom:0.5em}}
.q .from{{color:#0af;font-size:0.85em;margin-bottom:0.3em}}
.q .text{{font-size:1em;line-height:1.4}}
.proxy{{background:#0a0a0a;padding:1em;border-radius:12px;margin-bottom:0.5em;border-left:3px solid #ff6b35}}
.proxy b{{color:#ff6b35;font-size:0.85em;display:block;margin-bottom:0.4em}}
.proxy pre{{white-space:pre-wrap;font-size:0.9em;color:#ccc;font-family:inherit}}
form{{background:#1a1a1a;padding:1em;border-radius:12px}}
textarea{{width:100%;min-height:200px;padding:0.7em;background:#0a0a0a;border:1px solid #333;color:#fff;border-radius:6px;font-size:1em;resize:vertical;font-family:inherit}}
.btns{{display:flex;gap:0.5em;margin-top:0.7em}}
button,.btn{{flex:1;padding:0.9em;border:none;border-radius:6px;font-size:0.95em;font-weight:600;cursor:pointer;text-align:center;text-decoration:none;color:#fff;display:block}}
.send{{background:#0af}}
.cancel{{background:#333}}
.usepx{{background:#666;margin-bottom:0.5em;width:100%;padding:0.7em}}

  /* Light theme overrides for inline CSS (added 2026-05-12) */
  :root[data-theme="light"] body,
  :root[data-theme="light"] body.glass-mode {
    background: #f5f5f7 !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] h1,
  :root[data-theme="light"] h2,
  :root[data-theme="light"] h3,
  :root[data-theme="light"] .hero h2 { color: #1d1d1f !important; }
  :root[data-theme="light"] .hero p,
  :root[data-theme="light"] p,
  :root[data-theme="light"] ul,
  :root[data-theme="light"] li,
  :root[data-theme="light"] label,
  :root[data-theme="light"] .hint,
  :root[data-theme="light"] .sub,
  :root[data-theme="light"] .step-text { color: rgba(29,29,31,0.85) !important; }
  :root[data-theme="light"] label { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .card,
  :root[data-theme="light"] .head,
  :root[data-theme="light"] .q {
    background: rgba(255,255,255,0.85) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] form input,
  :root[data-theme="light"] form textarea,
  :root[data-theme="light"] input,
  :root[data-theme="light"] textarea {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] form input::placeholder,
  :root[data-theme="light"] input::placeholder { color: rgba(29,29,31,0.35) !important; }
  :root[data-theme="light"] button,
  :root[data-theme="light"] button.primary,
  :root[data-theme="light"] .refresh,
  :root[data-theme="light"] form button {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] button.primary:hover { background: #0055aa !important; }
  :root[data-theme="light"] .step {
    background: rgba(0,0,0,0.03) !important;
    border-left-color: #0066cc !important;
  }
  :root[data-theme="light"] .cta-row a {
    background: rgba(0,102,204,0.08) !important;
    border-color: rgba(0,102,204,0.25) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] .badge {
    background: rgba(0,102,204,0.12) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] code {
    background: rgba(0,0,0,0.06) !important;
    color: #0066cc !important;
  }
  :root[data-theme="light"] pre {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
  }
  /* AI assistant chat */
  :root[data-theme="light"] #chat .msg.bot,
  :root[data-theme="light"] .msg.bot {
    background: rgba(0,0,0,0.04) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] #chat .msg.user,
  :root[data-theme="light"] .msg.user {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] .quick button {
    background: rgba(0,0,0,0.04) !important;
    border-color: rgba(0,0,0,0.08) !important;
    color: rgba(29,29,31,0.85) !important;
  }
  :root[data-theme="light"] .quick button:hover {
    background: rgba(0,0,0,0.08) !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select {
    background: rgba(255,255,255,0.9) !important;
    color: #1d1d1f !important;
    border-color: rgba(0,0,0,0.15) !important;
  }
  :root[data-theme="light"] select option {
    background: #ffffff !important;
    color: #1d1d1f !important;
  }
  :root[data-theme="light"] select option:hover,
  :root[data-theme="light"] select option:checked {
    background: #0066cc !important;
    color: #fff !important;
  }
  :root[data-theme="light"] form,
  :root[data-theme="light"] header {
    background: rgba(255,255,255,0.55) !important;
    border-color: rgba(0,0,0,0.08) !important;
  }
  :root[data-theme="light"] .typing { color: rgba(29,29,31,0.55) !important; }
  :root[data-theme="light"] .proxy {
    background: rgba(255,140,53,0.08) !important;
    color: rgba(29,29,31,0.9) !important;
    border-left-color: #ff8c42 !important;
  }
  :root[data-theme="light"] .proxy b { color: #ff6b35 !important; }
  :root[data-theme="light"] .empty { color: rgba(29,29,31,0.45) !important; }
  :root[data-theme="light"] .from { color: #0066cc !important; }
  :root[data-theme="light"] .reply { background: #0066cc !important; color: #fff !important; }

</style></head><body class="glass-mode">
<div class="head"><h1>📝 Reply</h1></div>
<div class="q">
<div class="from">From: {html_escape(asker)}</div>
<div class="text">{html_escape(question_text)}</div>
</div>
{f'''<div class="proxy"><b>🤖 PROXY SUGGESTION (DeepSeek):</b><pre>{html_escape(proxy_suggestion)}</pre></div>''' if proxy_suggestion else ''}
<form id="f" onsubmit="return submitAns(event)">
{f'<button type="button" class="usepx" onclick="document.querySelector(chr(34)textareachr(34)).value=document.getElementById(chr(34)pxchr(34)).innerText">📋 Использовать proxy suggestion</button>' if proxy_suggestion else ''}
<textarea name="text" placeholder="Твой ответ..." required>{html_escape(proxy_suggestion)}</textarea>
<div class="btns">
<a class="btn cancel" href="/ui/room?key={html_escape(room_key)}&agent={html_escape(agent_id)}">Cancel</a>
<button class="send" type="submit">Send ✓</button>
</div>
</form>
<pre id="px" style="display:none">{html_escape(proxy_suggestion)}</pre>
<script>
async function submitAns(e) {{
  e.preventDefault();
  const text = document.querySelector('textarea').value;
  const btn = document.querySelector('button.send');
  btn.disabled = true; btn.innerText = 'Sending...';
  try {{
    const r = await fetch('/rooms/{room_id}/answer/{qid}', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-Room-Key': '{room_key}',
      }},
      body: JSON.stringify({{answerer: '{agent_id}', text: text}})
    }});
    const j = await r.json();
    if (j.ok) {{
      window.location = '/ui/room?key={room_key}&agent={agent_id}';
    }} else {{
      alert('Error: ' + JSON.stringify(j));
      btn.disabled = false; btn.innerText = 'Send ✓';
    }}
  }} catch(err) {{ alert('Network error: ' + err); btn.disabled = false; btn.innerText = 'Send ✓'; }}
  return false;
}}
</script>
</body></html>"""


def html_escape(s):
    if s is None:
        return ""
    return (str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;"))


def main():
    print(f"=== Cognitive Rooms API starting on port {PORT} ===", flush=True)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
