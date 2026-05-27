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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

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
        "name": "cognitive_media_upload",
        "description": (
            "Загрузить медиа-файл (video/image/audio/document) на сервер для анализа. "
            "Возвращает media_id + frames URLs + Whisper transcript + готовое vision-описание "
            "(mechanics_summary) для НЕ-multimodal LLM. "
            "Размер ≤ 200MB. file_b64 = base64-encoded content файла. "
            "Это серверная alternative для bash CLI cogmedia."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_b64": {
                    "type": "string",
                    "description": "base64-encoded content файла",
                },
                "filename": {
                    "type": "string",
                    "description": "original filename с расширением (e.g. 'video.mp4')",
                },
                "kind": {
                    "type": "string",
                    "enum": ["auto", "video", "image", "audio"],
                    "default": "auto",
                    "description": "auto = определить по расширению",
                },
            },
            "required": ["file_b64", "filename"],
        },
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
    {
        "name": "cognitive_my_team",
        "description": (
            "v3: список ВСЕХ агентов того же владельца аккаунта (multi-machine "
            "registry). Каждый: agent_id, machine_label, mcp_online, "
            "last_mcp_connect_at, total_events. Полезно для cross-agent "
            "коллаборации — узнать кто online, передать DM или присоединиться "
            "к общей комнате owner-а."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "only_online": {"type": "boolean", "default": False, "description": "Только агенты с MCP-online"},
            },
        },
    },

    # ─── AI Video Generation (Phase post-launch 2026-05-26) ────────────
    # Per-tenant Kling/Sora API key через /ui/profile External AI providers
    # (provider="kling_video", key="access_key|secret_key")
    {
        "name": "cognitive_video_generate",
        "description": (
            "Создать видео через Kling.ai (или Sora когда public API). "
            "Asynchronous — возвращает task_id, потом polling через "
            "cognitive_video_status. Generation занимает 30-180s в зависимости "
            "от duration и модели. Требует Kling key в /ui/profile."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string", "minLength": 3, "maxLength": 2000,
                           "description": "Текст промпта для генерации"},
                "provider": {"type": "string", "enum": ["kling_video", "sora_video"],
                             "default": "kling_video"},
                "image_url": {"type": "string",
                              "description": "Опц. — для image2video режима (URL картинки)"},
                "duration_sec": {"type": "integer", "minimum": 3, "maximum": 10, "default": 5},
                "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "default": "16:9"},
                "model_name": {"type": "string",
                               "description": "Опц. override — kling-v1 (cheap) или kling-v1-pro (best)"},
            },
        },
    },
    {
        "name": "cognitive_video_status",
        "description": (
            "Poll статус задачи генерации видео. Возвращает status "
            "(queued/generating/completed/failed) + progress_pct + video_url "
            "когда completed. Вызывайте каждые 10-30 секунд после "
            "cognitive_video_generate."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "description": "task_id из cognitive_video_generate response"},
                "provider": {"type": "string", "enum": ["kling_video", "sora_video"],
                             "default": "kling_video"},
            },
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
    """Make in-process ASGI call to own FastAPI app, propagating X-API-Key
    AND X-Owner-User-Id (если уже резолвен) — внутренние endpoints читают
    его как trusted источник чтобы не дёргать БД повторно.

    Defensive timeout default 8s — overridable per-call. NEVER unbounded.
    """
    app = request.app
    api_key = request.headers.get("x-api-key", "")
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key
    # PR #23: пропагандируем owner_user_id чтобы внутренние memory-handler'ы
    # могли применить tenant-фильтр без повторного DB-lookup.
    cached_owner = getattr(request.state, "_resolved_owner_user_id", None)
    if cached_owner is None:
        # Если ещё не резолвен — попробуем взять из _resolved_agent (tuple)
        resolved = getattr(request.state, "_resolved_agent", None)
        if isinstance(resolved, tuple) and len(resolved) == 2:
            cached_owner = resolved[1]
    if cached_owner:
        headers["X-Owner-User-Id"] = str(cached_owner)

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


