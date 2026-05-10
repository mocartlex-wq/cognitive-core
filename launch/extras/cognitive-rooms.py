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
        # Prefer env (container deploy); fall back to docker derivation (host deploy).
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or ""
        if not dsn:
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
    # Prefer env (container deploy); fall back to docker (host deploy).
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return k
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
        return f"[proxy-tentative for {target_agent} may-override]\n\n{text}"
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
                if len(ar) >= 2 and "[proxy-tentative" in ar[1]:
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

            if path == "/health":
                self._send(200, {"status": "ok", "service": "cogcore-rooms"})
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
    return """<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Cogcore Rooms</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif}
body{background:#0f0f0f;color:#fff;min-height:100vh;padding:1em;display:flex;flex-direction:column;justify-content:center;align-items:center}
.card{background:#1a1a1a;padding:1.5em;border-radius:12px;width:100%;max-width:400px}
h1{color:#0af;margin-bottom:1em;font-size:1.4em}
label{display:block;margin:0.5em 0 0.3em;color:#aaa;font-size:0.9em}
input{width:100%;padding:0.7em;background:#0a0a0a;border:1px solid #333;color:#fff;border-radius:6px;font-size:1em}
button{width:100%;padding:0.9em;margin-top:1em;background:#0af;color:#fff;border:none;border-radius:6px;font-size:1em;font-weight:600}
.hint{color:#666;font-size:0.85em;margin-top:1em;line-height:1.4}
</style></head><body>
<div class="card">
<h1>🚪 Cogcore Rooms</h1>
<form action="/ui/room" method="get">
<label>Room key (rk_...)</label>
<input name="key" placeholder="rk_xxxxx" required>
<label>Your agent ID</label>
<input name="agent" placeholder="mobile-bob" required value="mobile-user">
<button type="submit">Open room</button>
</form>
<p class="hint">Mobile quick-reply UI для answering pending room questions. Получи room key от owner.</p>
</div></body></html>"""


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
</style>
<meta http-equiv="refresh" content="20">
</head><body>
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
        "WHERE q.id = %s::uuid AND m.text LIKE '[proxy-tentative%%' "
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
</style></head><body>
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
