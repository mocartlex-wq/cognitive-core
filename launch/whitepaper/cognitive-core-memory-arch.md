# Cognitive Core: a 5-Layer Memory and Cross-Platform Coordination Architecture for Autonomous Agents

> **Defensive publication. Released to the public domain on 2026-05-10. Citations
> not required but appreciated.**
>
> **Authors**: Cognitive Core Contributors. Correspondence: hello@cognitive-core.dev
>
> **Status**: pre-print. Comments welcome at github.com/mocartlex-wq/cognitive-core/discussions

## Abstract

We describe a production-deployed system for storing, consolidating, and serving
long-term memory of autonomous AI agents across heterogeneous platforms (Anthropic
Claude Code, OpenAI ChatGPT, Google Gemini, custom LLMs). The system organises
agent experience into five distinct, automatically-promoted storage tiers and
coordinates inter-agent collaboration through HTTP long-polling with a graceful
fallback to LLM-generated proxy answers when target agents are offline.

We publish the design as prior art so that no single party can patent these
techniques and restrict open implementation. The system has been MIT-licensed at
[github.com/mocartlex-wq/cognitive-core](https://github.com/mocartlex-wq/cognitive-core).

## 1. Introduction

Autonomous AI agents face two unsolved infrastructure problems:

1. **Long-term memory** — chat-history-based context windows lose information
   on session boundaries; vector retrieval alone lacks confidence calibration
   and natural decay.
2. **Cross-platform coordination** — agents on different vendors (Anthropic,
   OpenAI, Google) cannot share state without bespoke glue per pair.

Existing systems address parts of this:
- LangChain / LangGraph offer single-process orchestration but no persistent
  multi-agent memory across vendors.
- AutoGen provides multi-agent collaboration but is Python-only, in-process.
- OpenAI Assistants v1 carries thread state but locks users to OpenAI.
- Vector databases (Pinecone, Weaviate, pgvector) provide retrieval but no
  promotion / decay heuristics.
- Slack / Discord chatbots share text but lose semantic context outside the
  message body.

We present an integrated design that solves both problems with conventional,
boring infrastructure (PostgreSQL, Redis, MinIO, NATS) and a small amount of
glue logic.

## 2. The Five-Layer Memory Model

### 2.1 L1 — Raw Events

Append-only log of every observable agent action: user prompts, tool calls,
file ingests, decisions, inbound messages.

Schema (PostgreSQL):
```sql
l1_raw_events (
  id           uuid PK,
  source_agent text,
  domain       text,        -- 'agent_inbox' | 'tool_call' | 'file_ingest' | …
  raw_payload  jsonb,
  timestamp    timestamptz,
  archive      boolean DEFAULT false
)
```

**Deduplication**: a `BEFORE INSERT` trigger computes
`SHA-256(payload || agent || domain || floor(timestamp / 60s))` and silently
drops collisions. The 60-second bucket prevents identical events from a
crash-loop or retry storm but allows genuine repetitions a minute apart.

**Retention**: a daily cron prunes `WHERE archive = true AND timestamp < NOW() -
RETENTION_DAYS days` (default 14). The `archive` flag is set by the L2
consolidator to mark "safe to forget at the raw level".

### 2.2 L2 — Daily Buffers

Once per day per agent (configurable cron, default 04:00 UTC) a curator LLM
(default DeepSeek's `deepseek-chat`, swappable) summarises that agent's L1
events of the past day into a compact JSON record:

```json
{
  "agent_id": "alice",
  "date": "2026-05-10",
  "actions":     ["…"],
  "decisions":   ["…"],
  "learnings":   ["…"],
  "open_questions": ["…"]
}
```

The L2 buffer is the next session's "what happened yesterday" preamble. It is
not directly user-facing.

### 2.3 L3 — Master Knowledge

Promotion from L2 to L3 happens **only** when both criteria hold:
1. The fact appeared in **≥ N** consecutive daily buffers (default `N = 2`).
2. The curator's confidence score for the fact is **≥ τ** (default `τ = 0.6`).

Conversely, an L3 entry whose `last_used_at` exceeds `L3_STALENESS_DAYS`
(default 90) has its confidence multiplied by a decay factor and, when below
threshold, is removed.

L3 is structured into:
- `l3_master_knowledge` (general facts and preferences)
- `l3_tools_registry` (callable tools; entries unused for `TOOL_UNUSED_DAYS`,
  default 60, are pruned)
- `l3_entities` (knowledge-graph nodes)
- `l3_knowledge_links` (subject–predicate–object edges)
- `l3_domain_links` (cross-domain bridges with weight)

### 2.4 L4 — Snapshots

Periodic point-in-time snapshots of the L3 + per-agent state are written to
S3-compatible object storage (default MinIO).

Trigger: a full snapshot is written when **either** `L4_FULL_SNAPSHOT_INTERVAL_WEEKS`
(default 4) has elapsed **or** the L3 delta since the last snapshot exceeds
`L4_MIN_CHANGE_PERCENT` (default 5%). The disjunction prevents both stale
snapshots and snapshot storms.

### 2.5 L5 — Audit Log

A tamper-evident, append-only JSONL log of every privileged operation: lock
acquisitions, write-tool executions, approval grants, force-releases, GDPR
purges. Mirrored to an immutable filesystem mount in production deployments.

L5 is for compliance and post-incident forensics, not for the runtime path.

### 2.6 Promotion summary (the core algorithm)

```
event arrives
   │
   ▼
[ L1 ] ─ SHA-256 dedup, archive flag ─────────────┐
   │                                              │
   │ daily 04:00 UTC                              │
   ▼                                              │
[ L2 ] ─ LLM curator summarises per agent/day ────┤
   │                                              │
   │ if (repeated ≥ N days) AND (confidence ≥ τ)  │
   ▼                                              │
[ L3 ] ─ master knowledge + KG                    │
   │                                              │
   │ every M weeks OR Δ > P%                      │
   ▼                                              │
[ L4 ] ─ tar.gz snapshot to S3-compatible store   │
                                                  │
   privileged ops ─────────────────────────────► [ L5 ] tamper-evident
```

**Tunable parameters** (all environment-driven):

| Symbol | Default | Meaning |
|--------|---------|---------|
| `RETENTION_DAYS` | 14 | L1 prune horizon |
| `MIN_EVENTS_FOR_DAILY` | 3 | Skip L2 below this |
| `MIN_CONFIDENCE_FOR_L3` (τ) | 0.6 | Promotion confidence threshold |
| `MIN_L2_REPETITIONS_FOR_L3` (N) | 2 | Promotion repetition threshold |
| `L3_STALENESS_DAYS` | 90 | Decay onset |
| `TOOL_UNUSED_DAYS` | 60 | Tool removal |
| `L4_FULL_SNAPSHOT_INTERVAL_WEEKS` (M) | 4 | Cadence trigger |
| `L4_MIN_CHANGE_PERCENT` (P) | 5 | Delta trigger |

## 3. Cross-Platform Rooms

Rooms are virtual collaboration spaces with REST + per-room API key. Tables
`rooms`, `room_participants`, `room_messages`, `room_questions` (PostgreSQL)
plus a NATS subject `room.<id>.events` for push.

Endpoints (HTTP):
- `POST /rooms` — create, returns `{room_id, api_key}`
- `POST /rooms/{id}/join` — register agent
- `POST /rooms/{id}/post` — broadcast
- `POST /rooms/{id}/ask` — long-poll Q&A (see §4)
- `POST /rooms/{id}/answer/{qid}` — resolve a question
- `GET  /rooms/{id}/{messages,participants,pending,sync-pending}` — read

Auth is uniform: `X-Room-Key` header on every request after `/rooms`. Per-agent
identity is carried in `agent_id` field, not auth.

## 4. The B+D Orchestrator (Long-Poll + LLM Proxy Fallback + Async Override)

### 4.1 Problem

Asker A wants to ask agent B a question. B may be:
- (B-online) running and listening
- (B-offline) on a sleeping laptop, off network, in a different timezone

Existing solutions: synchronous chat (A blocks until B online), webhooks (A
must build a server), polling (B checks every N minutes — high latency, wasteful).

### 4.2 Algorithm

On `POST /ask` with `wait_for: ["B"]`, `timeout: T`:

```
1. The server reads B's last_seen_at heartbeat.
2. If now - last_seen_at < ONLINE_THRESHOLD (default 90 s):
     The HTTP response is held open up to T seconds.
     A NATS subscription on room.<id>.events is established for the
     specific question_id.
     When B issues POST /answer, the response unblocks and returns the answer.
3. If B is offline:
     The server waits PROXY_FALLBACK_AFTER seconds (default 5) in case B
     comes online.
     If still no answer, the curator LLM is invoked with the question and
     room context. The reply is prefixed with the marker
       [proxy-tentative for B may-override]
     and stored under answered_by='B-proxy' (NOT 'B' — preserves the
     question as 'pending' for the real B).
     The response is returned to A immediately.
4. Later, when B issues GET /sync-pending?agent_id=B, B sees the question
   plus the proxy answer. B may issue POST /answer with a real reply, which
   replaces the proxy in the room's history of record.
```

### 4.3 Properties

- A never blocks beyond T (bounded latency)
- B never loses context (sync-pending shows everything that happened while
  asleep)
- Hallucination is bounded: the marker is text-level, easy for a downstream
  consumer to filter; the override path is one tool call

### 4.4 Push pipeline

The "real-time" feel comes from a sub-second push pipeline:

1. `POST /answer/{qid}` writes a row to `room_messages`.
2. `AFTER INSERT` trigger fires `pg_notify('room_event', payload)`.
3. A small Python service holds `LISTEN room_event` and republishes on NATS
   subject `room.<id>.events`.
4. The long-poll handler in §4.2 step 2 is awoken and the response unblocks.

End-to-end measured latency on a single i5-7500 host: median 280 ms.

## 5. Footprint and deployability

The full stack runs in 8 Docker containers:

| Container | Memory idle | Purpose |
|-----------|-------------|---------|
| postgres (pgvector) | ~120 MB | L1–L5 storage |
| redis (-stack) | ~80 MB | L0 blackboard, locks, RediSearch |
| minio | ~150 MB | L4 snapshots |
| nats (JetStream + WebSocket) | ~30 MB | event bus |
| api (FastAPI) | ~250 MB | memory endpoints, MCP server |
| mcp | ~120 MB | Claude-Code-facing MCP server |
| rooms | ~60 MB | rooms HTTP service |
| pg-to-nats | ~40 MB | push bridge |

**Total: ~850 MB RAM at idle, < 3% CPU on a 2017-era 4-core CPU.**

Bootstrap: `curl … | bash` clones, generates random secrets, brings everything
up in under 60 seconds.

## 6. Prior art

The constituent ideas are not novel:

- **Tiered storage** — DBMS hot/warm/cold has been standard since the 1990s.
- **Repetition + confidence promotion** — derives from spaced-repetition
  learning systems (Leitner box, Anki) and from DeepMind's experience replay
  buffers (Mnih et al., 2015).
- **Forgetting curve / staleness decay** — Ebbinghaus, 1885.
- **LLM-curated summarisation** — RAG patterns, OpenAI cookbook, 2023.
- **Long-poll for low-latency Q&A** — predates the web.
- **Audit log with immutable mount** — AWS QLDB and Azure Confidential Ledger.

The contribution of this work is **the integration**: the specific combination
of these techniques, the parameter defaults, the deduplication trigger shape,
and the marker-based proxy-override flow.

## 7. License and licensing intent

The implementation is released under the MIT License at
[github.com/mocartlex-wq/cognitive-core](https://github.com/mocartlex-wq/cognitive-core).

This document is released to the public domain. The authors specifically intend
this publication to constitute prior art under §102 of the U.S. Patent Act and
the corresponding provisions of the European Patent Convention and Russian
patent law, so as to prevent third parties from patenting the techniques
described here and restricting open implementation.

## Acknowledgements

Built largely with [Claude Code](https://claude.com/claude-code) as a pair-
programming partner. Curator LLM defaults to [DeepSeek](https://deepseek.com/),
whose generous free tier made early iteration possible. Thanks to the
PostgreSQL, Redis, MinIO, and NATS communities, on whose shoulders this stands.

## How to cite

```
@misc{cognitivecore2026,
  title  = {{Cognitive Core}: a 5-Layer Memory and Cross-Platform
            Coordination Architecture for Autonomous Agents},
  author = {{Cognitive Core Contributors}},
  year   = {2026},
  url    = {https://github.com/mocartlex-wq/cognitive-core/blob/main/launch/whitepaper/cognitive-core-memory-arch.md},
  note   = {Defensive publication}
}
```
