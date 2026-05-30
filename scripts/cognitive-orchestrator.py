#!/usr/bin/env python3
"""Cognitive Orchestrator — приёмщик/исполнители (2026-05-13).

User → Receiver → 1-3 Executors (parallel/sequential) → Synthesizer → User.

Endpoints (port 9099):
  POST /orchestrator/ask                — submit user request
  GET  /orchestrator/tasks/{id}         — poll task status
  GET  /orchestrator/tasks/{id}/stream  — SSE live updates
  GET  /orchestrator/tasks?limit=N      — recent tasks (for user)
  POST /orchestrator/capabilities       — register/update capability
  GET  /orchestrator/capabilities       — list capabilities
  POST /orchestrator/login              — issue user JWT token
  POST /orchestrator/heartbeat          — agent presence ping

  GET  /ui/ask                          — chat UI for user (mobile-friendly)
  GET  /manifest.json                   — PWA manifest
  GET  /sw.js                           — service worker for offline + push

Architecture:
  - HTTP layer (Flask-style with stdlib http.server + json)
  - Postgres via psycopg (host: cognitive_postgres in docker network)
  - DeepSeek for routing + synthesis
  - Existing /agents/message endpoint for executor handoff
  - SSE for live updates to UI
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from psycopg.rows import dict_row

# ─── Config ────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("ORCH_PORT", "9099"))
DB_DSN = os.environ.get(
    "ORCH_DB_DSN",
    "host=127.0.0.1 port=5433 dbname=cognitive_core user=cognitive password=cognitive",
)
COGCORE_BASE = os.environ.get("COGCORE_BASE", "http://127.0.0.1:8000")
COGCORE_INTERNAL = os.environ.get("COGCORE_INTERNAL", "https://mcp.xn----8sbwawqx4fza.xn--p1ai")
DS_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
ORCH_SIGN_KEY = os.environ.get(
    "ORCH_SIGN_KEY",
    "default-orch-key-please-set-in-env-32chars-min-ok-12345",
)

EXECUTOR_TIMEOUT_S = int(os.environ.get("ORCH_EXEC_TIMEOUT_S", "60"))
EXECUTOR_WAKE_WAIT_S = int(os.environ.get("ORCH_WAKE_WAIT_S", "5"))
ROUTING_TIMEOUT_S = 12
SYNTH_TIMEOUT_S = 25
MAX_CASCADE_DEPTH = 3
PROXY_AGENT_KEY_ENV_PREFIX = "AGENT_KEY_"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("orch")

# ─── DB pool (cheap: short-lived connections) ─────────────────────────────
_db_lock = threading.Lock()

def db():
    return psycopg.connect(DB_DSN, row_factory=dict_row)


# ─── SSE event bus (in-process pub/sub for task updates) ──────────────────
_subs_lock = threading.Lock()
_subscribers: dict[str, list[queue.Queue]] = {}  # task_id -> [queue, ...]


def publish_event(task_id: str, event: dict) -> None:
    """Publish a trace event for a task to all SSE subscribers."""
    event["at"] = datetime.now(timezone.utc).isoformat()
    with _subs_lock:
        subs = list(_subscribers.get(task_id, []))
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass
    # Also persist into trace_events
    try:
        with db() as conn:
            conn.execute(
                "UPDATE orchestrator_tasks "
                "SET trace_events = trace_events || %s::jsonb "
                "WHERE task_id = %s",
                (json.dumps([event]), task_id),
            )
            conn.commit()
    except Exception as e:
        log.warning(f"trace persist failed for {task_id}: {e}")


def subscribe(task_id: str) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=100)
    with _subs_lock:
        _subscribers.setdefault(task_id, []).append(q)
    return q


def unsubscribe(task_id: str, q: queue.Queue) -> None:
    with _subs_lock:
        if task_id in _subscribers:
            try:
                _subscribers[task_id].remove(q)
            except ValueError:
                pass
            if not _subscribers[task_id]:
                del _subscribers[task_id]


# ─── DeepSeek client ──────────────────────────────────────────────────────
def deepseek_chat(messages: list[dict], max_tokens: int = 800, temperature: float = 0.3, timeout_s: int = 30) -> str:
    """Call DeepSeek chat API. Returns text content or empty string."""
    if not DS_API_KEY:
        log.warning("DEEPSEEK_API_KEY not set — DS call skipped")
        return ""
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        f"{DS_BASE}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {DS_API_KEY}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d["choices"][0]["message"]["content"] or ""
    except Exception as e:
        log.error(f"deepseek call failed: {e}")
        return ""


# ─── Capability registry helpers ──────────────────────────────────────────
def list_active_capabilities() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT agent_id, capability, description, confidence "
            "FROM agent_capabilities WHERE is_active "
            "ORDER BY agent_id, capability"
        ).fetchall()
    return rows


def upsert_capability(agent_id: str, capability: str, description: str = "", confidence: float = 0.8) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO agent_capabilities(agent_id, capability, description, confidence) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (agent_id, capability) DO UPDATE "
            "SET description = EXCLUDED.description, "
            "    confidence = EXCLUDED.confidence, "
            "    is_active = TRUE, "
            "    updated_at = NOW()",
            (agent_id, capability, description, confidence),
        )
        conn.commit()


def agents_with_any_capability(caps: list[str]) -> list[str]:
    """Return agent_ids that have at least one of the given capabilities."""
    if not caps:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent_id FROM agent_capabilities "
            "WHERE is_active AND capability = ANY(%s)",
            (caps,),
        ).fetchall()
    return [r["agent_id"] for r in rows]


def is_agent_online(agent_id: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT last_seen_at, online FROM agent_presence WHERE agent_id = %s",
            (agent_id,),
        ).fetchone()
    if not row:
        return False
    if not row["online"]:
        return False
    if not row["last_seen_at"]:
        return False
    delta = (datetime.now(timezone.utc) - row["last_seen_at"]).total_seconds()
    return delta < 120


def update_presence(agent_id: str, online: bool = True) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO agent_presence(agent_id, online, last_seen_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (agent_id) DO UPDATE "
            "SET online = EXCLUDED.online, last_seen_at = NOW()",
            (agent_id, online),
        )
        conn.commit()


# ─── Stage 1.2: AI-based routing (DeepSeek classifier) ────────────────────
ROUTING_SYS_PROMPT = (
    "Ты — диспетчер задач между ИИ-помощниками. На вход даётся текст запроса пользователя "
    "и список доступных помощников с их навыками. "
    "Твоя задача: выбрать 1-3 помощников, наиболее подходящих для решения этой задачи. "
    "\n\nПравила:\n"
    "1. Отвечай СТРОГО в формате JSON: {\"executors\": [\"agent1\", \"agent2\"], \"reasoning\": \"короткое объяснение\"}\n"
    "2. НЕ выдумывай имена — только из списка.\n"
    "3. Если задача не требует специалиста (общий вопрос) — выбери одного универсального.\n"
    "4. Чем меньше помощников, тем лучше — не дублируй роли."
)


def route_task(request_text: str, available: list[dict]) -> tuple[list[str], str]:
    """Pick 1-3 executors via DeepSeek. Validates against registry.

    Returns (executor_ids, reasoning).
    """
    # Build capability summary by agent
    by_agent: dict[str, list[str]] = {}
    descs: dict[str, str] = {}
    for c in available:
        by_agent.setdefault(c["agent_id"], []).append(c["capability"])
        descs.setdefault(c["agent_id"], c.get("description") or "")
    agent_summary = "\n".join(
        f"- {aid}: навыки [{', '.join(caps)}]"
        for aid, caps in by_agent.items()
    )

    user_msg = (
        f"Запрос пользователя: {request_text[:1500]}\n\n"
        f"Доступные помощники:\n{agent_summary}\n\n"
        f"Выбери кому передать задачу. Ответ только JSON."
    )

    raw = deepseek_chat(
        [
            {"role": "system", "content": ROUTING_SYS_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=300,
        temperature=0.2,
        timeout_s=ROUTING_TIMEOUT_S,
    )

    # Parse JSON (DS sometimes wraps in code fences)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(l for l in lines if not l.startswith("```"))
    try:
        parsed = json.loads(cleaned)
        execs = parsed.get("executors") or []
        reasoning = parsed.get("reasoning") or ""
    except Exception:
        log.warning(f"route parse failed, raw: {raw[:200]}")
        # Fallback: keyword-pick first agent
        execs = [list(by_agent.keys())[0]] if by_agent else []
        reasoning = "fallback: classifier returned non-JSON"

    # Validate — only agents from registry
    valid = [e for e in execs if e in by_agent]
    if not valid and by_agent:
        valid = [list(by_agent.keys())[0]]
        reasoning += " (validation fallback)"
    return valid[:3], reasoning


# ─── Stage 1.3: Dispatch to executors ─────────────────────────────────────
def get_agent_key(agent_id: str) -> str:
    """Look up agent's API key for sending DMs as orchestrator-on-behalf.

    For MVP we use the laptop key to send to executors as user-style messages.
    """
    # We send AS the orchestrator persona via laptop key (sender field in JSON)
    key_env = f"{PROXY_AGENT_KEY_ENV_PREFIX}{agent_id.upper().replace('-','_')}"
    return os.environ.get(key_env) or os.environ.get("AGENT_KEY_LAPTOP") or ""


def dispatch_task(task_id: str, executor: str, request_text: str, context: dict) -> str | None:
    """Send task to executor via /agents/message. Returns message id or None."""
    key = os.environ.get("ORCH_KEY") or get_agent_key("cognitive-core-laptop")  # orchestrator dispatches as orchestrator-bot
    if not key:
        log.error(f"no key to dispatch task {task_id[:8]} to {executor}")
        return None

    text = (
        f"Задача от пользователя через диспетчера:\n\n"
        f"{request_text}\n\n"
        f"Пожалуйста, отвечай по существу. Ответ будет передан пользователю "
        f"после автоматической сборки."
    )
    payload = {
        "to": executor,
        "text": text,
        "context": {"orch_task_id": task_id, **(context or {})},
    }
    req = urllib.request.Request(
        f"{COGCORE_INTERNAL}/agents/message",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("id")
    except Exception as e:
        log.error(f"dispatch failed task={task_id[:8]} to={executor}: {e}")
        return None


def collect_responses(task_id: str, executors: list[str], parent_ids: dict[str, str], deadline_ts: float) -> dict[str, dict]:
    """Poll /agents/inbox for replies from executors. Returns {agent_id: {text, at}}."""
    laptop_key = os.environ.get("ORCH_KEY") or get_agent_key("cognitive-core-laptop")
    collected: dict[str, dict] = {}
    last_check = 0.0
    while time.time() < deadline_ts and len(collected) < len(executors):
        now = time.time()
        if now - last_check < 2.0:
            time.sleep(0.5)
            continue
        last_check = now

        # Read inbox of laptop (orchestrator persona)
        try:
            req = urllib.request.Request(
                f"{COGCORE_INTERNAL}/agents/inbox?since_minutes=10&limit=50",
                headers={"X-API-Key": laptop_key},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.warning(f"inbox poll failed: {e}")
            time.sleep(2)
            continue

        for m in d.get("messages", []):
            sender = m.get("from")
            if sender not in executors or sender in collected:
                continue
            ctx = m.get("context") or {}
            # Match by parent_id of dispatched message
            if ctx.get("parent_id") == parent_ids.get(sender):
                text = m.get("text") or ""
                # Strip natural marker if present (it's still server-side; harmless)
                collected[sender] = {
                    "text": text,
                    "received_at": m.get("sent_at"),
                    "raw_id": m.get("id"),
                }
                publish_event(task_id, {
                    "type": "executor_responded",
                    "agent": sender,
                    "chars": len(text),
                })

    return collected


# ─── Stage 1.4: Synthesis via DeepSeek ────────────────────────────────────
SYNTH_SYS_PROMPT = (
    "Ты — помощник по сборке ответов. На вход даётся вопрос пользователя и ответы "
    "от 1-3 помощников-исполнителей. Твоя задача: собрать один связный ответ для пользователя "
    "на грамотном русском языке. "
    "\n\nПравила:\n"
    "1. Пиши простым русским языком, без технических сокращений (или с пояснением в скобках).\n"
    "2. Если ответы согласованы — объедини их в единый текст.\n"
    "3. Если противоречат — укажи разногласия и приведи оба варианта.\n"
    "4. Удали из ответа служебные пометки помощников (всё после '— автоматический ответ').\n"
    "5. Не упоминай технические имена помощников типа 'ai-crm-deploy' — используй их роль: "
    "'помощник AI-CRM', 'помощник по серверу'.\n"
    "6. Ответ должен быть законченным и понятным сам по себе.\n"
    "7. Если ответов нет (помощники не на связи) — честно скажи об этом и предложи попробовать позже."
)


def sanitize_response(text: str) -> str:
    """Strip internal markers and sensitive fields from executor response."""
    if not text:
        return ""
    # Cut suffix marker
    suffix_idx = text.find("— автоматический ответ")
    if suffix_idx > 0:
        text = text[:suffix_idx].rstrip()
    # Strip legacy markers too
    legacy_idx = text.find("[from ")
    if legacy_idx == 0:  # only if prefix
        nl = text.find("\n", legacy_idx)
        if nl > 0:
            text = text[nl + 1:].lstrip()
    return text.strip()


def display_name_for(agent_id: str) -> str:
    """Map agent_id to user-friendly role name."""
    NAMES = {
        "cognitive-core-laptop": "Помощник по серверу",
        "ai-crm-deploy": "Помощник AI-CRM",
    }
    return NAMES.get(agent_id, agent_id)


def synthesize(request_text: str, responses: dict[str, dict], proxy_fallback: bool = False) -> str:
    """Merge executor responses into one user-friendly answer."""
    if not responses:
        return "К сожалению, ни один из помощников сейчас не отвечает. Попробуйте, пожалуйста, позже."

    sanitized = []
    for agent_id, r in responses.items():
        clean = sanitize_response(r.get("text", ""))
        if clean:
            sanitized.append(f"### Ответ помощника «{display_name_for(agent_id)}»:\n{clean}")

    if not sanitized:
        return "Помощники получили задачу, но не дали содержательного ответа."

    user_msg = (
        f"Вопрос пользователя: {request_text[:1500]}\n\n"
        f"Ответы помощников:\n\n" + "\n\n".join(sanitized)
    )

    raw = deepseek_chat(
        [
            {"role": "system", "content": SYNTH_SYS_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=1500,
        temperature=0.4,
        timeout_s=SYNTH_TIMEOUT_S,
    )

    if not raw:
        # Fallback: just concatenate sanitized responses with separators
        return "\n\n".join(sanitized)

    if proxy_fallback:
        raw += "\n\n_(временный ответ от сервера — основные помощники сейчас оффлайн)_"
    return raw.strip()


# ─── Stage 1: Main orchestration flow ─────────────────────────────────────
def run_orchestration(task_id: str) -> None:
    """Background thread to handle a task end-to-end."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT request_text, context, cascading_depth FROM orchestrator_tasks WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        if not row:
            log.error(f"task {task_id} not found")
            return

        request_text = row["request_text"]
        context = row["context"] or {}
        depth = row["cascading_depth"] or 0

        publish_event(task_id, {"type": "started", "request_preview": request_text[:80]})
        _set_status(task_id, "routing")

        # 1) Route via DS classifier
        caps = list_active_capabilities()
        executors, reasoning = route_task(request_text, caps)
        publish_event(task_id, {
            "type": "routed",
            "executors": executors,
            "reasoning": reasoning,
        })

        if not executors:
            _complete(task_id, "Не удалось определить подходящих помощников.", proxy=True)
            return

        # Save assignment
        with db() as conn:
            conn.execute(
                "UPDATE orchestrator_tasks SET assigned_agents = %s::jsonb, status = 'waiting_executors' WHERE task_id = %s",
                (json.dumps(executors), task_id),
            )
            conn.commit()

        # 2) Dispatch to each executor (parallel)
        parent_ids: dict[str, str] = {}
        for exec_id in executors:
            pid = dispatch_task(task_id, exec_id, request_text, context)
            if pid:
                parent_ids[exec_id] = pid
                publish_event(task_id, {"type": "dispatched", "agent": exec_id})
            else:
                publish_event(task_id, {"type": "dispatch_failed", "agent": exec_id})

        if not parent_ids:
            _complete(task_id, "Не удалось доставить задачу помощникам. Попробуйте позже.", proxy=True)
            return

        # 3) Collect responses (timeout aware)
        deadline = time.time() + EXECUTOR_TIMEOUT_S
        responses = collect_responses(task_id, list(parent_ids.keys()), parent_ids, deadline)

        proxy_used = len(responses) == 0
        if proxy_used:
            publish_event(task_id, {"type": "proxy_fallback", "reason": "no executor responded"})

        # Save responses to DB
        with db() as conn:
            conn.execute(
                "UPDATE orchestrator_tasks SET responses = %s::jsonb, status = 'synthesizing' WHERE task_id = %s",
                (json.dumps(responses), task_id),
            )
            conn.commit()

        # 4) Synthesize
        publish_event(task_id, {"type": "synthesizing", "response_count": len(responses)})
        final = synthesize(request_text, responses, proxy_fallback=proxy_used)

        # 5) Complete
        _complete(task_id, final, proxy=proxy_used)

    except Exception as e:
        log.exception(f"orchestration failed for {task_id}")
        publish_event(task_id, {"type": "error", "msg": str(e)})
        _complete(task_id, f"Внутренняя ошибка при обработке: {e}", proxy=True, status="failed")


