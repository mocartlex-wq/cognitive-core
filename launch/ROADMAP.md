# Roadmap

> Updated 2026-05-10. Living doc — PRs welcome.

## Where we are (v0.5.0 alpha)

- ✅ 5-layer memory (L1–L5) with deduplication, automated promotion, decay, snapshots, audit
- ✅ Cross-platform Rooms with REST + per-room API key + long-poll + B+D fallback
- ✅ MCP server for Claude Code (10 `cognitive_room_*` tools)
- ✅ One-line install (`curl | bash`), 8-container Docker stack, ~850 MB idle
- ✅ E2E smoke test, OpenAPI 3.1 spec, 11 docs
- ✅ AI onboarding assistant on the Rooms web UI (DeepSeek-backed, `/ui/assistant`)
- ✅ Public release: MIT license, defensive publication white paper, CLA

## Calendar

### Next 7 days — public launch

- [ ] Repo merged + made public ✅ (done 2026-05-10)
- [ ] PyPI: reserve `cognitive-core-mcp`, publish via `publish-pypi.yml` workflow on `git tag v0.5.0`
- [ ] GHCR: publish `cognitive-core-api` + `cognitive-core-extras` images via `publish-image.yml` on tag
- [ ] Trademark filing for "Cognitive Core" in Роспатент (~30K ₽)
- [ ] Pre-sell to existing ai-crm-deploy clients (5 emails) → 1–3 anchor customers
- [ ] Demo instance public access (subdomain + Let's Encrypt cert)
- [ ] 5 PNG screenshots + hero GIF for README
- [ ] Record 5-min screencast per `screencast/SCRIPT.md`
- [ ] Submit on Hacker News (Tue 09:00 EST), draft `posts/HN_v2.md`

### Month 1 — react to launch traffic

- [ ] Triage every issue within 24 h
- [ ] Daily HN/Reddit thread monitoring (PostHog free tier on the demo)
- [ ] Stagger posts: HN → Habr → r/selfhosted → r/LocalLLaMA → r/ClaudeAI → Product Hunt
- [ ] Fix the top 3 bugs reported (whatever they are)
- [ ] Onboard ai-crm-deploy clients to managed Cognitive Core ($50–200/mo each)
- [ ] Discord server set up + invite link in README
- [ ] Defensive publication submitted to arXiv (cs.AI / cs.DC)

### Month 2–3 — first paying customers

- [ ] Set up payments: ЮKassa (RU) + crypto USDT (CIS) — international deferred
- [ ] Landing page at cognitive-core.dev with `pricing.html` (Hobby $15 / Pro $50 / Enterprise $200)
- [ ] Hire junior support engineer (remote RU, ~60K ₽/mo) — only if MRR ≥ $300
- [ ] First Selectel VPS-4 for production-only (separate from demo) — ~3500 ₽/mo
- [ ] `docs/COMMERCIAL.md` with sponsorship + commercial-use ask
- [ ] First 1–3 case studies blogged on Habr / dev.to

### Month 4–6 — scale + MRR target

- [ ] Selectel VPS-8 for production + VPS-2 for NATS/Redis HA → ~9500 ₽/mo
- [ ] **Premium edition** as separate repo `cognitive-core-enterprise` under BSL:
  - SSO / SAML
  - RBAC (roles, audit-only keys)
  - Multi-tenancy (one server, many orgs)
  - HA / replication (multi-server scale-out)
- [ ] First Premium customer — anchor testimonial
- [ ] KZ ИП setup for international payments (Wise Business)
- [ ] Target: $1500 MRR

### Month 7–12 — sustainable lifestyle business

- [ ] Dedicated Timeweb E5 64 GB → ~18000 ₽/mo (postgres on dedicated)
- [ ] Hire mid dev part-time (~100K ₽/mo) — if MRR ≥ $4000
- [ ] Productize 3 industry templates (Bitrix24, amoCRM, 1C integrations)
- [ ] Translate README + docs to English (currently mixed)
- [ ] Conference talk submission (ChatBot Conference, AI Engineer Summit)
- [ ] Target: $10K MRR / 1000+ stars / 100+ Discord members

### Year 2 (2027)

- [ ] Launch **Cognitive Core Cloud** — fully managed SaaS at cogcore.cloud
- [ ] Hosted offering with one-click "Connect Claude Code"
- [ ] Marketplace for community-built MCP integrations (CRM connectors, observability, …)
- [ ] At least one $5K+/mo enterprise contract
- [ ] Conditional: hire full-time #2 (DevRel or sales)

### Year 3 (2028) — pivot point

- [ ] Cognitive Core Cloud MRR overtakes ai-crm-deploy revenue
- [ ] Open-source community stable: 2000+ stars, 5+ regular contributors
- [ ] Premium edition revenue ≥ 50% of total
- [ ] Decision: stay solo / fundraise / acquire smaller competitor

### Year 4 (2029)

- [ ] Sell ai-crm-deploy at 3–5× annual EBITDA (~5–7 M ₽ exit)
- [ ] All-in on Cognitive Core
- [ ] Provisional USPTO patent on the proxy-override flow if heading to fundraise/exit

### Year 5 (2030)

- [ ] Steady state: $500K – $1M annual revenue OR
- [ ] Strategic exit to enterprise buyer for $500K – $1M

---

## Technical roadmap (orthogonal to business calendar)

### v0.5.x — current (alpha hardening)

- [ ] Fix bugs reported in launch week
- [ ] Replace `subprocess + psql` fallback in rooms.py (legacy code path, dormant but ugly)
- [ ] Add demo data seed (`make demo-seed`) — pre-populated room makes the screencast easier to film
- [ ] Documentation: troubleshooting page, FAQ
- [ ] CI: run smoke on push to PR + nightly cron

### v0.6.0 — Knowledge Graph + GPU

- [ ] Knowledge Graph queries: `cognitive_kg_search`, `cognitive_kg_path` MCP tools
- [ ] GPU-accelerated embeddings (Dockerfile.gpu shipped, needs prod test)
- [ ] Vector recall in `cognitive_recall` (currently text-only)
- [ ] Dashboard: Knowledge Graph visualization (D3 or Cytoscape)
- [ ] `cognitive_room_*` tools added to MCP wrapper (currently REST only)

### v0.7.0 — Multi-tenancy + RBAC (Premium hooks)

- [ ] Tenant table + scoped queries everywhere
- [ ] Role-based access control: admin / writer / reader per agent
- [ ] Audit-only API keys (read endpoints + L5 audit only)
- [ ] Per-tenant data isolation in postgres + minio
- [ ] First multi-tenant deploy in production

### v0.8.0 — HA + scale-out

- [ ] Postgres logical replication (read replicas)
- [ ] NATS JetStream cluster (3-node minimum)
- [ ] Redis Sentinel for failover
- [ ] Stateless API (currently has in-process state)
- [ ] Load-test playbook + reproducible benchmarks

### v0.9.0 — Cognitive Core Cloud (managed SaaS)

- [ ] Self-service signup at cogcore.cloud
- [ ] Per-tenant resource limits + billing integration
- [ ] One-click Claude Code MCP connection wizard
- [ ] Per-region deployment (RU + EU at minimum)
- [ ] SLA: 99.9% uptime, < 200 ms median latency

### v1.0.0 — Stable

- [ ] No breaking schema changes for 12 months
- [ ] At least 3 production deployments outside the maintainer
- [ ] Bench published in 1+ external paper or blog post
- [ ] Maintainer pool of ≥ 3 active committers
- [ ] Trademark registered in RU + USPTO

---

## Non-goals (intentionally NOT building)

- ❌ A new agent framework (LangChain, AutoGen, CrewAI all already exist)
- ❌ A new LLM (we wrap existing ones)
- ❌ End-user chat UI (we provide the substrate; consumers build their own UI)
- ❌ Speech / voice (out of scope; integrate via your own pipeline)
- ❌ Mobile native apps (web UI works on phones)
- ❌ Video / image memory (text + structured data only for v1.x)
- ❌ Chains / agent reasoning loops (you bring those; we give them memory + comms)

---

## How to influence the roadmap

- **Open an issue** with the `feature` label — concrete use-cases are gold
- **Open a discussion** for vague directions
- **Open a PR** if you want to land a feature yourself — pre-discuss for anything > 200 lines
- **Sponsor** an item — sponsors get to bump priority on items already on the roadmap (within reason)

This document is updated at every minor release.
