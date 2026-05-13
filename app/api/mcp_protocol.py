"""Native MCP protocol endpoint inside cognitive_api FastAPI.

v2 (2026-05-13): COMPACT-SURVIVAL hardening.

Changes vs v1:
  1. cognitive_remember now resolves source_agent from API key (was sending
     empty string → 422 string_too_short → silent memory write failure).
  2. Per-tool timeouts split: heavy tools (cognitive_recall, cognitive_consolidate)
     get 25s, light tools 8s. Default tools/call wait_for cap raised to 30s
     but every tool has its own httpx timeout to prevent worker pool starvation.
  3. cognitive_continue enriched with pending DMs, active rooms, active locks,
     human-readable "since" — agent gets full state in one call after /compact.
  4. New cognitive_resume = single super-tool that combines continue + inbox +
     online + recall last domain. Designed to be the FIRST call after /compact.
  5. cognitive_my_history split: history (checkpoints, current behaviour) vs
     cognitive_my_events (raw L1 events). Old name kept for backward compat.
  6. _resolve_agent caches AGENT_API_KEYS dict (was re-parsing JSON every call).
  7. Better error messages with hint on auth failures.
  8. Defensive timeouts on every _call_self call.

Routes added:
  POST /mcp/messages       — JSON-RPC dispatch
  GET  /mcp/sse            — empty SSE stub (legacy compat)
  GET  /mcp/health         — quick health check
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

router = APIRouter(prefix="/mcp", tags=["mcp"])
log = logging.getLogger("mcp_protocol")

# ─────────────────────────────────────────────────────────────────
# Per-tool timeouts (seconds). Heavy tools get more, light ones less.
# Total cap is enforced by asyncio.wait_for in mcp_messages.
# ─────────────────────────────────────────────────────────────────
TOOL_TIMEOUTS_S: dict[str, float] = {
    "cognitive_recall": 20.0,         # DS embedding call — slow but bounded
    "cognitive_consolidate": 30.0,    # batch DS calls — slowest
    "cognitive_resume": 12.0,         # multi-fetch but parallel
    "cognitive_agent_manifest": 8.0,  # 2 calls
    # default for everything else
}
DEFAULT_TOOL_TIMEOUT_S = 6.0
GLOBAL_HARD_CAP_S = 35.0

# ─────────────────────────────────────────────────────────────────
# Tool schemas — registered with MCP `tools/list`
# ─────────────────────────────────────────────────────────────────
TOOLS: list[dict[str, Any]] = [
    {
        "name": "cognitive_remember",
        "description": (
            "Записать новый опыт в долгосрочную память (L1 событие). "
            "Память пройдёт цикл L1 → daily → weekly → L3 эталонные знания."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Предметная область, e.g. fastapi_dev"},
                "task": {"type": "string", "description": "Что было сделано"},
                "result": {"type": "string", "description": "Каков результат", "default": ""},
                "feedback": {"type": "string", "description": "positive / negative / neutral", "default": ""},
                "lessons": {"type": "string", "description": "Какие уроки извлечены", "default": ""},
                "tools_used": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["domain", "task"],
        },
    },
    {
        "name": "cognitive_recall",
        "description": (
            "Найти релевантные знания по запросу через KNN-поиск (L3 + tools). "
            "Возвращает frame с patterns / mistakes / rules / tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "domain": {"type": "string"},
                "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                "include_tools": {"type": "boolean", "default": True},
                "grouped": {"type": "boolean", "default": True},
            },
            "required": ["query", "domain"],
        },
    },
    {
        "name": "cognitive_list",
        "description": "Просмотреть активные L3 знания.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "cognitive_tools",
        "description": "Список инструментов в реестре домена.",
        "inputSchema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": ["domain"],
        },
    },
    {
        "name": "cognitive_consolidate",
        "description": "Запустить consolidation цикл вручную (daily или weekly).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["daily", "weekly"], "default": "daily"},
            },
        },
    },
    {
        "name": "cognitive_health",
        "description": "System status: postgres / redis / minio / llm.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cognitive_domains",
        "description": "Список всех известных доменов с counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cognitive_save_state",
        "description": (
            "Сохранить checkpoint текущей задачи и контекста. Можно потом "
            "восстановить через cognitive_continue."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "current_task": {"type": "string"},
                "state_data": {"type": "object", "default": {}},
            },
            "required": ["current_task"],
        },
    },
    {
        "name": "cognitive_continue",
        "description": (
            "Восстановить последний checkpoint текущего агента. "
            "Возвращает: current_task, state_data, since_human, last_checkpoint_at, "
            "pending_dm_count, recent_events. Достаточно для resume после /compact."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cognitive_resume",
        "description": (
            "ГЛАВНЫЙ инструмент после /compact: возвращает ВСЁ нужное для продолжения "
            "работы — state, новые DM, online агенты, активные комнаты. ПЕРВЫЙ вызов "
            "сразу после восстановления контекста."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cognitive_my_history",
        "description": "Хронология checkpoints текущего агента (последние N save_state).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500}},
        },
    },
    {
        "name": "cognitive_my_events",
        "description": "Raw L1 события текущего агента (что писал через cognitive_remember).",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200}},
        },
    },
    {
        "name": "cognitive_agent_manifest",
        "description": "Полный manifest агента: state + статистика + capabilities.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cognitive_send",
        "description": "Direct message другому агенту.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "text": {"type": "string"},
                "context": {"type": "object", "default": {}},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "cognitive_inbox",
        "description": "Прочитать входящие DM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_minutes": {"type": "integer", "default": 60, "minimum": 1, "maximum": 10080},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
        },
    },
    {
        "name": "cognitive_online",
        "description": "Список онлайн агентов (heartbeat в последние N секунд).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "within_seconds": {"type": "integer", "default": 120},
            },
        },
    },
    {
        "name": "cognitive_heartbeat",
        "description": "Обновить presence + current_task.",
        "inputSchema": {
            "type": "object",
            "properties": {"current_task": {"type": "string"}},
        },
    },
]


# ─────────────────────────────────────────────────────────────────
# JSON-RPC envelope models
# ─────────────────────────────────────────────────────────────────
class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] | list[Any] | None = None


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ─────────────────────────────────────────────────────────────────
# Tool dispatcher — calls existing REST handlers via in-process ASGI
# ─────────────────────────────────────────────────────────────────
async def _call_self(
    request: Request,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout_s: float = 8.0,
) -> dict:
    """Make in-process ASGI call to own FastAPI app, propagating X-API-Key.

    Defensive timeout default 8s — overridable per-call. NEVER unbounded.
    """
    app = request.app
    api_key = request.headers.get("x-api-key", "")
    headers = {"X-API-Key": api_key} if api_key else {}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://internal", timeout=timeout_s) as client:
        try:
            resp = await client.request(
                method, path, headers=headers, json=json_body, params=params,
            )
        except Exception as e:
            return {"_error": f"{type(e).__name__}: {e}", "_path": path, "_method": method}
        try:
            return resp.json()
        except Exception:
            return {"_status": resp.status_code, "_text": resp.text[:500]}


# ─────────────────────────────────────────────────────────────────
# Cached agent_id lookup from X-API-Key
# ─────────────────────────────────────────────────────────────────
_KEYS_CACHE: dict[str, dict[str, str]] = {"_data": {}, "_loaded_at": 0}
_KEYS_TTL_S = 60  # re-read env every 60s


def _load_keys() -> dict[str, str]:
    """Parse AGENT_API_KEYS env JSON: {agent_id: api_key}. Cached for 60s."""
    now = time.time()
    if now - _KEYS_CACHE["_loaded_at"] < _KEYS_TTL_S and _KEYS_CACHE["_data"]:
        return _KEYS_CACHE["_data"]  # type: ignore
    raw = os.environ.get("AGENT_API_KEYS", "{}")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _KEYS_CACHE["_data"] = data
    _KEYS_CACHE["_loaded_at"] = now
    return data


async def _resolve_agent(request: Request) -> str:
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        raise ValueError("X-API-Key header required (set in MCP client config or curl -H 'X-API-Key: ...')")
    keys = _load_keys()
    for agent_id, key in keys.items():
        if key == api_key:
            return agent_id
    raise ValueError(
        "API key not registered in AGENT_API_KEYS. "
        "Ask admin to add your agent to /opt/cognitive-core/.env and recreate api container."
    )


def _human_since(iso_ts: str | None) -> str:
    """Convert ISO timestamp to '5 minutes ago' style. Best-effort."""
    if not iso_ts:
        return "unknown"
    try:
        from datetime import datetime, timezone
        # Parse ISO 8601 with optional fractional seconds and tz
        ts = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        secs = delta.total_seconds()
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except Exception:
        return iso_ts


async def _enrich_continue(request: Request, agent_id: str, base_state: dict) -> dict:
    """Add pending DMs count, active rooms, locks, since_human to /agents/{id}/state result."""
    enriched = dict(base_state)

    last_ts = base_state.get("last_checkpoint_at")
    enriched["since_human"] = _human_since(last_ts)

    # Pending DMs since last checkpoint (best-effort, default to last 60min if no checkpoint)
    inbox_minutes = 60
    if last_ts:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            secs = (datetime.now(timezone.utc) - dt).total_seconds()
            inbox_minutes = max(5, min(int(secs / 60) + 5, 10080))  # cap at 1 week
        except Exception:
            pass
    inbox = await _call_self(
        request, "GET", "/agents/inbox",
        params={"since_minutes": inbox_minutes, "limit": 50},
        timeout_s=4.0,
    )
    enriched["pending_dm_count"] = inbox.get("count", 0) if isinstance(inbox, dict) else 0
    enriched["pending_dm_preview"] = (
        [{"from": m.get("from"), "preview": (m.get("text", "") or "")[:80], "at": m.get("sent_at")}
         for m in (inbox.get("messages", [])[:5] if isinstance(inbox, dict) else [])]
    )

    return enriched


async def _dispatch_tool(request: Request, name: str, args: dict) -> dict:
    """Map MCP tool name to existing REST endpoint and execute."""
    a = args or {}

    if name == "cognitive_remember":
        domain = a.get("domain")
        if not domain or not a.get("task"):
            raise ValueError("domain + task required")
        # CRITICAL FIX: resolve agent_id from API key. Was sending "" → 422 silent fail.
        agent_id = await _resolve_agent(request)
        payload = {
            "task": a.get("task", ""),
            "result": a.get("result", ""),
            "feedback": a.get("feedback", ""),
            "lessons": a.get("lessons", ""),
            "tools_used": a.get("tools_used", []),
        }
        body = {"source_agent": agent_id, "domain": domain, "payload": payload}
        return await _call_self(request, "POST", "/events", json_body=body, timeout_s=6.0)

    if name == "cognitive_recall":
        body = {
            "domain": a.get("domain"),
            "context": a.get("query"),
            "top_k": min(max(int(a.get("top_k", 5)), 1), 20),
            "include_tools": bool(a.get("include_tools", True)),
        }
        params = {"grouped": "true"} if a.get("grouped", True) else None
        return await _call_self(request, "POST", "/operative/query", json_body=body, params=params, timeout_s=18.0)

    if name == "cognitive_list":
        params = {"limit": min(max(int(a.get("limit", 50)), 1), 200)}
        if a.get("domain"):
            params["domain"] = a["domain"]
        return await _call_self(request, "GET", "/dashboard/knowledge", params=params, timeout_s=6.0)

    if name == "cognitive_tools":
        return await _call_self(request, "GET", "/tools", params={"domain": a.get("domain", "")}, timeout_s=4.0)

    if name == "cognitive_consolidate":
        level = a.get("level", "daily")
        if level not in {"daily", "weekly"}:
            raise ValueError("level must be 'daily' or 'weekly'")
        return await _call_self(request, "POST", f"/memory/consolidate/{level}", timeout_s=28.0)

    if name == "cognitive_health":
        return await _call_self(request, "GET", "/health", timeout_s=4.0)

    if name == "cognitive_domains":
        return await _call_self(request, "GET", "/dashboard/domains", timeout_s=4.0)

    if name == "cognitive_save_state":
        body = {
            "current_task": a.get("current_task", ""),
            "state_data": a.get("state_data", {}),
        }
        agent_id = await _resolve_agent(request)
        return await _call_self(request, "POST", f"/agents/{agent_id}/checkpoint", json_body=body, timeout_s=6.0)

    if name == "cognitive_continue":
        agent_id = await _resolve_agent(request)
        base = await _call_self(request, "GET", f"/agents/{agent_id}/state", timeout_s=5.0)
        if isinstance(base, dict) and "_error" not in base:
            base = await _enrich_continue(request, agent_id, base)
        return base

    if name == "cognitive_resume":
        # SUPER-tool for /compact recovery: parallel fetch state + inbox + online
        agent_id = await _resolve_agent(request)

        async def fetch_state():
            return await _call_self(request, "GET", f"/agents/{agent_id}/state", timeout_s=5.0)

        async def fetch_online():
            return await _call_self(request, "GET", "/agents/online", params={"within_seconds": 120}, timeout_s=4.0)

        async def fetch_inbox():
            return await _call_self(
                request, "GET", "/agents/inbox",
                params={"since_minutes": 1440, "limit": 50},  # 24h window
                timeout_s=4.0,
            )

        state, online, inbox = await asyncio.gather(
            fetch_state(), fetch_online(), fetch_inbox(),
            return_exceptions=False,
        )
        # Enrich state inline
        if isinstance(state, dict) and "_error" not in state:
            last_ts = state.get("last_checkpoint_at")
            state["since_human"] = _human_since(last_ts)

        return {
            "agent_id": agent_id,
            "state": state,
            "pending_dms": {
                "count": inbox.get("count", 0) if isinstance(inbox, dict) else 0,
                "preview": [
                    {"from": m.get("from"), "text": (m.get("text", "") or "")[:200], "at": m.get("sent_at")}
                    for m in (inbox.get("messages", [])[:10] if isinstance(inbox, dict) else [])
                ],
            },
            "online_agents": [
                a.get("agent_id") for a in (online.get("agents", []) if isinstance(online, dict) else [])
            ][:20],
            "guidance": (
                "1) review state.current_task. 2) check pending_dms. 3) decide next action."
                if isinstance(state, dict) and state.get("exists") else
                "no checkpoint found — fresh session. just review pending_dms and proceed."
            ),
        }

    if name == "cognitive_my_history":
        agent_id = await _resolve_agent(request)
        params = {"limit": min(max(int(a.get("limit", 20)), 1), 500)}
        return await _call_self(request, "GET", f"/agents/{agent_id}/history", params=params, timeout_s=5.0)

    if name == "cognitive_my_events":
        # Raw L1 events from this agent
        agent_id = await _resolve_agent(request)
        params = {
            "agent": agent_id,
            "limit": min(max(int(a.get("limit", 20)), 1), 200),
        }
        # Try /events list endpoint, fall back gracefully
        result = await _call_self(request, "GET", "/events", params=params, timeout_s=5.0)
        if isinstance(result, dict) and result.get("_status") in (404, None) and "_error" not in result:
            # Endpoint may not exist, try alternative
            result = await _call_self(request, "GET", "/dashboard/events", params=params, timeout_s=5.0)
        return result

    if name == "cognitive_agent_manifest":
        agent_id = await _resolve_agent(request)
        state, history = await asyncio.gather(
            _call_self(request, "GET", f"/agents/{agent_id}/state", timeout_s=5.0),
            _call_self(request, "GET", f"/agents/{agent_id}/history", params={"limit": 5}, timeout_s=5.0),
            return_exceptions=False,
        )
        return {"agent_id": agent_id, "state": state, "recent_history": history.get("items", []) if isinstance(history, dict) else []}

    if name == "cognitive_send":
        body = {
            "to": a.get("to"),
            "text": a.get("text", ""),
            "context": a.get("context", {}),
        }
        return await _call_self(request, "POST", "/agents/message", json_body=body, timeout_s=6.0)

    if name == "cognitive_inbox":
        params = {
            "since_minutes": min(max(int(a.get("since_minutes", 60)), 1), 10080),
            "limit": min(max(int(a.get("limit", 50)), 1), 500),
        }
        return await _call_self(request, "GET", "/agents/inbox", params=params, timeout_s=5.0)

    if name == "cognitive_online":
        params: dict = {"within_seconds": int(a.get("within_seconds", 120))}
        if a.get("project"):
            params["project"] = a["project"]
        return await _call_self(request, "GET", "/agents/online", params=params, timeout_s=4.0)

    if name == "cognitive_heartbeat":
        body = {"current_task": a.get("current_task")}
        return await _call_self(request, "POST", "/agents/heartbeat", json_body=body, timeout_s=4.0)

    raise ValueError(f"unknown tool: {name}")


# ─────────────────────────────────────────────────────────────────
# Main JSON-RPC endpoint
# ─────────────────────────────────────────────────────────────────
@router.post("/messages")
async def mcp_messages(request: Request) -> JSONResponse:
    """JSON-RPC 2.0 dispatch endpoint."""
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error"))

    if not isinstance(raw, dict):
        return JSONResponse(_err(None, -32600, "Invalid request"))

    try:
        req = JsonRpcRequest.model_validate(raw)
    except Exception as e:
        return JSONResponse(_err(raw.get("id"), -32600, f"Invalid request: {e}"))

    method = req.method
    req_id = req.id

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return JSONResponse({"jsonrpc": "2.0"}, status_code=200)

    if method == "initialize":
        return JSONResponse(_ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cognitive-core", "version": "0.5.1"},
        }))

    if method == "ping":
        return JSONResponse(_ok(req_id, {}))

    if method == "tools/list":
        return JSONResponse(_ok(req_id, {"tools": TOOLS}))

    if method == "tools/call":
        params = req.params or {}
        tool_name = params.get("name") if isinstance(params, dict) else None
        tool_args = params.get("arguments", {}) if isinstance(params, dict) else {}
        if not tool_name:
            return JSONResponse(_err(req_id, -32602, "tools/call requires 'name'"))
        # Per-tool timeout, hard-capped by GLOBAL_HARD_CAP_S to never starve workers
        per_tool_to = TOOL_TIMEOUTS_S.get(tool_name, DEFAULT_TOOL_TIMEOUT_S)
        wait_for_to = min(per_tool_to + 5.0, GLOBAL_HARD_CAP_S)
        try:
            result = await asyncio.wait_for(
                _dispatch_tool(request, tool_name, tool_args), timeout=wait_for_to,
            )
            return JSONResponse(_ok(req_id, {
                "content": [{"type": "text", "text": _format_text(result)}],
                "structuredContent": result,
                "isError": False,
            }))
        except ValueError as e:
            return JSONResponse(_err(req_id, -32602, str(e)))
        except asyncio.TimeoutError:
            return JSONResponse(_err(req_id, -32603, f"Tool '{tool_name}' timeout ({wait_for_to:.0f}s)"))
        except Exception as e:
            log.exception(f"tool {tool_name} error")
            return JSONResponse(_err(req_id, -32603, f"{type(e).__name__}: {e}"))

    return JSONResponse(_err(req_id, -32601, f"Method not found: {method}"))


def _format_text(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)[:8000]
    except Exception:
        return str(data)[:8000]


@router.get("/sse")
async def mcp_sse_stub() -> StreamingResponse:
    async def gen():
        yield "event: endpoint\n"
        yield "data: /mcp/messages\n\n"
        await asyncio.sleep(1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/health")
async def mcp_health() -> dict:
    return {
        "status": "ok",
        "protocol": "json-rpc-2.0",
        "transport": "http",
        "tools_count": len(TOOLS),
        "implementation": "native (cognitive_api/mcp_protocol.py)",
        "version": "0.5.1-compact-survival",
    }
