# Cognitive Core — Changelog

> The three most recent entries are in English. Older entries below the marker are kept in Russian as written at the time.

## Unreleased

### Changed — "Owner ↔ fleet link" plan v2, 2026-09-05/06 (#240–#246 + fork/designer branches)

- **Deploy (#240)**: rollback also on a failed build or migration, not only on a
  failed smoke test; `conditional_reload.sh` runs `alembic upgrade head` after
  the api comes up (`COGNITIVE_MIGRATE=0` on rollback — the schema is never
  downgraded).
- **Owner gate (#241)**: a database key without `owner_user_id` → 403 (it used
  to mean admin); `POST /agents/register` requires an owner; `_can_deploy(None)`
  → False; alembic 0024 (orphans revoked); `X-Request-Id` in logs and
  responses; rate limit on `tools/call`. The cookie path of
  `resolve_owner_user_id` fixed (it imported a non-existent function).
- **MCP Streamable HTTP (#241)**: `POST /mcp` — inline response, stateless,
  batch; legacy SSE replies inline on `PUBLISH` with no subscribers instead of
  a 202 "into the void". Client:
  `claude mcp add --transport http cognitive-core https://mcp.me-ai.ru/mcp`.
- **Daemon (#242, #244)**: wake-up via PG NOTIFY (`room_event`, `agent_inbox`);
  one tool loop for every brain — `managed` (Anthropic, default
  `claude-sonnet-5`), `custom_llm` (OpenAI-compatible), `deepseek`; room context
  in the prompt; the `cognitive_remember` tool; `claude_routine` timeout with a
  fallback reply.
- **Single UI (#243, #246, design/*)**: `/ui/room` = webchat; "To" chips,
  typing indicator in the status line, paste from clipboard, `?room=`; two
  panels on desktop, markdown in bubbles, "Show in full", a "deputy" chip; the
  `/static/chat-prototype.html` showcase embeds the live chat (`/chat?embed=1`,
  nginx `location = /chat` → `X-Frame-Options SAMEORIGIN`).
- **Memory**: room attachments/documents are no longer deleted after a day
  (#245); snapshot restore sets `owner_user_id`; `cognitive_recall` without
  `domain` is reachable over MCP (review/recall-domain-optional);
  `l3_coverage_pct` in `/health.deep`; platform memory-hygiene rules
  (alembic 0025).
- **Claude Code channel** `tools/cc-channel/` (Channels preview): rooms push
  messages into a live session; reply via `reply`.
- Web Push enabled in production (VAPID in override.yml).
- **Daemon hardening after the 06.09 review**: an exception on one message no
  longer kills the pass or loses `seen_ids`; the LISTEN thread probes the
  connection every 60 s (`SELECT 1`, keepalives, password escaped); the
  Anthropic loop keeps `tools` in every request (limited via
  `tool_choice=none`), `max_tokens` floor 4096; a stale `routine_pending` is
  cleared without a fallback reply.
- **Room presence** (fork): `online`/`last_seen` of participants in
  `/user/rooms/{id}/detail`, a dot on the "To" chip, "N online" in the status
  line. The default heartbeat (`last_heartbeat_at DEFAULT NOW()` =
  `created_at`) no longer counts as presence — previously six participants
  showed the same room re-creation date.
- **Webchat composer** (fork): attachment preview strip before sending
  (thumbnail, name, size, ✕ removes it and the `(media:id)` marker), size cap
  before base64 (200 MB / 50 MB audio, same as the server), `accept` on the
  file picker.
- **Webchat desktop pass #2** (designer + fork): the room list no longer
  slides off-screen (both panels get `grid-row:1`); sender name 13px/600 and a
  `<time>` on every message; long reports collapse by actual height (~12
  lines); feed and composer in one 900px column, from 1600px the app is 1680 /
  panel 360; inputs are not black, the light theme paints the page via
  `html:has(.app.light)`; "To" chip tooltip reads "seen N ago" / "never
  connected". Node-script tests use `encoding="utf-8"` (on Windows cp1251
  turned Cyrillic into garbage).
- **L2→L3 consolidation (Phase 5b)**: 30+ domains with a single L2 buffer
  never produced L3 — the repetition threshold was computed over a 7-day
  window. Weekly now uses a catch-up window `WEEKLY_BACKFILL_DAYS=90` (only
  when there is not enough material) and `run_weekly_cycle` selects domains by
  the same window; the `min_l2_repetitions_for_l3` threshold is fixed in code
  (one buffer → `insufficient_repetition` without an LLM call), the window
  widens per owner; curator failures are visible in logs and as
  `curator_errors` in the weekly result (secrets stripped). A one-off run on
  06.09 raised `l3_coverage_pct` 27.5 → 94.1, but some entries come from a
  single buffer (see the owner's memory).
- **Domain hints (Phase 5b)**: `tools/list` fills the description of
  `cognitive_remember.domain` with the owner's established domains (active L3
  or ≥ 2 L2), cached for 5 min; the base text no longer suggests a lone
  example.


### Added — Wake channels: provider-agnostic agent wake-up channels (#191–#201)

- The `scripts/cognitive-agent-runtime.py` daemon generalised: a 24/7 stand-in
  for ANY opted-in agent (#191), per-agent UI toggle (#192).
- Per-agent `wake_channel` dispatcher (#193): `deepseek` / `claude_routine` /
  `managed` (Claude API directly, #195) / `custom_llm` (any OpenAI-compatible
  endpoint, #196) / `webhook` (one central waker for all agents, #199).
- Channel secrets encrypted with Fernet (`COGCORE_CONFIG_KEY`) (#196); channel
  selector in the UI on the agent card (#194); test-connection button + quick
  refresh (#198).
- Retries of transient DNS/URL errors in the daemon (#197); agents reply in
  their own voice — the auto-reply marker removed (#201).
- Docs: Agent Loop — the standard for plugging any agent into rooms (#200).

### Added — Rooms: conductor routing and agent participation

- Unaddressed owner messages in a room with a conductor go to the conductor
  (#190).
- Per-room agent auto-reply (`room_participants.auto_respond`, migration 0017):
  the daemon wakes an agent on a direct @mention in a specific room and posts
  the reply through its `wake_channel`, without a full 24/7 stand-in (#214).
  Copies to the conductor and unaddressed messages do not wake anyone. API
  `POST /user/rooms/{room_id}/participants/{agent_id}/auto-respond` + state in
  `GET /user/rooms/{id}/detail` + an "Auto-reply" toggle in `/ui/room`.
- Owner-side "add my agent to the room": endpoint + picker in room.html (#226).
- Room composer: Enter sends, Shift+Enter inserts a newline (#207).

### Added — Shared Brain: several devices = one agent (#205, #206)

- `agent_states.brain_id` + a `brains` table: devices of one owner share
  state/history/memory as a single agent. UI button to bind a device to a brain.

### Added — Skills in recall (#209, #210)

- `cognitive_resume` automatically mixes in the top-2 relevant skills from L3
  `domain='skills'`.

### Fixed — Multi-worker stability (#202–#204, #208)

- The MCP SSE transport is multi-worker-safe: sessions via Redis pub/sub
  (`mcp:sse:{sid}`) — the client no longer drops when a POST lands on another
  of the 4 uvicorn workers (#204).
- `pg_advisory_xact_lock` serialises `init_db` DDL between workers — previously
  2–3 of 4 crashed on startup (#203).
- Empty env strings are treated as absent (mcp_protocol/outbox) (#202).
- Eager warm-up of the embedding model on startup: the first `cognitive_recall`
  after a restart no longer times out on a cold load (#208).

### Fixed — Deploy and cron (#211–#213, #224)

- The fastembed cache moved to a persistent volume in prod.yml — recall no
  longer degrades to the hash fallback after a container recreate (#211).
- Auto-deploy unblocked: the foreign `nginx/conf.d/ai-crm.conf` excluded from
  the dirty-tree guard (#212).
- subscription-cron honestly reads `DATABASE_URL` from env (#213); fix for the
  module-level `import os` (NameError in main()) (#224).

### Fixed — UI round (#215–#223, #225)

- Agent card: channel-select width capped, name/meta take the full row
  (#215, #216); agent/claim dialogs show the real error instead of
  `[object Object]`, spaces in agent_id are blocked (#222); a taken agent_id →
  a suggestion of a free one (base-N) instead of a dead-end 409 (#223).
- Neural background: axon visibility/softness + cache-bust of glass.css and
  theme.js (#217–#220); the connect-wizard stepper isolated from the `.step`
  leak out of shared.css (#221); room invite — one generate+copy button (#225).

### Security — Daemon: boundaries of a foreign domain (#227, #228)

- `/opt/ai-crm` removed from the agent daemon's WRITE whitelist (`git_pull` +
  `docker_restart`): ad-hoc ai-crm deploys took production down three times —
  now only the regular `deploy/update.sh` (#227); the mention also removed from
  the tool description (#228).

### Added — Audit round + mobile UI + deploy MCP (#229)

- Results of the audit round, mobile UI fixes and MCP deploy tools.

## v0.6.0 (2026-05-28) — Multi-tenant platform release

A big arc after `v0.5.0-rc1`: the single-tenant pipeline grew into a
multi-tenant SaaS product. Phase 4 (tenant isolation) → Phase 5 (Gitea +
billing + welcome flow) → Phase 6 (Operating Rules) + the media pipeline,
rooms, video generation, Russian enterprise compliance and public onboarding.
Production instance [https://mcp.me-ai.ru](https://mcp.me-ai.ru), 29 MCP tools.

### Added — Phase 4: Multi-tenancy

- `owner_user_id` isolation on every layer L1–L4: one owner = many agents,
  shared memory via `WHERE owner_user_id = …` (#23).
- Per-owner prefix in MinIO (`l4/{owner_user_id}/…`) for snapshots and media.
- Per-tenant quotas + `GET /user/usage` (events/day, media volume, number of
  agents).
- Backfill of `agent_keys.owner_user_id` + INSERT path for new keys (#34).

### Added — Phase 5: Workspace platform (Gitea + billing + welcome)

- Self-hosted git at `git.me-ai.ru` (Gitea) with a combined TLS certificate for
  `me-ai.ru` + the punycode domain (#25, #39).
- Pricing page `/ui/pricing` + welcome flow `/ui/welcome` + admin tenants
  (#25, #26).
- Billing skeleton for Stripe + YooKassa: `/api/billing/checkout/{tier}`,
  pricing.html → checkout wiring, upgrade hints in the profile (#95).
- Subscription expiry cron + README modernisation (#96).

### Added — Phase 6: Operating Rules (self-improving)

- Tables `agent_rules` + `rule_proposals` + `rule_votes`, seed of 4 platform
  core rules (#79), later a 5th `rule-media-via-pipeline` (migration 0012).
- Per-owner Operating Rules injected into the orchestrator's LLM prompt (#82).
- CRUD `/user/rules` + proposals + `/admin/rule-proposals` (#80), a rules
  section in `/ui/profile` + an admin proposals page (#81).
- Weekly rule analyzer + systemd timer — DeepSeek reviews rule proposals every
  week (#83).

### Added — Accounts & Auth

- OTP sign-in (6-digit code by email, 15-min TTL, single-use, rate-limited)
  instead of magic links, `/ui/login` page.
- Sessions with `device_info` parsing, "My devices" management, account
  editing (name/avatar), a collapsible "Account" card in the profile.
- Actionable 401 message for API/CLI clients (#97).
- Email backend for OTP and digests.

### Added — Media pipeline

- Media analyzer: video → frames + Whisper transcription, plus images.
- GPU Whisper on CUDA 12.2 + cuDNN8 + ffmpeg (`Dockerfile.gpu`), env-driven
  device/compute_type (#64, #70–#74).
- Qwen-VL vision stage → `mechanics_summary` in the L1 payload (#51), with a
  DeepSeek text-only fallback when Qwen is unavailable (#52).
- Per-tenant external vision keys (opt-in): Qwen / MiniMax / GigaChat /
  YandexGPT / Claude / OpenAI / Gemini (#53).
- MCP tool `cognitive_media_upload` (the 25th tool) + universal base64 input
  `/api/media/upload_b64` (predecessor of #108).
- Resumable upload (`cognitive_media_upload_init/_finalize`) — curl PUT around
  the LLM base64 context cap (#108) + hourly cleanup of orphan files (#109).
- Public `GET /api/media/info/{id}` + a "My media" grid card in the profile.

### Added — Rooms & multi-agent

- 7 `room_*` MCP tools + the `_call_rooms` helper (#90), backend
  `cognitive-rooms.service` on :9098.
- Rooms CRUD in `/ui/profile` (create/rename/delete, modelled on machines)
  (#102).
- Direct messages between agents, `my_team`, auto-DM, presence indicator
  (green dot) in "My assistants".

### Added — AI video generation

- Kling / Sora video generation skeleton: the `cognitive_video_generate` MCP
  wrapper + 17 tests (#93).

### Added — Orchestrator

- Server-side AI dispatcher (daemon) for multi-agent scenarios (#42).
- Multi-step planning chains (#49) + actions `room_join` / `room_read` /
  `cognitive_recall` / `analyze_media` (#47).
- Direct SQL registration of agents (bypassing the broken `/agents/register`)
  (#44).

### Added — Onboarding & agent discovery

- Unified Agent Onboarding v1: the `/ui/connect` wizard + claim-token flow (the
  agent connects itself), Redis backend for claim tokens (survives an API
  restart).
- Peek endpoint `claim/peek` + manifest enrichment (`peers[]`) + a clean claim
  prompt (Phase O, #101).
- Claim-token idempotency + one-liner self-hosted install
  `curl /static/install-self-hosted.sh | sudo bash` (#106).
- 🟢 canary in the prompt + UI pre-check of a pending agent (#110).
- Support for 6 platforms (Claude Code, Cursor, ChatGPT, LangChain, Telegram
  bot, self-hosted VPS); browser-stable machine fingerprint via JS (#78).

### Added — Russian enterprise

- 152-FZ compliance pack: DPA template + FSTEC-21 UZ-3, docs
  `compliance-152fz.md` (#84).
- GigaChat / YandexGPT adapters as vision providers (#53).

### Added — Docs

- Quickstarts: rooms, video generation, billing, self-hosted VPS,
  external providers, agent discovery, memory scope.
- `docs/index.md` TOC, a migration guide from competitors + case studies.
- Launch materials (Habr / VC.ru / Twitter).

### Changed

- `cognitive_recall` / `analyze_media`: 60 s timeout for the slow semantic KNN
  (#48).
- nginx: modernised `http2` directive (1.25+ syntax), joined the `ai-crm_aicrm`
  network, lazy DNS resolver, `me-ai.ru` split into a redirect-only block for
  Yandex Browser (#40, #61, #62, #65–#67).
- Postgres tuning for a 32 GB host (4 GB shared_buffers, 32 MB work_mem) (#68);
  API memory limit 3G → 8G for 4 workers + Whisper pre-cache (#69).
- agent_id: Cyrillic and any Unicode letters allowed (#32, #87).
- Sanitizer: the `SQL_PATTERN` check removed (queries are parameterised; the
  em dash is no longer blocked).
- UI: unified top nav (Home · AI chat · Rooms · API), Apple Liquid Glass
  design, view transitions, head bootstrap against flicker.

### Fixed

- claim-token: a short `agent_id` (`'8'`) broke `_create_agent_core` → 5xx;
  ON CONFLICT bug on claim (#85).
- Migration 0007: `accounts.id` → `accounts.user_id` (#31); `/user/usage`
  AttributeError `user.id` → `user.user_id` (#29).
- CI fully green + 4 latent F821 bugs in `connect.py` (#86).
- Whisper compute_type=int8 for Pascal GPUs (the GTX 1050 Ti supports only
  int8 on GPU) (#76, #77).
- vision_analyzer: handling of empty env strings from docker-compose
  `${VAR:-}` (#63).
- Replication: TimeoutError flood suppressed when `nats-py` is missing (#56).
- MCP/SSE: session-based message routing + `event:ping` keepalive (fix for the
  Claude Code SDK 32 s timeout).
- Rooms: invite template `sender` → `from_agent` (#103).
- Auto-deploy self-heal for a dirty git tree + rooms diagnostics (#88).

### Security

- Billing security hardening (#89).
- Media cleanup v2: HARD DELETE (files + L1 row) per the owner's decision (#30).
- Sanitised state_data, quota enforcement as a defensive mechanism.

## Unreleased — v1.0 roadmap (in progress, 2026-05-27 → 28)

Overnight sprint towards v1.0: ~36 PRs, partly by parallel agents. M1 and M2
complete, M3–M6 partial. Plan: `~/.claude/plans/iridescent-enchanting-rabbit.md`.
Known blockers: PAT scope for workflow, Docker IPv6 (see the fix below).

### Added — M1: Test foundation ✅

- Session-cookie fixture infrastructure (#115) + admin fixture, the full claim
  flow with a session fixture (#116).
- Unit tests: media peek + resumable upload (#113), CircuitBreaker (#114),
  vault encrypt/decrypt in `user_settings` (#117), quota_enforcer with a mock
  pool (#118), email_client + email_templates (#119), pure functions of
  `prompts.py` (#134).

### Added — M2: Observability ✅

- SRE incident runbook (#120).
- `scripts/restore-from-l4.sh` + a design doc for restoring from L4 (#121).
- Grafana dashboard + Prometheus alerts (#122).
- SLO-tracking endpoint `/admin/slo` (#123).

### Added — M3: UI i18n (partial)

- Internationalisation infrastructure for the RU/EN UI: `sandbox/i18n.js` +
  ru/en locales (#124).

### Added — M4: Features (partial)

- `me-ai-edge` container stub + architecture doc (#127).
- Admin audit-log read endpoint `/admin/audit` (#131).
- Tenant webhook notifications with anti-SSRF + HMAC signature (#133).

### Changed — Ops

- Docs `*.md` are served over HTTP (`/docs/X.md`) — broken links fixed; the
  files are included in the Docker image (#129, #130).
- Weekly docker-prune cron + memory-scope documentation (#105).

### Fixed

- **Docker IPv6 build (permanent fix)**: buildkit resolved
  `registry-1.docker.io` over IPv6 → "network is unreachable" on a VPS in
  Russia → auto-deploy failed. Solution: automatic pre-pull of base images
  (`docker pull` with an IPv4 fallback) before every build, which primes the
  cache (#132).
- Machine-rename pencil moved to the group header + broadcast label (#100).
- `my_rooms` query: the non-existent `p.user_id` removed (#126).
- Restored the `agentDot` + `agentChip` helpers in `profile.html` (regression
  from PR #102) (#125).
- Claim-token idempotency (#106).

---

> **Older entries below are in Russian** (kept as originally written; not yet translated).

## Unreleased — v0.5.0-prod sprint (in progress)

Production-readiness sprint, согласован two-voice (Claude + DeepSeek). Превращает working pipeline в production-grade. См. `roadmap.md` раздел v0.5.0-prod.

### Added
- `scripts/auto-deploy.sh`: smoke-test (6 attempts × 5s) после conditional_reload + auto-rollback к предыдущему SHA если /health не возвращает healthy=true минимум 5/6 раз. Production остаётся на последней рабочей версии при любом сломанном push'е. Telegram alerts задним числом (sprint task 5).

## v0.5.0-rc1 (2026-05-04) — Pre-deploy ready

Полная готовность к переносу на сервер.

### Added — Production deployment
- `install-server.sh`: 9-шаговый installer для Ubuntu 22/24 (за ~10 минут от чистого сервера)
- `scripts/gen-secrets.sh`: генерация strong passwords + 3 agent API keys
- `scripts/setup-tls.sh`: self-signed cert или Let's Encrypt
- `scripts/cron-backup.sh`: автоматические бэкапы Postgres + MinIO каждые 6 часов с ротацией 14 дней
- `scripts/stress_test.py`: нагрузочный тест с правильной оценкой 429 как security feature
- `docker-compose.prod.yml`: resource limits (DeepSeek-validated для 4cpu/16GB), logging rotation, backup-сервис, nginx с TLS
- `nginx/nginx.conf`: + `/mcp/sse` proxy для remote MCP-клиентов
- `mcp_server/server.py`: SSE transport (`--sse`) для удалённого подключения
- `DEPLOY-SERVER.md`: полный runbook (TLS, бэкапы, DR, security checklist)
- `.env.production.example`: шаблон без секретов

### Added — Per-agent state checkpoint (новый pillar)
- Tables: `agent_states` + `agent_state_history`
- API: `POST /agents/{id}/checkpoint`, `GET /agents/{id}/state`, `GET /agents/{id}/history`, `GET /agents`
- MCP tools: `cognitive_save_state`, `cognitive_continue`, `cognitive_my_history`
- Recovery: при срыве сессии агент в новом сеансе вызывает `cognitive_continue` → восстанавливает task + state_data + recent events
- Sanitize state_data (256KB limit, SQL/JS/XSS защита)
- Гибридный trigger: manual + auto + heartbeat + session_close + event_milestone
- 8 тестов agent_state

### Added — Operative grouped frame
- `POST /operative/query?grouped=true`: возвращает структурированный frame по разделам
  patterns / mistakes / rules / tools / all + counts
- MCP `cognitive_recall` теперь по умолчанию `grouped=True`
- 4 теста grouped behavior

### Added — Multi-client installer
- `installer.ps1` (Windows): авто-detect Claude Desktop / Cursor / Claude Code / Cherry Studio
- `installer.sh` (Linux/macOS): аналогичный
- Cherry Studio: clipboard-based (IndexedDB binary, нельзя писать напрямую)
- `CHERRY_STUDIO.md`: 3-click setup guide

### Added — Tools registry global view
- `GET /dashboard/tools-registry`: glob al агрегация tools across доменов с count + breadth + recency
- Новая вкладка «Инструменты» в дашборде
- 7 тестов

### Added — Apple Liquid Glass UI
- `sandbox/glass.css`: design system с floating orbs, frosted glass cards
- `sandbox/neurons.svg`: анимированная нейросеть с импульсами
- `sandbox/icons.svg`: 15 SF Symbols-style иконок
- Light/dark theme switcher с auto-detect
- Onboarding tour (shepherd.js)

### Fixed
- MCP via docker exec: `python -u` + `PYTHONUNBUFFERED=1` решает stdout buffering
- installer.ps1: убран `*>&1` — false NativeCommandError на docker progress
- save_daily.ps1: backticks через `[char]96` вместо escape
- L4 snapshot integrity check (SHA-256 + counts validation)
- pgvector 384-dim + HNSW для KNN
- Postgres advisory lock от двойной консолидации
- Hot-reload эмбеддингов через `cleanup_stale_vectors`

### Tests
- 114 → 122+ passing (+ 8 agent_state)
- Stress test: 200 events @ 10 concurrency = p95 ~80ms, 100 events/sec rate
- KNN: 50 queries = p95 ~750ms, 40 req/sec

### Deferred (post-deploy)
- Prometheus + Grafana monitoring (после реальных метрик с сервера)
- 10k events/min sustained test (на реальном сервере)
- GitHub publish + видео-демо

## v0.4.0 (2026-05-03) — Reach release

См. предыдущий коммит history. Initial production-grade release с pgvector, MCP server, Apple-glass UI, dogfooding инфраструктура.

## v0.2.0 — MVP

5-layer memory с DeepSeek + curator architecture.
