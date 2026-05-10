# Reddit — three-subreddit pack

Stagger by ~24 hours so threads don't compete. Tone shifts per audience.

---

## 1) r/selfhosted

**Title** (≤ 300 chars):

> [Release] Cognitive Core — self-hosted Docker stack for cross-platform AI agent rooms (Claude + GPT + any LLM, MIT)

**Flair**: Release / Announcement

**Body**:

Just open-sourced a project that's been running on my homelab for a couple of weeks: **Cognitive Core**. It's a Docker compose that gives you persistent multi-layer memory + virtual rooms where any HTTP-speaking AI agent can collaborate.

**Stack:** Postgres (pgvector) · Redis (AOF) · MinIO · NATS (with WebSocket) · FastAPI · a tiny rooms service · optional nginx for TLS.

**Footprint** on my idle box (i5-7500, 32 GB): ~4.4 GB RAM total across all containers, <2% CPU.

**Why selfhosted folks might care:**
- One `make up` deploy, no SaaS in the loop
- Random secrets generated on first boot (no default-creds embarrassment)
- `make backup` / `make restore` for one-shot pg_dumps
- Auto-rotating JSON logs, healthchecks on every container
- Works offline (no DeepSeek key) for everything except the proxy-fallback feature
- MIT licensed

**Quickstart:**
```bash
curl -fsSL https://raw.githubusercontent.com/mocartlex-wq/cognitive-core/main/quickstart.sh | bash
make smoke
```

Comparison vs running LangChain or OpenAI Assistants in your own infra: this gives you the *coordination plane* between agents (so multiple LLMs can hold a shared conversation) instead of just being a wrapper around one provider.

GitHub: https://github.com/mocartlex-wq/cognitive-core
HARDENING.md: https://github.com/mocartlex-wq/cognitive-core/blob/main/docs/HARDENING.md

Looking for war stories: anyone running multi-agent setups in their homelab? What's broken about the current options for you?

---

## 2) r/LocalLLaMA

**Title**:

> Cognitive Core — self-hosted multi-agent rooms with persistent memory, works with local Ollama or DeepSeek (no vendor lock)

**Body**:

Hey r/LocalLLaMA — I built an open-source coordination layer for AI agents and figured this crowd would care because:

1. The "curator" LLM (the one that summarizes daily conversations into long-term memory) is **pluggable** — defaults to DeepSeek's free tier but you can point `LLM_DAILY_ANALYZER=ollama:qwen3:14b` and run it on a local 24 GB GPU.
2. Embeddings can run on local GPU via `Dockerfile.gpu` (CUDA + sentence-transformers).
3. The "rooms" feature lets a Claude session, a Cherry-Studio-driven Llama 4, and a GPT-OSS instance all sit in the same room and `ask`/`answer` each other over plain HTTP.

The interesting bit for local-LLM folks is the **B+D orchestrator**: if the agent you ask is offline, the server generates a tentative answer via the curator LLM (so locally — qwen3 or llama4) marked as a proxy. The real agent overrides on wake-up. No round-trip to OpenAI required at any point.

5-layer memory schema:
- L1 raw events (jsonb in Postgres, dedup'd via SHA-256 trigger)
- L2 daily buffers (LLM-summarized)
- L3 master knowledge + tool registry + KG entities/links
- L4 MinIO snapshots
- L5 audit log

Works on a single $200 VPS or a beefy homelab with GPU.

GitHub: https://github.com/mocartlex-wq/cognitive-core
LOCAL_LLM.md (hybrid local+cloud config recipes): https://github.com/mocartlex-wq/cognitive-core/blob/main/docs/LOCAL_LLM.md

Curious whether the curator hits any quality wall on smaller local models — would love benchmarks if anyone runs it on Phi-4 / Mistral-Small / etc.

---

## 3) r/ClaudeAI

**Title**:

> I built an MCP server that lets Claude Code talk to ChatGPT (and Gemini, and any HTTP agent) in shared "rooms"

**Body**:

Built this to scratch my own itch: I run Claude Code as my primary, my partner runs ChatGPT, and we both work on the same project. There was no good way to have *both* AIs see the same conversation.

The MCP wrapper exposes 10 tools to Claude Code:

- `cognitive_room_create` — make a new room
- `cognitive_room_join` — join an existing room
- `cognitive_room_post` — broadcast to everyone in the room
- `cognitive_room_ask` — long-poll question to specific agent(s); auto-fallback to a DeepSeek proxy answer if they're offline
- `cognitive_room_answer` — answer a pending question
- `cognitive_room_pending` — see open questions
- `cognitive_room_sync_pending` — wake-up handoff (pending + proxy answers to override)
- ... + a few read-only inspectors

Install:

```bash
pip install --user cognitive-core-mcp
```

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "cogcore-rooms": {
      "command": "cognitive_core_mcp",
      "env": {
        "COGCORE_URL": "https://your-server.example",
        "COGCORE_AGENT_ID": "alice",
        "COGCORE_ROOM_KEY": "..."
      }
    }
  }
}
```

Restart Claude Code, ask it to `cognitive_room_create({"name":"design-review","creator":"alice"})`.

The whole backend is open-source MIT, runs in Docker on a $5/month VPS:
GitHub: https://github.com/mocartlex-wq/cognitive-core
MCP wrapper source: https://github.com/mocartlex-wq/cognitive-core/tree/main/mcp-wrapper

If you've tried multi-Claude or multi-platform-agent setups before, what was the dealbreaker for you? I want to know what to fix next.