def _set_status(task_id: str, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status = %s WHERE task_id = %s",
            (status, task_id),
        )
        conn.commit()
    publish_event(task_id, {"type": "status", "value": status})


def _complete(task_id: str, final: str, proxy: bool = False, status: str = "completed") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE orchestrator_tasks SET status = %s, final_answer = %s, proxy_used = %s, completed_at = NOW() WHERE task_id = %s",
            (status, final, proxy, task_id),
        )
        conn.commit()
    publish_event(task_id, {"type": "completed", "preview": final[:120], "proxy": proxy})


# ─── Stage 3.3: Voting (consensus from multiple responses) ────────────────
def synthesize_with_voting(request_text: str, responses: dict[str, dict]) -> str:
    """When >= 3 executors with same capability — let DS pick majority view."""
    if len(responses) < 3:
        return synthesize(request_text, responses)
    sys_p = (
        "Тебе даны 3+ ответа на один вопрос. Найди консенсусный (большинство) "
        "ответ и сформулируй его. Если есть разногласия — отметь это. "
        "Пиши на грамотном русском без жаргона."
    )
    user_p = (
        f"Вопрос: {request_text[:1000]}\n\n"
        f"Ответы:\n\n"
        + "\n\n---\n\n".join(f"От {display_name_for(a)}:\n{sanitize_response(r['text'])}" for a, r in responses.items())
    )
    return deepseek_chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}], max_tokens=1200, temperature=0.3, timeout_s=30) or synthesize(request_text, responses)


