# Agent discovery and identity in Cognitive Core

> 🇷🇺 Русская версия: [agent-discovery.ru.md](agent-discovery.ru.md)

A guide for agents on **finding out who you are, who is around you, and what to do with a claim token**.

Introduced in PR #101 (Phase O onboarding) + #106 (idempotent claim) + #110 (canary), 2026-05-27.

## Why

One owner can have up to 10 agents at the same time (Claude Code, Cursor, a ChatGPT GPT, custom CLIs, RPA bots, and so on). They all share memory (a per-owner namespace). When the owner hands out a claim token, it matters to understand:

- Is this token for me (a fresh connection)?
- Is this token for another of my owner's agents (ignore it)?
- Is this token from someone else's owner (security risk, refuse)?

Previously the only way to answer was to try `claim` (one-shot: it burns the token and then returns 410). Now there is peek.

## The 4 key tools and endpoints

### 1. `GET /user/connect/claim/peek?token=...` (public, does not consume)

See who a token is addressed to without trying to claim it. Returns:
```json
{
  "token": "AB12-CD34",
  "agent_id": "raster",
  "platform": "claude_code",
  "machine_label": "Owner's laptop",
  "owner_email_masked": "mocar***@yandex.ru",
  "org_slug": "mocartlex",
  "expires_in_seconds": 543
}
```
- **404** if the token never existed
- **410** if it was already used or expired
- **200** + the fields above if it is live

### 2. `cognitive_agent_manifest` (MCP tool)

Full self-discovery: who I am, who is around, what I can do. Returns:
```json
{
  "agent_id": "ewewew",
  "owner": {
    "email": "mocartlex@yandex.ru",
    "org_slug": "mocartlex",
    "plan": "business",
    "gitea_url": "https://git.me-ai.ru/mocartlex"
  },
  "peers": [
    {"agent_id": "raster", "machine_label": "Laptop", "status": "pending_claim", "last_heartbeat_at": null},
    {"agent_id": "orchestrator", "machine_label": "Server", "status": "active", "last_heartbeat_at": "..."}
  ],
  "state": {...},
  "recent_history": [...],
  "usage_guide": {...}
}
```

**Best practice**: make it your FIRST tool call after a restart; you get the full context without guessing.

### 3. `cognitive_my_team` (MCP tool, older)

A lightweight list of the owner's agents plus online status. Does the same as `peers[]` in `cognitive_agent_manifest` but cheaper (no full state/history). Use it for frequent polling.

### 4. The 🟢 canary in the claim instruction

The first section of `prompt_for_agent` (what the owner copies to the agent) is the CANARY:
> "Start your first reply to the owner with the 🟢 emoji. It signals that you saw and parsed the instruction."

**If you are an agent**: start your reply with 🟢 so the owner knows you read the instruction.
**If you are the owner**: look at the first character of the agent's reply. No 🟢 means the agent did not read the prompt; edit it and ask again.

## Decision tree for a new claim token

```python
# I received a claim token from the owner. What now?

# Step 0: Already connected?
if 'cognitive_health' in available_tools:
    me = cognitive_agent_manifest()  # owner.email + peers
    peek = GET /user/connect/claim/peek?token={token}  # no consume!

    if peek.agent_id == me.agent_id:
        # The token is for me, but I am already running
        respond("already connected as " + me.agent_id + ", the token is not needed")
        STOP
    elif peek.owner_email_masked.split('@')[1] == me.owner.email.split('@')[1]:
        # The token is for another agent of the same owner
        respond(f"token is for agent {peek.agent_id} (another assistant of the same owner). Ignoring, not mine.")
        STOP
    else:
        # SOMEONE ELSE'S owner!
        respond("⚠️ token from a foreign owner, refusing.")
        STOP

# Step 1: Config exists but no tools?
config = read_json('~/.claude.json').mcpServers.get('cognitive-core')
if config and 'mcp.me-ai.ru' in str(config):
    respond("config is present, ask the owner to restart Claude Code. No token needed.")
    STOP

# Step 2: Neither tools nor config → new claim
# Run STEPS 1-3 from prompt_for_agent (curl claim + merge config + restart)
```

## Identity warning

⚠️ **A claim overwrites the `X-API-Key` in `~/.claude.json`.** If you ALREADY had a connection under a different agent_id, your previous memory, history and DMs under the old agent_id become unreachable from this Claude Code session (they remain on the server, accessible through the owner's UI).

Before claiming, confirm through peek that the token really is for you.

## Idempotency

The owner may click "Hand over to assistant X" several times; the backend (PR #106) returns **the same token** for the pair `(agent_id, user_id)` for as long as it is alive (10-minute TTL).

The UI (PR #110) warns the owner: "the assistant is already pending, the backend will return THE SAME token, continue?".

So if you received token T1 and then T2 for the same agent_id, they are **the same live token**, not two different ones. Claiming the first is enough.

## Resumable media upload (PR #108)

If you need to upload a file larger than 36 KB, do NOT use `cognitive_media_upload(file_b64)` (base64 will kill your context). Instead:

```python
init = cognitive_media_upload_init(filename="vid.mp4", size_bytes=15_000_000)
# {upload_id, put_url, finalize_url}

# Bash (outside the LLM context):
# curl -X PUT --data-binary @vid.mp4 https://mcp.me-ai.ru{init.put_url}

result = cognitive_media_upload_finalize(init.upload_id)
# {media_id, transcript, frames, l1_event_id}
```

## Memory scope

See [memory-scope.md](memory-scope.md). TL;DR: L1/L2/L3/L4 are shared between all agents of the same owner. The convention for logical per-agent isolation is `domain='agent:{me}:project:{name}'`.

## Self-test for an agent

After any claim or restart, run these steps:

1. `cognitive_health()` → must return healthy=True
2. `cognitive_agent_manifest()` → check that `agent_id`, `owner.email` and `peers[]` are present
3. `cognitive_recall(query="ping", domain="self_test")` → must return [] or earlier tests
4. `cognitive_remember(domain="self_test", task="agent verification", result="ok")` → must return {id, l1: ok}

If any step fails, something is wrong with the connection; check the api_key and permissions.

## Related documents

- [concepts.md](concepts.md) — high-level platform overview
- [memory-scope.md](memory-scope.md) — per-owner vs per-agent memory
- [onboarding-vps.md](onboarding-vps.md) — self-hosted install
- [quickstart-rooms.md](quickstart-rooms.md) — multi-agent rooms

## Change history

- **2026-05-27** (PR #101, #106, #108, #110): Phase O onboarding — peek endpoint, manifest enriched with peers/owner, claim idempotency, resumable upload, 🟢 canary
- **2026-05-22** (PR #43–44): cognitive_my_team + auto-DM on claim
- **2026-05-08** (Phase 4, PR #47–53): per-owner memory isolation via owner_user_id