async def _resolve_agent_full(request: Request) -> tuple[str, str | None]:
    """Резолвит api_key в (agent_id, owner_user_id). Кеширует в request.state
    чтобы не дёргать БД повторно на цепочке tool-вызовов одного request'а.

    owner_user_id может быть None для legacy env-агентов (admin-pre-provisioned).
    Для UI-созданных через /user/agents/create или claim-wizard — всегда есть.
    """
    cached = getattr(request.state, "_resolved_agent", None)
    if cached is not None:
        return cached

    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        raise ValueError("X-API-Key header required (set in MCP client config or curl -H 'X-API-Key: ...')")

    # 1. Static env JSON (быстрый lookup, admin-pre-provisioned ключи —
    #    owner_user_id у них None — это owner-уровень доступа).
    keys = _load_keys()
    for agent_id, key in keys.items():
        if key == api_key:
            result = (agent_id, None)
            request.state._resolved_agent = result
            return result

    # 2. Postgres agent_keys — claim-wizard и /user/agents/create.
    try:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT agent_id, owner_user_id::text AS owner_user_id "
                "FROM agent_keys WHERE api_key = $1 AND revoked_at IS NULL LIMIT 1",
                api_key,
            )
        if row:
            result = (row["agent_id"], row["owner_user_id"])
            request.state._resolved_agent = result
            return result
    except Exception:
        pass

    raise ValueError(
        "API key not registered. Создан через /ui/profile + «Передать помощнику» "
        "wizard? Проверьте что agent_id есть в agent_keys таблице. "
        "Иначе — admin должен добавить в /opt/cognitive-core/.env AGENT_API_KEYS."
    )


async def _resolve_agent(request: Request) -> str:
    """Backward-compat: возвращает только agent_id. Новый код — _resolve_agent_full."""
    agent_id, _ = await _resolve_agent_full(request)
    return agent_id


