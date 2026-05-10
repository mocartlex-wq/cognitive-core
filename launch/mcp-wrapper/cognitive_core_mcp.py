#!/usr/bin/env python3
"""
cognitive_core_mcp — standalone MCP server (stdio) that exposes
Cognitive Core Rooms as tools to any MCP-compatible client (Claude Code,
Cherry Studio, Continue, etc.).

It forwards every tool call to a remote Cognitive Core server via HTTPS
using `X-Room-Key` for room-scoped auth.  No memory, no state — purely a
thin RPC bridge.

INSTALL
-------
    pip install --user cognitive-core-mcp
        # or:
    pip install --user mcp httpx
    curl -fsSL https://raw.githubusercontent.com/cognitive-core/launch/main/mcp-wrapper/cognitive_core_mcp.py \
        -o ~/.local/bin/cognitive_core_mcp && chmod +x ~/.local/bin/cognitive_core_mcp

CONFIGURE Claude Code
---------------------
Add to ``~/.claude/settings.json`` (or per-project ``.claude/settings.json``):

    {
      "mcpServers": {
        "cogcore-rooms": {
          "command": "cognitive_core_mcp",
          "env": {
            "COGCORE_URL":      "https://your-server.example",
            "COGCORE_AGENT_ID": "alice",
            "COGCORE_ROOM_KEY": "default-room-key"
          }
        }
      }
    }

Restart Claude Code.  You'll see ``cognitive_room_*`` tools in the picker.

ENV VARS
--------
- ``COGCORE_URL``      base URL of the rooms service          (required)
- ``COGCORE_AGENT_ID`` your default agent identity            (required)
- ``COGCORE_ROOM_KEY`` default room api_key for tools that
                       don't take ``room_key`` explicitly     (optional)
- ``COGCORE_TIMEOUT``  HTTP timeout in seconds (default 60)
- ``COGCORE_LOG``      ``debug``/``info``/``warning`` (default ``warning``)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    print("ERROR: pip install httpx", file=sys.stderr)
    sys.exit(1)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:  # pragma: no cover
    print("ERROR: pip install mcp", file=sys.stderr)
    sys.exit(1)


# ── Config ──────────────────────────────────────────────────────────────
REMOTE = os.environ.get("COGCORE_URL", "http://localhost:9098").rstrip("/")
AGENT_ID = os.environ.get("COGCORE_AGENT_ID", "")
DEFAULT_KEY = os.environ.get("COGCORE_ROOM_KEY", "")
TIMEOUT = float(os.environ.get("COGCORE_TIMEOUT", "60"))

logging.basicConfig(
    level=os.environ.get("COGCORE_LOG", "warning").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("cognitive-core-mcp")

if not AGENT_ID:
    log.warning("COGCORE_AGENT_ID is empty — tools that need an agent_id will fail.")

server: Server = Server("cognitive-core-rooms")


# ── HTTP helper ─────────────────────────────────────────────────────────
async def http_call(
    method: str,
    path: str,
    room_key: str | None = None,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = room_key or DEFAULT_KEY
    if key:
        headers["X-Room-Key"] = key
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        url = f"{REMOTE}{path}"
        log.debug("→ %s %s key=%s", method, url, "set" if key else "none")
        r = await client.request(method, url, headers=headers, json=payload, params=params)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}


def _err(e: Exception) -> list[TextContent]:
    if isinstance(e, httpx.HTTPStatusError):
        msg = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
    else:
        msg = f"{type(e).__name__}: {e}"
    log.warning("tool error: %s", msg)
    return [TextContent(type="text", text=json.dumps({"error": msg}, ensure_ascii=False))]


def _ok(data: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


# ── Tool definitions ────────────────────────────────────────────────────
TOOLS: list[Tool] = [
    Tool(
        name="cognitive_room_create",
        description="Create a new collaboration room. Returns {room_id, api_key} — store the api_key, it's your only auth.",
        inputSchema={
            "type": "object",
            "properties": {
                "name":    {"type": "string", "description": "Human-readable room name."},
                "creator": {"type": "string", "description": "Agent id of the creator (defaults to COGCORE_AGENT_ID)."},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="cognitive_room_join",
        description="Register the current agent as a participant in an existing room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string", "description": "X-Room-Key. Defaults to COGCORE_ROOM_KEY."},
                "agent_id": {"type": "string", "description": "Defaults to COGCORE_AGENT_ID."},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="cognitive_room_post",
        description="Broadcast a message into a room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string"},
                "agent_id": {"type": "string"},
                "text":     {"type": "string"},
            },
            "required": ["room_id", "text"],
        },
    ),
    Tool(
        name="cognitive_room_ask",
        description=(
            "Ask a question and long-poll for an answer. Hangs up to `timeout` seconds. "
            "If target is offline, server returns a DeepSeek-generated proxy answer "
            "tagged `[proxy-tentative]` that the real agent may override later."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string"},
                "asker":    {"type": "string", "description": "Defaults to COGCORE_AGENT_ID."},
                "wait_for": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent ids you expect to answer.",
                },
                "text":    {"type": "string"},
                "timeout": {"type": "integer", "default": 25, "minimum": 1, "maximum": 60},
            },
            "required": ["room_id", "wait_for", "text"],
        },
    ),
    Tool(
        name="cognitive_room_answer",
        description="Post an answer to a pending question_id.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":     {"type": "string"},
                "room_key":    {"type": "string"},
                "question_id": {"type": "string"},
                "agent_id":    {"type": "string"},
                "text":        {"type": "string"},
            },
            "required": ["room_id", "question_id", "text"],
        },
    ),
    Tool(
        name="cognitive_room_messages",
        description="List recent broadcast messages in a room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string"},
                "limit":    {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="cognitive_room_participants",
        description="List active participants and their last-seen timestamps.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string"},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="cognitive_room_pending",
        description="List unanswered questions in a room.",
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string"},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="cognitive_room_sync_pending",
        description=(
            "Wake-up handoff: pending questions addressed to me + any proxy answers "
            "the server generated while I was offline (which I can override)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "room_id":  {"type": "string"},
                "room_key": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            "required": ["room_id"],
        },
    ),
    Tool(
        name="cognitive_room_health",
        description="Ping the rooms service. Returns version + uptime + db status.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}
    room_id = args.get("room_id", "")
    room_key = args.get("room_key") or DEFAULT_KEY
    agent_id = args.get("agent_id") or AGENT_ID
    asker = args.get("asker") or AGENT_ID

    try:
        if name == "cognitive_room_create":
            return _ok(await http_call(
                "POST", "/rooms",
                payload={"name": args["name"], "creator": args.get("creator") or AGENT_ID},
            ))

        if name == "cognitive_room_join":
            return _ok(await http_call(
                "POST", f"/rooms/{room_id}/join",
                room_key=room_key, payload={"agent_id": agent_id},
            ))

        if name == "cognitive_room_post":
            return _ok(await http_call(
                "POST", f"/rooms/{room_id}/post",
                room_key=room_key,
                payload={"agent_id": agent_id, "text": args["text"]},
            ))

        if name == "cognitive_room_ask":
            return _ok(await http_call(
                "POST", f"/rooms/{room_id}/ask",
                room_key=room_key,
                payload={
                    "asker":    asker,
                    "wait_for": args["wait_for"],
                    "text":     args["text"],
                    "timeout":  int(args.get("timeout", 25)),
                },
            ))

        if name == "cognitive_room_answer":
            return _ok(await http_call(
                "POST", f"/rooms/{room_id}/answer/{args['question_id']}",
                room_key=room_key,
                payload={"agent_id": agent_id, "text": args["text"]},
            ))

        if name == "cognitive_room_messages":
            return _ok(await http_call(
                "GET", f"/rooms/{room_id}/messages",
                room_key=room_key, params={"limit": int(args.get("limit", 20))},
            ))

        if name == "cognitive_room_participants":
            return _ok(await http_call("GET", f"/rooms/{room_id}/participants", room_key=room_key))

        if name == "cognitive_room_pending":
            return _ok(await http_call("GET", f"/rooms/{room_id}/pending", room_key=room_key))

        if name == "cognitive_room_sync_pending":
            return _ok(await http_call(
                "GET", f"/rooms/{room_id}/sync-pending",
                room_key=room_key, params={"agent_id": agent_id},
            ))

        if name == "cognitive_room_health":
            return _ok(await http_call("GET", "/health"))

        return _err(ValueError(f"unknown tool: {name}"))

    except Exception as e:
        return _err(e)


def main() -> None:
    log.info("cognitive_core_mcp starting → %s as agent=%s", REMOTE, AGENT_ID or "<unset>")
    asyncio.run(_run())


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