# ─── Stage 3.1: Cascading tasks ───────────────────────────────────────────
def create_child_task(parent_task_id: str, child_request: str, executor: str | None = None) -> str | None:
    """Create a child orchestrator task spawned by an executor. Returns task_id."""
    with db() as conn:
        parent = conn.execute(
            "SELECT user_id, cascading_depth FROM orchestrator_tasks WHERE task_id = %s",
            (parent_task_id,),
        ).fetchone()
    if not parent:
        return None
    new_depth = (parent["cascading_depth"] or 0) + 1
    if new_depth > MAX_CASCADE_DEPTH:
        log.warning(f"cascade depth limit hit on {parent_task_id}")
        return None

    new_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO orchestrator_tasks(task_id, user_id, request_text, context, parent_task_id, cascading_depth) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (new_id, parent["user_id"], child_request, json.dumps({"parent_task_id": parent_task_id}),
             parent_task_id, new_depth),
        )
        conn.commit()
    threading.Thread(target=run_orchestration, args=(new_id,), daemon=True).start()
    return new_id


# ─── Stage 2.4: JWT tokens ────────────────────────────────────────────────
def issue_token(user_id: str, ttl_minutes: int = 15) -> str:
    """Issue a signed token (header.payload.sig, like JWT-lite)."""
    payload = {
        "user_id": user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_minutes * 60,
        "jti": str(uuid.uuid4()),
    }
    pb = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(ORCH_SIGN_KEY.encode(), pb.encode(), hashlib.sha256).digest())
    token = f"{pb}.{sig}"

    # Persist hash for revocation
    h = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        conn.execute(
            "INSERT INTO user_tokens(user_id, token_hash, expires_at) "
            "VALUES (%s, %s, NOW() + interval '%s minutes')",
            (user_id, h, ttl_minutes),
        )
        conn.commit()
    return token


