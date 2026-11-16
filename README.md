# Cognitive Core

> 🇷🇺 Русская версия: [README.ru.md](README.ru.md)

**Long-term memory and a shared workspace for AI agents, exposed over MCP.**

Cognitive Core is a self-hosted server that gives any MCP-compatible assistant (Claude Code, Claude Desktop, Cursor, ChatGPT, LangChain agents) a persistent memory that actually learns: raw events are distilled by an LLM curator into daily summaries and then into durable knowledge, so an agent remembers *lessons*, not every keystroke. On top of that memory sit multi-agent rooms with direct messages, a media pipeline (video → frames + Whisper transcript), AI video generation, a built-in git server and a billing layer for running it as a SaaS.

[![Version](https://img.shields.io/badge/version-v0.6.0-blue.svg)](CHANGELOG.md)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Postgres 16 + pgvector](https://img.shields.io/badge/postgres-16+pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![Tests](https://img.shields.io/badge/tests-369%20passing-success.svg)](#testing)
[![Stress](https://img.shields.io/badge/stress-171%20ev%2Fs%20p95%20111ms-success.svg)](scripts/stress_test.py)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## ⚡ Try it in one minute, no install

**Public instance:** [https://mcp.me-ai.ru](https://mcp.me-ai.ru) (legacy alias: https://mcp.ии-память.рф)

1. Sign in with email + one-time code at [`/ui/login`](https://mcp.me-ai.ru/ui/login)
2. Click "Hand over to assistant" in [`/ui/profile`](https://mcp.me-ai.ru/ui/profile) for a one-click setup of Claude Code / Cursor / ChatGPT
3. Restart your AI client and you get 32 MCP tools (`cognitive_remember`, `cognitive_recall`, `room_post`, `cognitive_video_generate`, `cognitive_media_upload_init`, ...)

Free tier: 10k events/day, 1 GB media, 10 agents. Pro ($5/mo) is 10x everything plus priority support. Plans: [`/ui/pricing`](https://mcp.me-ai.ru/ui/pricing).

## 📦 What's inside (2026-07-05)

| Feature | Endpoint / Tool | Docs |
|---|---|---|
| **5-layer memory** (L1→L2→L3→L4 + OP) | `cognitive_remember`, `cognitive_recall` | [concepts.md](docs/concepts.md) |
| **Multi-agent rooms** + DMs | `room_*` (7 tools), `cognitive_send` | [quickstart-rooms.md](docs/quickstart-rooms.md) |
| **Media pipeline** (video → frames + Whisper) | `cognitive_media_upload` | server-side, behind nginx |
| **AI video generation** (Kling / Sora) | `cognitive_video_generate` | [quickstart-video-generation.md](docs/quickstart-video-generation.md) |
| **External vision providers** (per-tenant keys) | Qwen / MiniMax / GigaChat / YandexGPT / Claude / OpenAI / Gemini | [external-providers.md](docs/external-providers.md) |
| **Self-hosted git** (Gitea at git.me-ai.ru) | Standard git protocol | [gitea-tenant-onboarding.md](docs/gitea-tenant-onboarding.md) |
| **Operating Rules** (Phase 6) | 5 core rules auto-injected into the system prompt | rules via [`/user/rules`](https://mcp.me-ai.ru/ui/profile) |
| **Billing** (Stripe + YooKassa) | `/api/billing/checkout/{tier}` | [quickstart-billing.md](docs/quickstart-billing.md) |
| **Multi-tenant isolation** (Phase 4) | `owner_user_id` on every L1–L4 query | per-tenant MinIO prefix |
| **Resumable upload** (PR #108, no context cap) | `cognitive_media_upload_init/_finalize` | curl PUT bypasses base64 in the LLM context |
| **Agent discovery + onboarding** (PR #101, #106, #110) | `claim/peek`, `cognitive_agent_manifest.peers[]`, 🟢 canary, idempotent claim | [agent-discovery.md](docs/agent-discovery.md), [memory-scope.md](docs/memory-scope.md) |
| **Self-hosted instance** (one-liner) | `curl /static/install-self-hosted.sh \| sudo bash` | [onboarding-vps.md](docs/onboarding-vps.md) |
| **Russian enterprise compliance** (152-FZ) | DPA + FSTEC-21 UZ-3 | [compliance-152fz.md](docs/compliance-152fz.md) (Russian) |

Full documentation index: [docs/index.md](docs/index.md).

## Why

Most "AI memory" products are `add()` + `search()` on top of a vector database. Cognitive Core does it differently: **raw event → daily analysis → weekly synthesis → long-term knowledge**, with an LLM curator filtering noise at every transition. The result: your agent **remembers the lessons it learned, not every keystroke**.

After one minute of the local demo the system holds 18 events, 3 daily buffers and roughly 30 learned knowledge entries.

## Who it's for

| Profile | Why |
|---|---|
| Solo AI developer | Persistent memory for Claude Desktop / Cursor / Cherry Studio over MCP |
| AI startup (5–50 people) | Accumulated experience of a team of agents, with an audit log |
| Enterprise (regulated) | Self-hosted compliance (GDPR, SOC 2), full L5 audit, on-prem |

## 30 seconds, locally

```bash
git clone <repo-url> cognitive-core && cd cognitive-core
cp .env.example .env  # add your DEEPSEEK_API_KEY
docker compose up -d
open http://localhost:9001/   # landing page with a "Run demo" button
```

## Your own VPS (one-liner, ~10 min)

For tenants who need **full isolation** rather than the shared cloud:

```bash
curl -fsSL https://mcp.me-ai.ru/static/install-self-hosted.sh | sudo bash
```

The script asks for a domain, an email and SMTP credentials, issues TLS certificates, brings up docker-compose, runs migrations and creates an admin account. Full guide: [`docs/onboarding-vps.md`](docs/onboarding-vps.md).

## Production server (one command)

```bash
ssh user@your-vps
git clone <repo-url> /opt/cognitive-core && cd /opt/cognitive-core
DOMAIN=cognitive.example.com EMAIL=admin@example.com bash install-server.sh
```

Ten minutes later you have a working HTTPS stack with automatic backups, MCP over SSE / Streamable HTTP for remote clients and systemd auto-start. See [`DEPLOY-SERVER.md`](DEPLOY-SERVER.md).

## How it compares

| | Mem0 | Letta | Zep | **Cognitive Core** |
|---|---|---|---|---|
| Self-hosted | ✅ | ⚠️ heavy | ⚠️ paid | ✅ one docker compose |
| Multi-layer consolidation | ❌ | ❌ | ❌ | ✅ **3 LLM levels** |
| Per-agent state checkpoint | ❌ | ✅ | ❌ | ✅ + shared memory |
| Audit log (L5) | ❌ | ❌ | ❌ | ✅ append-only |
| Snapshot + restore with SHA-256 | ❌ | ❌ | ❌ | ✅ |
| Multi-language | ❌ | ❌ | ❌ | ✅ 8 languages |
| MCP server | ⚠️ | ❌ | ❌ | ✅ stdio + SSE + Streamable HTTP |
| Production install in 10 min | ❌ | ❌ | ⚠️ | ✅ install-server.sh |

Detailed comparison with code samples: [`COMPARISON.md`](COMPARISON.md) (Russian). Market analysis: [`MARKET.md`](MARKET.md) (Russian).

## The core idea: 5 layers of memory

```
L1 raw events  →  L2 daily buffers  →  L3 master knowledge  →  L4 archive (S3)
      ↓                ↓ (LLM)               ↓ (LLM + curator)        ↓
 POST /events      nightly               weekly                 snapshots
                                              ↓
                                     OP — KNN search from agents
```

| Layer | What it stores | When it updates | Who writes |
|---|---|---|---|
| **L1** raw events | Raw log of agent experience | Immediately on `POST /events` | Agents |
| **L2** daily buffers | Per-domain daily summaries | Every night (02:00 UTC) | LLM Daily Analyzer |
| **L3** master knowledge | Confirmed knowledge + tools | Weekly + on audit | LLM Weekly Consolidator + Curator |
| **L4** snapshots | Backups of L3 in S3/MinIO | After weekly, if L3 changed | System |
| **L5** audit log | Every action in the system | Continuously | All components |
| **OP** operative | KNN search session over L3 | On `POST /operative/query` | Agents |

## Why this beats plain RAG

| Plain RAG | Cognitive Core |
|---|---|
| Stores everything | An LLM curator filters noise at every transition |
| Knowledge = text + vector | Knowledge = `{pattern, mistake, rule} + confidence + version + history` |
| Vector search | Vector search + temporal awareness + tools registry |
| No audit | Full L5 audit log: who, when, what |
| No backups | L4 snapshot/restore with SHA-256 |
| One context for everything | Domain isolation, multi-agent |

## Stack

- **Python 3.11** + **FastAPI** + **asyncpg**
- **PostgreSQL 16** with **pgvector** (HNSW for KNN)
- **Redis Stack** (RediSearch for fast KNN with TAG filters)
- **MinIO** (S3-compatible storage for L4)
- **DeepSeek V4 Pro** as the primary LLM (fallback to OpenAI / Ollama)
- **fastembed** `multilingual-e5-small` for embeddings (CPU, 384-dim, 8 languages)
- **Docker Compose**: one command to bring everything up

No npm or build step for the UI: vanilla HTML+CSS+JS, ~600 LOC.

## Install and run

### Requirements
- Docker + Docker Compose v2
- 4 GB RAM for the containers + 1 GB for the fastembed model cache

### Steps

```bash
# 1. Clone
git clone <repo-url> cognitive-core
cd cognitive-core

# 2. Configure
cp .env.example .env
# Open .env and set DEEPSEEK_API_KEY (get one at platform.deepseek.com)

# 3. Start
docker compose up -d --build

# 4. Check
curl http://localhost:9001/health
# {"healthy":true,"version":"0.6.0",...}

# 5. Load demo data (optional — also available from the UI)
python scripts/seed_demo.py --full
```

### Default ports
- `9001` — Web UI + API (FastAPI)
- `5432` — PostgreSQL
- `6379` — Redis
- `9000` / `9002` — MinIO API / Console (admin: `minioadmin`/`minioadmin`)

## Web UI

| URL | Purpose |
|---|---|
| `http://localhost:9001/` | Landing page: explanation, diagram, demo button |
| `http://localhost:9001/ui` | Dashboard: live metrics, charts, knowledge explorer |
| `http://localhost:9001/sandbox` | API sandbox: every endpoint, stage by stage |
| `http://localhost:9001/docs` | OpenAPI Swagger |

## API in brief

```bash
# Write an event into L1
curl -X POST http://localhost:9001/events \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{"source_agent":"my_agent","domain":"my_domain","payload":{"task":"...","result":"...","feedback":"positive"}}'

# Find knowledge with KNN
curl -X POST http://localhost:9001/operative/query \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{"domain":"my_domain","context":"how to ...","top_k":5}'

# Trigger daily consolidation
curl -X POST "http://localhost:9001/memory/consolidate/daily?domain=my_domain" \
  -H "X-API-Key: key-design-001"
```

Full list: [DEMO.md](DEMO.md) (Russian) or open `/docs`.

## Python SDK

```python
from cognitive import AsyncMemoryClient

async with AsyncMemoryClient(
    base_url="http://localhost:9001",
    api_key="key-design-001",
) as memory:
    # Record an experience
    await memory.remember(
        domain="codegen",
        payload={"task": "...", "result": "...", "feedback": "positive"},
    )
    # Find knowledge
    results = await memory.recall(domain="codegen", context="how to ...")
```

The SDK lives in [cognitive-client/](cognitive-client/).

## MCP server for Claude Code / Claude Desktop / Cursor

Cognitive Core speaks MCP natively. The server exposes Streamable HTTP at `/mcp` (plus legacy SSE at `/mcp/sse`); authenticate with your agent key as `X-API-Key` or `Authorization: Bearer <key>`.

```bash
# Claude Code, one line
claude mcp add --transport http cognitive-core http://localhost:9001/mcp \
  --header "Authorization: Bearer key-design-001"
```

```json
// Generic MCP client config (Cursor, Claude Desktop with a remote MCP, etc.)
{
  "mcpServers": {
    "cognitive-core": {
      "url": "http://localhost:9001/mcp",
      "headers": { "Authorization": "Bearer key-design-001" }
    }
  }
}
```

For a one-click setup against the public instance use "Hand over to assistant" in [`/ui/profile`](https://mcp.me-ai.ru/ui/profile). Local installers for Claude Desktop / Cursor / Claude Code / Cherry Studio: [`installer.sh`](installer.sh), [`installer.ps1`](installer.ps1), [`CHERRY_STUDIO.md`](CHERRY_STUDIO.md).

After a restart the client sees the memory tools (`cognitive_remember`, `cognitive_recall`, `cognitive_list`, `cognitive_tools`, `cognitive_consolidate`, `cognitive_health`, `cognitive_domains`), the `room_*` tools, direct messages, media upload and video generation. Call `cognitive_agent_manifest` for the complete list.

## Testing

```bash
# All tests inside the container
docker exec cognitive_api python -m pytest tests/ -v

# Unit tests only (no network / LLM)
docker exec cognitive_api python -m pytest tests/test_sanitizer.py tests/test_embedder.py tests/test_models.py -v

# API + integration only
docker exec cognitive_api python -m pytest tests/test_api.py tests/test_dashboard.py -v
```

Current categories:
- `test_models.py` — Pydantic schemas (12 tests)
- `test_sanitizer.py` — SQL/JS/XSS/shell filter (20 tests)
- `test_embedder.py` — embeddings (3 tests)
- `test_api.py` — API integration (34 tests)
- `test_dashboard.py` — read-only explorers (8 tests)
- `test_advisory_lock.py` — protection against double consolidation (3 tests)
- `test_pgvector.py` — pgvector integration (6 tests)
- `test_demo.py` — streaming `/demo/run` (2 tests)

## Security

- **Authentication**: per-agent `X-API-Key` (or `Authorization: Bearer`) via `AGENT_API_KEYS` in `.env`
- **Rate limiting**: 100 events/sec per agent (Redis INCR + TTL)
- **Sanitizer**: blocks SQL injection, JS, XSS and shell commands before anything is written to L1
- **JSON depth limit**: ≤10 levels, ≤500 keys, ≤256 KB payload
- **L5 audit log**: every action is logged (agent, time, target, success/error)
- **L4 snapshots**: SHA-256 integrity check on restore

For production:
- TLS via nginx: config in [nginx/](nginx/) + [docker-compose.prod.yml](docker-compose.prod.yml)
- Postgres backups: [scripts/backup_postgres.sh](scripts/backup_postgres.sh)
- More: [DEPLOY.md](DEPLOY.md) (Russian)

Threat model and reporting: [SECURITY.md](SECURITY.md).

## Hot-reloading embeddings

When you change the embedding model in `app/services/embedder.py`:

```bash
# Drops stale vectors from Redis and reindexes every domain
curl -X POST http://localhost:9001/memory/reindex \
  -H "X-API-Key: key-design-001"

# Cold start after a Redis restart (no LLM calls)
curl -X POST http://localhost:9001/memory/restore-redis \
  -H "X-API-Key: key-design-001"
```

## Performance

On an ordinary laptop (M2 / 16 GB / no GPU):
- POST /events: **~5 ms**
- KNN query (RediSearch, up to 1000 entries): **~3–5 ms**
- KNN query (pgvector HNSW, up to 1000 entries): **~5–15 ms**
- Daily consolidation (DeepSeek): **~10 s per domain**
- Weekly consolidation: **~20–30 s per domain**
- fastembed on CPU: **~50–100 ms per embed**

## Roadmap

| Version | Status | What |
|---|---|---|
| v0.2.0 | ✅ | MVP: 5 layers, 13 endpoints, 69 tests |
| v0.2.5 | ✅ | UX: landing page, dashboard, sandbox, shared design |
| v0.3.0 | ✅ | Reliability: pgvector, advisory lock, hot reload |
| v0.4.0 | ✅ | Reach: MCP server, Apple Glass UI, pgvector HNSW |
| v0.5.0 | ✅ | Production: Alembic, TLS, CI, backups, smoke + rollback |
| v0.6.0 | ✅ | Multi-tenant SaaS: isolation, billing, rooms, media, wake channels |
| v1.0.0 | ⏳ | Scale: Celery, sharding, Ollama + GPU, public launch |
| v2.0.0 | ⏳ | Advanced: knowledge graph, temporal queries, active learning |

Full plan: [roadmap.md](roadmap.md) (Russian).

## Project layout

```
cognitive-core/
├── app/                       # FastAPI application
│   ├── api/                   # Routers: events, operative, memory, tools, dashboard, demo, mcp_protocol, ...
│   ├── db/                    # Adapters: postgres (asyncpg), redis, s3 (minio)
│   ├── models/                # Pydantic schemas
│   ├── security/              # auth, sanitizer, validator, audit
│   ├── services/              # Business logic: ingestor, analyzer, consolidator, curator,
│   │                          #    embedder, llm_client, operative, tools, prompts, metrics
│   ├── main.py                # FastAPI + lifespan + middleware
│   ├── config.py              # pydantic-settings from .env
│   └── worker.py              # Background scheduler (daily/weekly/monthly)
├── sandbox/                   # Web UI: home.html, dashboard.html, index.html, shared.css, tour.js
├── cognitive-client/          # Python SDK (sync + async)
├── tests/                     # pytest suite
├── scripts/                   # seed_demo.py, stress_test.py, backups, deploy helpers
├── nginx/                     # nginx.conf for production TLS
├── docker-compose.yml         # Local development
├── docker-compose.prod.yml    # Production (nginx + TLS)
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md                  # ← you are here
├── README.ru.md               # Russian README
├── DEMO.md                    # Step-by-step demo walkthrough
├── DEPLOY.md                  # Production deployment runbook
├── roadmap.md                 # Development plan
└── CLAUDE.md                  # Notes for AI assistants
```

## For customers in Russia

- Billing via **YooKassa** (`YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY`) alongside Stripe; Pro tier is 490 ₽/month.
- **152-FZ** personal-data compliance pack: DPA template and FSTEC order 21 (UZ-3 level) checklist, see [docs/compliance-152fz.md](docs/compliance-152fz.md) and [docs/dpa-template-152fz.md](docs/dpa-template-152fz.md).
- Russia-resident LLM adapters (GigaChat, YandexGPT) for vision analysis without exporting data.
- Legacy Cyrillic domain alias: https://mcp.ии-память.рф.

## Contributing

PRs are welcome. Before opening one:
1. `docker exec cognitive_api python -m pytest tests/` must be green
2. New code is covered by tests
3. API changes → update `DEMO.md`
4. Architecture changes → update `roadmap.md`

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [DeepSeek](https://www.deepseek.com/) — primary LLM
- [pgvector](https://github.com/pgvector/pgvector) — vector search in Postgres
- [Redis Stack](https://redis.io/docs/stack/) — KNN with TAG filters
- [fastembed](https://github.com/qdrant/fastembed) — lightweight embeddings without a GPU
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server in Python
