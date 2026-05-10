# Cognitive Core — Public Quickstart

> 5-layer cognitive memory + cross-platform agent rooms, in one Docker compose.
> Self-hosted, no vendor lock-in. Works with Claude Code, ChatGPT, Gemini, or
> any HTTP-speaking agent.

## What you get

- **Persistent memory** — L1 events → L2 daily buffers → L3 knowledge → L4 snapshots → L5 audit
- **Cross-platform rooms** — virtual collab spaces; agents join via REST + room key
- **Wake-on-message** — long-poll `/ask` for real-time, async fallback for offline agents
- **DeepSeek-powered curator** — auto-summarize, extract knowledge, score relevance
- **MCP server** — drop-in Claude Code integration (see `docs/MCP.md`)

## 60-second install

```bash
# Requires: Docker 20.10+, docker compose plugin, make, openssl
git clone https://github.com/mocartlex-wq/cognitive-core ~/cognitive-core
cd ~/cognitive-core
make init     # generates .env with random secrets
# edit .env to add DEEPSEEK_API_KEY
make up
make smoke
```

Or one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/mocartlex-wq/cognitive-core/main/quickstart.sh | bash
```

Open:
- **API docs**: http://localhost:9001/docs
- **Rooms UI**: http://localhost:9098/ui
- **MinIO console**: http://localhost:9002

## Common operations

| Task | Command |
|------|---------|
| Start | `make up` |
| Stop (keep data) | `make down` |
| Tail logs | `make logs` |
| Container status | `make ps` |
| Smoke test | `make smoke` |
| Backup database | `make backup` |
| Restore | `make restore FILE=./backups/xxx.sql.gz` |
| Wipe everything | `make clean` |
| With nginx (TLS) | `make up-edge` |
| Update images | `make pull && make up` |

## Architecture (mini)

```
                    ┌────────────────────────────────┐
   Claude Code ─┐   │   nginx (optional, edge)        │
   ChatGPT     ─┼──▶│         │                       │
   any agent   ─┘   │         ▼                       │
                    │   ┌─────────┐    ┌──────────┐  │
                    │   │  api    │◀──▶│ postgres │  │
                    │   │ (FastAPI)    │ pgvector │  │
                    │   └────┬────┘    └──────────┘  │
                    │        │                        │
                    │   ┌────▼────┐   ┌──────────┐   │
                    │   │ rooms   │   │  redis   │   │
                    │   │ /ask /post│  │ AOF      │   │
                    │   └────┬────┘   └──────────┘   │
                    │        │ PG NOTIFY              │
                    │   ┌────▼────────┐  ┌────────┐  │
                    │   │ pg-to-nats  │─▶│  NATS  │  │
                    │   └─────────────┘  │ JS+WS  │  │
                    │                    └────┬───┘  │
                    │   ┌─────────┐           │      │
                    │   │   mcp   │           │      │
                    │   │ (SSE)   │ ◀── WebSocket    │
                    │   └─────────┘                  │
                    │   ┌─────────┐                  │
                    │   │  minio  │  L4 snapshots   │
                    │   └─────────┘                  │
                    └────────────────────────────────┘
```

## Why use this

| | Cognitive Core | LangChain | AutoGen | OpenAI Assistants |
|---|---|---|---|---|
| Cross-platform agents (Claude + GPT + …) | ✅ via REST rooms | ❌ Python only | ❌ Python only | ❌ vendor |
| Persistent multi-layer memory | ✅ L1–L5 | partial (chat history) | ❌ | partial |
| Self-host, no vendor lock-in | ✅ MIT | ✅ | ✅ | ❌ |
| Wake-on-message + offline fallback | ✅ NATS + DeepSeek proxy | ❌ | ❌ | ❌ |
| Cost (one-room demo) | $0 (own DeepSeek) | $0 | $0 | $0.03 / msg |

## Docs

- [`docs/MCP.md`](docs/MCP.md) — Claude Code / Cherry Studio integration
- [`docs/HARDENING.md`](docs/HARDENING.md) — TLS, auth, rate-limit, backups
- [`docs/ROOMS.md`](docs/ROOMS.md) — full Rooms API reference
- [`docs/MEMORY.md`](docs/MEMORY.md) — what gets stored where, GDPR/data-retention notes
- [`docs/UPGRADING.md`](docs/UPGRADING.md) — version bumps, schema migrations
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, repo layout
- [`SECURITY.md`](SECURITY.md) — disclosure policy

## License

MIT — see [`LICENSE`](LICENSE).

## Status

**Alpha** — expect bugs. Feedback welcome via [GitHub Issues](https://github.com/mocartlex-wq/cognitive-core/issues).
File a "this docs page confused me" issue too — those are the most useful right now.

🤖 [Built with Claude Code](https://claude.com/claude-code)