async def _resolve_owner(request: Request) -> str | None:
    """Возвращает owner_user_id (str UUID) или None для legacy env-агентов.

    Используется в memory-tools для WHERE owner_user_id = $1 фильтрации.
    Для env-агентов (owner=None) подразумевается «admin access» — фильтр
    не применяется (видят всё, для backward-compat и admin-debug).
    """
    _, owner = await _resolve_agent_full(request)
    return owner


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
        # Raw L1 events from this agent — fetch /dashboard/recent-events + filter
        agent_id = await _resolve_agent(request)
        limit = min(max(int(a.get("limit", 20)), 1), 200)
        # Fetch a wider window then filter in Python (endpoint has no agent filter)
        result = await _call_self(
            request, "GET", "/dashboard/recent-events",
            params={"limit": min(limit * 5, 500)}, timeout_s=5.0,
        )
        # /dashboard/recent-events returns {"items": [...], "count": N}
        # Items have field "agent" (not "source_agent")
        if isinstance(result, dict) and "items" in result:
            events = [
                e for e in result.get("items", [])
                if e.get("agent") == agent_id or e.get("source_agent") == agent_id
            ][:limit]
            return {"agent_id": agent_id, "count": len(events), "events": events}
        return result

    if name == "cognitive_agent_manifest":
        agent_id = await _resolve_agent(request)
        state, history = await asyncio.gather(
            _call_self(request, "GET", f"/agents/{agent_id}/state", timeout_s=5.0),
            _call_self(request, "GET", f"/agents/{agent_id}/history", params={"limit": 5}, timeout_s=5.0),
            return_exceptions=False,
        )
        # Detect legacy connection URL — informational advisory только.
        # Раньше требовали "тихо без вопросов" — но это выглядело как prompt-injection
        # и хорошие агенты справедливо отказывались. Теперь — just FYI: пусть агент
        # сам решает, обсудить с owner или прямо предложить замену. Legacy продолжает
        # работать неограниченно, миграция не срочная.
        connection_host = (request.headers.get("host") or "").lower().split(":")[0]
        _LEGACY_HOSTS = {
            "mcp.xn----8sbwawqx4fza.xn--p1ai",
            "mcp.ии-память.рф",
        }
        _PRIMARY_URL = "https://mcp.me-ai.ru"
        connection_advisory = None
        if connection_host in _LEGACY_HOSTS:
            connection_advisory = {
                "severity": "info",
                "type": "url_aliased",
                "current_host": connection_host,
                "primary_url": _PRIMARY_URL,
                "note": (
                    "FYI: ты сейчас подключён через legacy alias '" + connection_host + "'. "
                    "Primary canonical URL — '" + _PRIMARY_URL + "'. Старый alias продолжит "
                    "работать неограниченно, миграция НЕ срочная. Если хочешь — предложи "
                    "owner-у обновить ~/.claude.json (это human-readable URL, ASCII, его "
                    "проще набирать). Решение оставляю на тебя и owner-а."
                ),
            }
        # PR #22: добавлен usage_guide — категории tools + best practices.
        # Owner: «дай в нем информацию и на память, на все функции, что бы ИИ
        # их использовала». Claim-промпт инструктирует агента вызвать этот
        # tool первым после connect — тогда сам узнает что и как использовать.
        usage_guide = {
            "what_is_this": (
                "Cognitive Core — 5-слойная самохостимая память + комнаты + DM "
                "для AI-агентов. У тебя теперь есть постоянная память между "
                "сессиями + способ общаться с другими агентами того же владельца."
            ),
            "tool_categories": {
                "memory": {
                    "save": ["cognitive_remember (важный факт/lesson/decision)"],
                    "search": ["cognitive_recall (семантический поиск по L3)"],
                    "history": ["cognitive_my_history", "cognitive_continue", "cognitive_my_events"],
                    "state": ["cognitive_save_state (working memory)", "cognitive_resume (read)"],
                    "manual": ["cognitive_consolidate (force L1→L2→L3 trigger)"],
                },
                "rooms": {
                    "join_read": ["room_join", "room_read"],
                    "post": ["room_post (broadcast)"],
                    "ask_answer": ["room_ask (long-poll Q)", "room_pending", "room_answer"],
                },
                "dm": {
                    "send_recv": ["cognitive_send", "cognitive_inbox"],
                    "discover": ["cognitive_online", "cognitive_my_team"],
                },
                "utils": ["cognitive_health", "cognitive_heartbeat", "cognitive_domains", "cognitive_list", "cognitive_tools"],
            },
            "best_practices": [
                "После каждого важного решения/lesson → cognitive_remember (lessons-поле, без — — двойного дефиса и без ; ).",
                "ПЕРЕД новой задачей → cognitive_recall(query) — проверь не делал ли уже похожее.",
                "Длинная сессия → cognitive_save_state в начале (snapshot working memory).",
                "Команда — через rooms (открытый коллективный чат) или DM (приватный 1-to-1).",
                "Heartbeat каждые ~5 мин если ты долгоживущий daemon (cognitive_heartbeat).",
                "Не печатай api_key в transcript / логах / коммитах — он секретный.",
            ],
            "rate_limits": {
                "remember": "≤ 60 events/min",
                "recall": "≤ 30 queries/min (LLM-семантический поиск дорогой)",
                "room_post": "≤ 10/min per room (anti-spam)",
                "consolidate": "manual triggers throttled to 1/hour per domain",
            },
            "your_identity": {
                "agent_id": agent_id,
                "note": "owner создаёт тебя через /ui/profile, machine_label привязан к hostname. Если хочешь сменить роль — попроси owner-а.",
            },
            "more_info": "https://mcp.me-ai.ru/sandbox — все endpoints с примерами",
        }
        response = {
            "agent_id": agent_id,
            "state": state,
            "recent_history": history.get("items", []) if isinstance(history, dict) else [],
            "usage_guide": usage_guide,
        }
        if connection_advisory:
            response["connection_advisory"] = connection_advisory
        return response

    if name == "cognitive_media_upload":
        # P0 (2026-05-26 per ewewew feedback): MCP tool вместо bash CLI cogmedia.
        # Forwards к /api/media/upload_b64 который сам определяет kind и
        # dispatch'ит к /video|image|audio analyzer'ам.
        file_b64 = a.get("file_b64")
        filename = a.get("filename")
        kind = a.get("kind", "auto")
        if not file_b64 or not filename:
            raise ValueError("file_b64 + filename required")
        body = {"file_b64": file_b64, "filename": filename, "kind": kind}
        # timeout 180s — vision providers могут долго отвечать на 12 кадров
        return await _call_self(request, "POST", "/api/media/upload_b64", json_body=body, timeout_s=180.0)

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

    if name == "cognitive_my_team":
        # v3: возвращаем agents того же owner-а. Resolve owner через api_key.
        agent_id = await _resolve_agent(request)
        only_online = bool(a.get("only_online", False))
        try:
            from app.db.postgres import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Owner_user_id текущего агента
                me = await conn.fetchrow(
                    "SELECT owner_user_id FROM agent_states WHERE agent_id = $1",
                    agent_id,
                )
                if not me or not me["owner_user_id"]:
                    return {"team": [], "note": "Этот агент не привязан к owner-у (legacy admin-key)"}

                online_filter = "AND last_mcp_connect_at > NOW() - INTERVAL '60 seconds'" if only_online else ""
                rows = await conn.fetch(
                    f"""
                    SELECT agent_id, machine_label, machine_fingerprint,
                           total_events, total_checkpoints,
                           last_mcp_connect_at, last_heartbeat_at,
                           (last_mcp_connect_at > NOW() - INTERVAL '60 seconds') AS mcp_online
                      FROM agent_states
                     WHERE owner_user_id = $1 {online_filter}
                     ORDER BY mcp_online DESC NULLS LAST, last_heartbeat_at DESC NULLS LAST
                    """,
                    me["owner_user_id"],
                )
            team = []
            for r in rows:
                d = dict(r)
                d["is_me"] = (d["agent_id"] == agent_id)
                for k in ("last_mcp_connect_at", "last_heartbeat_at"):
                    v = d.get(k)
                    if v:
                        d[k] = v.isoformat()
                team.append(d)
            return {
                "team_size": len(team),
                "online_count": sum(1 for t in team if t.get("mcp_online")),
                "team": team,
                "me": agent_id,
            }
        except Exception as e:
            return {"_error": f"my_team failed: {e}"}

    # ─── AI Video Generation handlers (post-launch 2026-05-26) ───────────────
    if name == "cognitive_video_generate":
        prompt = a.get("prompt", "")
        if not prompt or not prompt.strip():
            raise ValueError("cognitive_video_generate: prompt обязателен (3-2000 chars)")
        body: dict[str, Any] = {
            "prompt": prompt,
            "provider": a.get("provider", "kling_video"),
            "duration_sec": int(a.get("duration_sec", 5)),
            "aspect_ratio": a.get("aspect_ratio", "16:9"),
        }
        if a.get("image_url"):
            body["image_url"] = a["image_url"]
        if a.get("model_name"):
            body["model_name"] = a["model_name"]
        # Submit может занять ~30s до Kling response — даём 35s timeout
        return await _call_self(request, "POST", "/api/video/generate",
                                json_body=body, timeout_s=35.0)

    if name == "cognitive_video_status":
        task_id = a.get("task_id")
        provider = a.get("provider", "kling_video")
        if not task_id:
            raise ValueError("cognitive_video_status: task_id обязателен")
        return await _call_self(request, "GET", f"/api/video/status/{task_id}",
                                params={"provider": provider}, timeout_s=12.0)

    raise ValueError(f"unknown tool: {name}")


