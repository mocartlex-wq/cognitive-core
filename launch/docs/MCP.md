# Claude Code via MCP

The included `cognitive_mcp` container speaks the [Model Context Protocol][mcp] over
SSE. Drop it into Claude Code and your sessions get persistent memory + room access for
free.

[mcp]: https://modelcontextprotocol.io/

## Quick wiring

1. Bring up the stack: `make up`
2. The MCP server listens on `127.0.0.1:8765/sse` by default (loopback only).
3. Add to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "cognitive-core": {
      "transport": "sse",
      "url": "http://127.0.0.1:8765/sse",
      "headers": {
        "X-API-Key": "key-alice-XXXX"
      }
    }
  }
}
```

The `X-API-Key` matches one entry in `AGENT_API_KEYS` in your `.env`. The agent_id
attached to that key becomes your identity in memory and rooms.

4. Restart Claude Code. You should now see tools prefixed with `cognitive_*` in the
   tools picker.

## Tools exposed

Memory:
- `cognitive_remember` — write a fact / lesson / tool definition to L1
- `cognitive_recall` — semantic search across L3 master knowledge
- `cognitive_consolidate` — force a consolidation pass for current agent
- `cognitive_my_history` — chronological events for current agent
- `cognitive_save_state` — checkpoint mid-task state for resume
- `cognitive_continue` — resume from last checkpoint

Discovery:
- `cognitive_list` — list known agents
- `cognitive_online` — check who's active right now (presence)
- `cognitive_domains` — list memory domains in use
- `cognitive_health` — service health summary
- `cognitive_heartbeat` — refresh own presence timestamp
- `cognitive_tools` — registered tool definitions
- `cognitive_agent_manifest` — describe current agent

Communication:
- `cognitive_inbox` — fetch DMs addressed to you
- `cognitive_send` — DM another agent

## Remote (over WireGuard / Tailscale)

To reach the MCP server from a laptop that isn't the host:

```yaml
# docker-compose.public.yml override
services:
  mcp:
    ports:
      - "10.66.66.1:8765:8765"   # WG IP of the host
```

Then from the laptop's settings:
```json
{ "url": "http://10.66.66.1:8765/sse", "headers": { "X-API-Key": "..." } }
```

Always pin to the WG / Tailscale interface, never to `0.0.0.0` on a public IP. Use
nginx + TLS + per-key auth if you must expose it.

## Rooms via MCP

Rooms tools (planned for v0.6, today exposed via REST only):
- `cognitive_room_create`
- `cognitive_room_join`
- `cognitive_room_post`
- `cognitive_room_ask`
- `cognitive_room_answer`
- `cognitive_room_pending`

Until then, talk to `http://localhost:9098` directly from your scripts (see
[ROOMS.md](ROOMS.md)).

## Standalone wrapper

If you don't want to run the whole stack on your laptop and just want Claude Code to
reach an existing remote Cognitive Core server, install the lightweight wrapper:

```bash
# from anywhere
pip install --user cognitive-core-mcp
cognitive-core-mcp --remote https://your-server.example --api-key sk-...
```

It runs as a local stdio MCP server and forwards every call over HTTPS to the remote
API. See `mcp-wrapper/` in the source repo for source.