def verify_token(token: str) -> dict | None:
    """Verify a signed token. Returns payload dict or None."""
    if not token or "." not in token:
        return None
    pb, sig = token.split(".", 1)
    expected_sig = _b64u(hmac.new(ORCH_SIGN_KEY.encode(), pb.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(_b64u_decode(pb))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    # Check revocation
    h = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        row = conn.execute(
            "SELECT revoked FROM user_tokens WHERE token_hash = %s",
            (h,),
        ).fetchone()
    if row and row["revoked"]:
        return None
    return payload


def _b64u(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    import base64
    pad = (4 - len(s) % 4) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


# ─── HTTP server ──────────────────────────────────────────────────────────
def json_response(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-User-Token")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def html_response(handler: BaseHTTPRequestHandler, html: str, content_type: str = "text/html; charset=utf-8") -> None:
    raw = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def extract_user(handler: BaseHTTPRequestHandler) -> str | None:
    """Extract user_id from token header or query."""
    token = handler.headers.get("X-User-Token") or handler.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        # Try query
        url = urllib.parse.urlparse(handler.path)
        qs = urllib.parse.parse_qs(url.query)
        token = (qs.get("token") or [""])[0]
    if not token:
        return None
    payload = verify_token(token)
    return payload.get("user_id") if payload else None


def cogcore_session_user(cookie_header: str | None) -> dict | None:
    """SSO bridge: validate the main-site session cookie (cogcore_session) via
    cognitive_api /auth/status. Returns {user_id, email, is_admin} if the visitor
    is already logged in to the main site, else None.

    This is what lets /ui/ask reuse the normal site login — no separate code.
    The browser sends cogcore_session automatically because /ui/ask and the
    main site share one origin (mcp.me-ai.ru).
    """
    if not cookie_header or "cogcore_session" not in cookie_header:
        return None
    for base in (COGCORE_INTERNAL, COGCORE_BASE):
        if not base:
            continue
        try:
            req = urllib.request.Request(
                f"{base.rstrip('/')}/auth/status",
                headers={"Cookie": cookie_header, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.warning(f"session-login check via {base} failed: {e}")
            continue
        if d.get("authenticated") and d.get("user_id"):
            return {
                "user_id": d.get("user_id"),
                "email": d.get("email"),
                "is_admin": bool(d.get("is_admin")),
            }
        return None
    return None


class OrchHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # quieter logging
        log.info(f"{self.address_string()} {format % args}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-User-Token")
        self.end_headers()

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)
        try:
            if path == "/orchestrator/health":
                return json_response(self, 200, {
                    "status": "ok",
                    "ds_configured": bool(DS_API_KEY),
                    "version": "0.1.0",
                })

            if path == "/orchestrator/capabilities":
                caps = list_active_capabilities()
                return json_response(self, 200, {"capabilities": caps})

            if path == "/orchestrator/tasks":
                user_id = extract_user(self)
                if not user_id:
                    return json_response(self, 401, {"error": "auth required"})
                limit = int((qs.get("limit") or ["20"])[0])
                with db() as conn:
                    rows = conn.execute(
                        "SELECT task_id, status, request_text, assigned_agents, final_answer, proxy_used, created_at, completed_at "
                        "FROM orchestrator_tasks WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (user_id, limit),
                    ).fetchall()
                # convert UUID & datetime to str
                for r in rows:
                    r["task_id"] = str(r["task_id"])
                    r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
                    r["completed_at"] = r["completed_at"].isoformat() if r["completed_at"] else None
                return json_response(self, 200, {"tasks": rows})

            if path.startswith("/orchestrator/tasks/") and path.endswith("/stream"):
                task_id = path.split("/")[3]
                return self.sse_stream(task_id)

            if path.startswith("/orchestrator/tasks/"):
                task_id = path.split("/")[3]
                with db() as conn:
                    row = conn.execute(
                        "SELECT * FROM orchestrator_tasks WHERE task_id = %s",
                        (task_id,),
                    ).fetchone()
                if not row:
                    return json_response(self, 404, {"error": "not found"})
                row["task_id"] = str(row["task_id"])
                if row.get("parent_task_id"):
                    row["parent_task_id"] = str(row["parent_task_id"])
                for k in ("created_at", "completed_at"):
                    if row.get(k):
                        row[k] = row[k].isoformat()
                return json_response(self, 200, row)

            if path == "/ui/ask":
                return html_response(self, ASK_UI_HTML)

            if path == "/manifest.json":
                return json_response(self, 200, {
                    "name": "Cognitive Core Помощники",
                    "short_name": "Помощники",
                    "start_url": "/ui/ask",
                    "scope": "/",
                    "display": "standalone",
                    "background_color": "#0a0a0c",
                    "theme_color": "#0066cc",
                    "icons": [
                        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    ],
                })

            if path == "/sw.js":
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.end_headers()
                self.wfile.write(SERVICE_WORKER_JS.encode("utf-8"))
                return

            return json_response(self, 404, {"error": "unknown path"})
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception(f"GET {path} failed")
            try:
                return json_response(self, 500, {"error": str(e)})
            except Exception:
                return

    def do_POST(self) -> None:
        u = urllib.parse.urlparse(self.path)
        path = u.path
        length = int(self.headers.get("Content-Length") or 0)
        body_raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(body_raw.decode("utf-8")) if body_raw else {}
        except Exception:
            return json_response(self, 400, {"error": "invalid JSON"})

        try:
            if path == "/orchestrator/login":
                # MVP: owner-only login by shared secret (env or fixed)
                code = body.get("code", "")
                expected = os.environ.get("ORCH_OWNER_CODE", "")
                if expected and hmac.compare_digest(code, expected):
                    user_id = body.get("user_id", "owner")
                    token = issue_token(user_id, ttl_minutes=24 * 60)  # 24h for owner
                    return json_response(self, 200, {"token": token, "user_id": user_id, "expires_in": 86400})
                return json_response(self, 401, {"error": "invalid code"})

            if path == "/orchestrator/session-login":
                # SSO: if the visitor already has a valid main-site session
                # cookie, issue an orchestrator token automatically — no code.
                u = cogcore_session_user(self.headers.get("Cookie"))
                if u:
                    token = issue_token(u["user_id"], ttl_minutes=24 * 60)
                    return json_response(self, 200, {
                        "token": token,
                        "user_id": u["user_id"],
                        "email": u.get("email"),
                        "via": "session",
                    })
                return json_response(self, 401, {"error": "no active site session"})

            if path == "/orchestrator/ask":
                user_id = extract_user(self) or body.get("user_id") or "anonymous"
                request_text = (body.get("request") or "").strip()
                if not request_text:
                    return json_response(self, 400, {"error": "request required"})

                task_id = str(uuid.uuid4())
                with db() as conn:
                    conn.execute(
                        "INSERT INTO orchestrator_tasks(task_id, user_id, request_text, context) "
                        "VALUES (%s, %s, %s, %s)",
                        (task_id, user_id, request_text, json.dumps(body.get("context") or {})),
                    )
                    conn.commit()
                threading.Thread(target=run_orchestration, args=(task_id,), daemon=True).start()
                return json_response(self, 202, {"task_id": task_id, "status": "queued"})

            if path == "/orchestrator/capabilities":
                upsert_capability(
                    body["agent_id"],
                    body["capability"],
                    body.get("description", ""),
                    float(body.get("confidence", 0.8)),
                )
                return json_response(self, 200, {"ok": True})

            if path == "/orchestrator/heartbeat":
                agent_id = body.get("agent_id")
                if not agent_id:
                    return json_response(self, 400, {"error": "agent_id required"})
                update_presence(agent_id, body.get("online", True))
                return json_response(self, 200, {"ok": True})

            if path == "/orchestrator/cascade":
                # Executor agent spawns a child task
                parent = body.get("parent_task_id")
                req = body.get("request", "").strip()
                if not parent or not req:
                    return json_response(self, 400, {"error": "parent_task_id + request required"})
                child = create_child_task(parent, req)
                if not child:
                    return json_response(self, 400, {"error": "cascade limit or invalid parent"})
                return json_response(self, 202, {"child_task_id": child})

            return json_response(self, 404, {"error": "unknown path"})
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception(f"POST {path} failed")
            try:
                return json_response(self, 500, {"error": str(e)})
            except Exception:
                return

    def sse_stream(self, task_id: str) -> None:
        """SSE stream for task updates."""
        # Send headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Initial state: replay trace_events
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT status, trace_events, final_answer FROM orchestrator_tasks WHERE task_id = %s",
                    (task_id,),
                ).fetchone()
        except Exception:
            row = None
        if row:
            for ev in (row["trace_events"] or []):
                self._write_sse(ev)
            if row["status"] in ("completed", "failed"):
                # Already done, send final and exit
                self._write_sse({"type": "completed", "preview": (row["final_answer"] or "")[:120]})
                return

        q = subscribe(task_id)
        try:
            last_heartbeat = time.time()
            while True:
                try:
                    ev = q.get(timeout=10)
                    self._write_sse(ev)
                    if ev.get("type") == "completed":
                        break
                except queue.Empty:
                    # heartbeat
                    if time.time() - last_heartbeat > 20:
                        self._write_sse({"type": "heartbeat"})
                        last_heartbeat = time.time()
        except BrokenPipeError:
            pass
        finally:
            unsubscribe(task_id, q)

    def _write_sse(self, event: dict) -> None:
        try:
            self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except BrokenPipeError:
            raise


# ─── UI: chat page for user ───────────────────────────────────────────────
ASK_UI_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Помощники Cognitive Core</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0066cc">
<style>
:root {
  --bg: #0a0a0c;
  --card: rgba(255,255,255,0.04);
  --border: rgba(255,255,255,0.08);
  --text: #ededf0;
  --muted: rgba(237,237,240,0.6);
  --accent: #2f6fed;
  --accent-soft: rgba(47,111,237,0.15);
  --shadow: 0 8px 32px rgba(0,0,0,0.4);
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body {
  margin: 0;
  font: 16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  padding-bottom: env(safe-area-inset-bottom);
}
header {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
  background: rgba(10,10,12,0.85);
  backdrop-filter: blur(10px);
  position: sticky;
  top: 0;
  z-index: 10;
}
header .title { font-weight: 600; flex: 1; }
header .status { font-size: 12px; color: var(--muted); }
.status.online::before { content: "● "; color: #34c759; }
.status.offline::before { content: "● "; color: #ff9500; }

main {
  flex: 1;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
}

.msg {
  max-width: 88%;
  padding: 10px 14px;
  border-radius: 14px;
  white-space: pre-wrap;
  word-wrap: break-word;
  line-height: 1.4;
}
.msg.user { align-self: flex-end; background: var(--accent); color: white; border-bottom-right-radius: 4px; }
.msg.assistant { align-self: flex-start; background: var(--card); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
.msg.system {
  align-self: center;
  font-size: 12px;
  color: var(--muted);
  background: transparent;
  padding: 4px 10px;
}
.msg .trace {
  font-size: 11px;
  color: var(--muted);
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}

footer {
  border-top: 1px solid var(--border);
  padding: 10px 12px;
  background: rgba(10,10,12,0.95);
  position: sticky;
  bottom: 0;
}
.input-row {
  display: flex;
  gap: 8px;
}
textarea {
  flex: 1;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 12px;
  padding: 10px 12px;
  resize: none;
  font: inherit;
  max-height: 120px;
  min-height: 44px;
}
button.send {
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 12px;
  padding: 0 18px;
  font-weight: 600;
  cursor: pointer;
  font-size: 15px;
}
button.send:disabled { opacity: 0.5; cursor: not-allowed; }

.login-overlay {
  position: fixed;
  inset: 0;
  background: rgba(10,10,12,0.95);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 14px;
  padding: 20px;
  z-index: 100;
}
.login-overlay h2 { margin: 0; font-size: 20px; }
.login-overlay input {
  width: 100%;
  max-width: 300px;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 12px;
  border-radius: 10px;
  font: inherit;
}
.login-overlay button { background: var(--accent); color: white; border: none; padding: 12px 24px; border-radius: 10px; font: inherit; cursor: pointer; }
.login-overlay .hint { color: var(--muted); font-size: 13px; text-align: center; max-width: 340px; }
.login-overlay a.login-primary { background: var(--accent); color:#fff; text-decoration:none; padding:13px 28px; border-radius:10px; font-weight:600; font-size:15px; }
.login-overlay button.login-secondary { background:transparent; color:var(--muted); border:1px solid var(--border); padding:9px 16px; border-radius:10px; font-size:13px; cursor:pointer; }
.login-overlay .codebox { display:flex; flex-direction:column; gap:10px; align-items:center; width:100%; max-width:300px; }

.spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--accent-soft);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: middle;
  margin-right: 6px;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="login-overlay" id="login" style="display:none">
  <h2>Вход в помощников</h2>
  <div class="hint">Несколько ИИ-помощников отвечают на ваши вопросы. Войдите тем же аккаунтом, что и на сайте — отдельный код не нужен.</div>
  <a class="login-primary" href="/ui/login?next=/ui/ask">Войти через почту</a>
  <button class="login-secondary" type="button" onclick="toggleCodeLogin()">У меня есть код доступа владельца</button>
  <div class="codebox" id="codeBox" style="display:none">
    <input id="loginCode" placeholder="Код доступа" autocomplete="off">
    <button onclick="doLogin()">Подтвердить код</button>
  </div>
</div>

<header>
  <div class="title">Помощники</div>
  <div class="status" id="presence">подключаюсь...</div>
</header>

<main id="chat">
  <div class="msg system">Здравствуйте! Задайте любой вопрос — система передаст его подходящему помощнику и пришлёт ответ.</div>
</main>

<footer>
  <div class="input-row">
    <textarea id="input" rows="1" placeholder="Ваш вопрос помощникам..."></textarea>
    <button class="send" id="sendBtn" onclick="sendMessage()">→</button>
  </div>
</footer>

<script>
const ORCH_BASE = location.origin.replace(/\\/$/,'') + '/orchestrator';
let token = localStorage.getItem('orch_token');
let userId = localStorage.getItem('orch_user') || 'owner';

init();
async function init() {
  if (token) { hideLogin(); loadHistory(); return; }
  // SSO: reuse the main-site session (same login as the rest of the site).
  try {
    const r = await fetch(ORCH_BASE + '/session-login', {method: 'POST', credentials: 'same-origin'});
    if (r.ok) {
      const d = await r.json();
      token = d.token;
      localStorage.setItem('orch_token', token);
      if (d.user_id) { userId = d.user_id; localStorage.setItem('orch_user', userId); }
      hideLogin();
      loadHistory();
      return;
    }
  } catch (e) { /* fall through to manual login */ }
  showLoginForm();
}
function hideLogin() { document.getElementById('login').style.display = 'none'; }
function showLoginForm() { document.getElementById('login').style.display = 'flex'; }
function toggleCodeLogin() {
  const b = document.getElementById('codeBox');
  b.style.display = (b.style.display === 'none' || !b.style.display) ? 'flex' : 'none';
  if (b.style.display === 'flex') document.getElementById('loginCode').focus();
}

async function doLogin() {
  const code = document.getElementById('loginCode').value.trim();
  if (!code) return;
  try {
    const r = await fetch(ORCH_BASE + '/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code, user_id: userId}),
    });
    if (!r.ok) { alert('Неверный код'); return; }
    const d = await r.json();
    token = d.token;
    localStorage.setItem('orch_token', token);
    if (d.user_id) { userId = d.user_id; localStorage.setItem('orch_user', userId); }
    hideLogin();
    loadHistory();
  } catch (e) { alert('Ошибка входа: ' + e); }
}

async function loadHistory() {
  try {
    const r = await fetch(ORCH_BASE + '/tasks?limit=10', {headers: {'X-User-Token': token}});
    if (r.status === 401) { localStorage.removeItem('orch_token'); document.getElementById('login').style.display='flex'; return; }
    const d = await r.json();
    const chat = document.getElementById('chat');
    chat.innerHTML = '<div class="msg system">История последних 10 запросов</div>';
    (d.tasks || []).reverse().forEach(t => {
      addMessage('user', t.request_text);
      if (t.final_answer) addMessage('assistant', t.final_answer);
    });
    setPresence('online');
  } catch (e) { console.error(e); }
}

function setPresence(state) {
  const el = document.getElementById('presence');
  el.className = 'status ' + state;
  el.textContent = state === 'online' ? 'на связи' : 'нет связи';
}

function addMessage(kind, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + kind;
  el.textContent = text;
  document.getElementById('chat').appendChild(el);
  el.scrollIntoView({behavior: 'smooth', block: 'end'});
  return el;
}

async function sendMessage() {
  const ta = document.getElementById('input');
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';
  document.getElementById('sendBtn').disabled = true;

  addMessage('user', text);
  const placeholder = addMessage('assistant', '');
  placeholder.innerHTML = '<span class="spinner"></span>Принято, обрабатываю...';

  let task_id;
  try {
    const r = await fetch(ORCH_BASE + '/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-User-Token': token},
      body: JSON.stringify({request: text}),
    });
    if (r.status === 401) { document.getElementById('login').style.display='flex'; return; }
    const d = await r.json();
    task_id = d.task_id;
  } catch (e) {
    placeholder.textContent = 'Не удалось отправить запрос: ' + e;
    document.getElementById('sendBtn').disabled = false;
    return;
  }

  // Open SSE stream for live updates
  const evtSrc = new EventSource(ORCH_BASE + '/tasks/' + task_id + '/stream?token=' + encodeURIComponent(token));
  let traceLines = [];

  evtSrc.onmessage = async (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.type === 'routed') {
      traceLines.push('Выбраны помощники: ' + (d.executors || []).join(', '));
    } else if (d.type === 'dispatched') {
      traceLines.push('Отправил запрос: ' + d.agent);
    } else if (d.type === 'executor_responded') {
      traceLines.push('Ответил: ' + d.agent + ' (' + d.chars + ' символов)');
    } else if (d.type === 'synthesizing') {
      traceLines.push('Собираю единый ответ...');
    } else if (d.type === 'proxy_fallback') {
      traceLines.push('Помощники не отвечают — отвечу сам.');
    } else if (d.type === 'completed') {
      evtSrc.close();
      // Fetch final
      try {
        const r2 = await fetch(ORCH_BASE + '/tasks/' + task_id, {headers: {'X-User-Token': token}});
        const final = (await r2.json()).final_answer || '(пусто)';
        placeholder.innerHTML = '';
        placeholder.textContent = final;
        if (traceLines.length) {
          const trace = document.createElement('div');
          trace.className = 'trace';
          trace.textContent = traceLines.join(' · ');
          placeholder.appendChild(trace);
        }
      } catch (e) { placeholder.textContent = 'Ошибка получения ответа'; }
      document.getElementById('sendBtn').disabled = false;
      return;
    } else if (d.type === 'error') {
      placeholder.textContent = 'Ошибка: ' + d.msg;
      evtSrc.close();
      document.getElementById('sendBtn').disabled = false;
      return;
    }
    // Update placeholder progress
    placeholder.innerHTML = '<span class="spinner"></span>' + (traceLines[traceLines.length-1] || 'Обрабатываю...');
  };

  evtSrc.onerror = () => {
    evtSrc.close();
    if (placeholder.textContent.includes('Принято') || placeholder.textContent.includes('Обрабатываю')) {
      placeholder.textContent = 'Соединение потеряно. Обновите страницу.';
    }
    document.getElementById('sendBtn').disabled = false;
  };
}

// Auto-grow textarea
document.getElementById('input').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
// Enter to send (Shift+Enter for newline)
document.getElementById('input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// Register service worker (PWA)
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(e => console.warn('SW reg failed', e));
}
</script>
</body>
</html>"""


SERVICE_WORKER_JS = """// Cognitive Core PWA service worker
const CACHE = 'cogcore-orch-v1';
const ASSETS = ['/ui/ask', '/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS).catch(() => null)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // network-first for API
  if (url.pathname.startsWith('/orchestrator/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', {headers: {'Content-Type':'application/json'}})));
    return;
  }
  // cache-first for static UI
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      const copy = resp.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => null);
      return resp;
    }))
  );
});

self.addEventListener('push', e => {
  let data = {title: 'Помощники', body: 'Новый ответ'};
  try { data = e.data.json(); } catch (_) {}
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    data: data.url || '/ui/ask',
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data || '/ui/ask'));
});
"""


# ─── Main entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"Cognitive Orchestrator starting on port {PORT}")
    log.info(f"DB: {DB_DSN.split('@')[-1] if '@' in DB_DSN else DB_DSN.split('host=')[-1].split(' ')[0]}")
    log.info(f"DeepSeek: {'configured' if DS_API_KEY else 'NOT CONFIGURED — routing/synth will fail'}")
    log.info(f"Cogcore base: {COGCORE_INTERNAL}")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), OrchHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