# ─────────────────────────────────────────────────────────────────
# Main JSON-RPC endpoint
# ─────────────────────────────────────────────────────────────────
async def _handle_jsonrpc(request: Request, raw: Any) -> dict:
    """Process JSON-RPC request → return response dict (без HTTP wrapping)."""
    if not isinstance(raw, dict):
        return _err(None, -32600, "Invalid request")

    try:
        req = JsonRpcRequest.model_validate(raw)
    except Exception as e:
        return _err(raw.get("id"), -32600, f"Invalid request: {e}")

    method = req.method
    req_id = req.id

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return {}  # notification — no response

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cognitive-core", "version": "0.5.1"},
        })

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = req.params or {}
        tool_name = params.get("name") if isinstance(params, dict) else None
        tool_args = params.get("arguments", {}) if isinstance(params, dict) else {}
        if not tool_name:
            return _err(req_id, -32602, "tools/call requires 'name'")
        per_tool_to = TOOL_TIMEOUTS_S.get(tool_name, DEFAULT_TOOL_TIMEOUT_S)
        wait_for_to = min(per_tool_to + 5.0, GLOBAL_HARD_CAP_S)
        try:
            result = await asyncio.wait_for(
                _dispatch_tool(request, tool_name, tool_args), timeout=wait_for_to,
            )
            return _ok(req_id, {
                "content": [{"type": "text", "text": _format_text(result)}],
                "structuredContent": result,
                "isError": False,
            })
        except ValueError as e:
            return _err(req_id, -32602, str(e))
        except asyncio.TimeoutError:
            return _err(req_id, -32603, f"Tool '{tool_name}' timeout ({wait_for_to:.0f}s)")
        except Exception as e:
            log.exception(f"tool {tool_name} error")
            return _err(req_id, -32603, f"{type(e).__name__}: {e}")

    return _err(req_id, -32601, f"Method not found: {method}")


@router.post("/messages")
async def mcp_messages(request: Request) -> JSONResponse:
    """JSON-RPC 2.0 dispatch endpoint.

    Два режима ответа:
    1. `?session_id=XYZ` → enqueue response в SSE-stream session-а,
       вернуть 202 Accepted с пустым body. Это proper MCP-SSE flow.
    2. Без session_id → response inline в HTTP body (legacy curl mode).
    """
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error"))

    session_id = request.query_params.get("session_id", "").strip()
    response = await _handle_jsonrpc(request, raw)

    # Если это notification (нет response) — просто 200 пусто
    if not response:
        return JSONResponse({}, status_code=202 if session_id else 200)

    # SSE-routed mode: enqueue в очередь session и вернуть 202
    if session_id and session_id in _MCP_SSE_SESSIONS:
        try:
            await _MCP_SSE_SESSIONS[session_id].put(response)
            return JSONResponse({}, status_code=202)
        except Exception as e:
            log.warning("SSE enqueue failed: %s", e)
            # Fall through → inline response (degraded)

    # Legacy mode — inline HTTP response (curl tests, no-SSE clients)
    return JSONResponse(response)


def _format_text(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)[:8000]
    except Exception:
        return str(data)[:8000]


async def _mark_mcp_connected(request: Request) -> None:
    """Записать «MCP-клиент только что подключился» в agent_states.

    Используется для UI presence indicator (зелёный/серый dot в /ui/profile).
    Best-effort — если postgres недоступен или agent не resolved, тихо
    пропускаем (не блокируем сам SSE handshake).

    v3: при ПЕРВОМ connect (first_mcp_connect_at IS NULL) — auto-DM всем
    online агентам того же owner-а «🟢 новый агент подключился».
    """
    try:
        agent_id = await _resolve_agent(request)
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            # UPDATE возвращает прежнее значение first_mcp_connect_at и owner_user_id
            row = await conn.fetchrow(
                """
                UPDATE agent_states
                   SET last_mcp_connect_at = NOW(),
                       first_mcp_connect_at = COALESCE(first_mcp_connect_at, NOW())
                 WHERE agent_id = $1
                RETURNING owner_user_id::text AS owner_user_id,
                          (first_mcp_connect_at = last_mcp_connect_at) AS is_first,
                          machine_label
                """,
                agent_id,
            )
            if not row or not row["owner_user_id"] or not row["is_first"]:
                return  # legacy admin agent OR не первый коннект
            # Auto-DM всем online агентам того же owner-а
            online_peers = await conn.fetch(
                """
                SELECT agent_id FROM agent_states
                 WHERE owner_user_id = $1::uuid
                   AND agent_id != $2
                   AND last_mcp_connect_at > NOW() - INTERVAL '60 seconds'
                """,
                row["owner_user_id"], agent_id,
            )
            if not online_peers:
                return
            ml = row["machine_label"] or "?"
            for peer in online_peers:
                try:
                    await conn.execute(
                        """
                        INSERT INTO l1_raw_events (source_agent, domain, raw_payload)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        "server-runtime",
                        "agent_inbox",
                        json.dumps({
                            "to": peer["agent_id"],
                            "from": "server-runtime",
                            "text": f"🟢 Новый агент `{agent_id}` (machine: {ml}) подключился к команде. Используй cognitive_my_team чтобы увидеть всех.",
                            "context": {"event": "agent_joined", "agent_id": agent_id, "machine_label": ml},
                        }, ensure_ascii=False),
                    )
                except Exception:
                    pass
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# Session-based SSE message routing (proper MCP SSE transport)
# ─────────────────────────────────────────────────────────────────
# In-memory session store: session_id → asyncio.Queue
# POST /mcp/messages?session_id=X enqueues response, SSE-generator
# pulls and yields as `event: message` frames.
# Single-process — для multi-worker нужен Redis pubsub, но для нашего
# single-uvicorn-worker config достаточно.
import uuid as _uuid

_MCP_SSE_SESSIONS: dict[str, asyncio.Queue] = {}


@router.get("/sse")
async def mcp_sse(request: Request) -> StreamingResponse:
    """Proper MCP SSE transport с session-id и message-routing.

    Flow по MCP spec:
    1. Client opens GET /sse
    2. Server assigns session_id, sends `event: endpoint
       data: /mcp/messages?session_id=XYZ`
    3. Client POSTs JSON-RPC to /mcp/messages?session_id=XYZ
    4. POST handler responds 202 Accepted + enqueues response
    5. SSE generator pulls from queue → yields `event: message
       data: {jsonrpc-response}`
    6. Client SDK matches response.id, marks tool/method complete

    Без session_id (legacy /mcp/messages) — response idёт в HTTP body
    (backward compat для curl-тестов).
    """
    # Mark agent as MCP-connected (for /ui/profile green dot UI)
    await _mark_mcp_connected(request)

    session_id = _uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _MCP_SSE_SESSIONS[session_id] = queue
    log.info("mcp_sse session opened: %s", session_id[:8])

    async def gen():
        try:
            # Initial endpoint event — session-bound POST URL
            yield "event: endpoint\n"
            yield f"data: /mcp/messages?session_id={session_id}\n\n"
            # Pull responses from queue. Timeout 25s → send keep-alive event:ping.
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield "event: message\n"
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive — event:ping чтобы Claude Code SDK не закрыл по inactivity
                    yield "event: ping\n"
                    yield "data: {}\n\n"
        except asyncio.CancelledError:
            log.info("mcp_sse session closed: %s", session_id[:8])
            return
        finally:
            _MCP_SSE_SESSIONS.pop(session_id, None)

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
