# Cognitive Core — Changelog

## Unreleased

### Added — Rooms: per-room авто-ответ агента (room auto-responder)

- Владелец может привязать агента к авто-ответам в КОНКРЕТНОЙ комнате
  (`room_participants.auto_respond`, миграция 0017). Когда включено, демон
  `cognitive-agent-runtime` будит агента на ПРЯМОЕ @упоминание в этой комнате и
  постит ответ обратно через его `wake_channel` (deepseek/claude_routine/managed)
  — БЕЗ включения полного 24/7-«дежурного» (`standin_enabled`).
- Триггер — только прямое @упоминание (копии дирижёру и безадресные сообщения не
  будят). Привязка строго per-room: включение в одной комнате не влияет на другие.
- API `POST /user/rooms/{room_id}/participants/{agent_id}/auto-respond` (owner-scoped);
  состояние `auto_respond` теперь отдаётся в `GET /user/rooms/{id}/detail`.
- UI-тумблер «Авто-ответ» у каждого участника в `/ui/room`.

## v0.6.0 (2026-05-28) — Multi-tenant platform release

Большой арк после `v0.5.0-rc1`: из single-tenant pipeline вырос multi-tenant
SaaS-продукт. Phase 4 (изоляция арендаторов) → Phase 5 (Gitea + биллинг +
welcome-флоу) → Phase 6 (Operating Rules) + media-пайплайн, комнаты, видео-генерация,
RU-enterprise compliance и публичный онбординг. Production-инстанс
[https://mcp.me-ai.ru](https://mcp.me-ai.ru), 29 MCP-инструментов.

### Added — Phase 4: Multi-tenancy

- `owner_user_id`-изоляция на всех слоях L1-L4: один владелец = много агентов,
  общая память по `WHERE owner_user_id = …` (#23).
- Per-owner префикс в MinIO (`l4/{owner_user_id}/…`) для snapshot'ов и медиа.
- Per-tenant quotas + `GET /user/usage` (события/день, медиа-объём, число агентов).
- Backfill `agent_keys.owner_user_id` + INSERT-путь для новых ключей (#34).

### Added — Phase 5: Workspace platform (Gitea + billing + welcome)

- Self-hosted git на `git.me-ai.ru` (Gitea) с комбинированным TLS-сертификатом
  для `me-ai.ru` + punycode-домена (#25, #39).
- Pricing-страница `/ui/pricing` + welcome-флоу `/ui/welcome` + admin tenants (#25, #26).
- Биллинг-скелет Stripe + ЮKassa: `/api/billing/checkout/{tier}`, привязка
  pricing.html → checkout, upgrade-подсказки в профиле (#95).
- Cron истечения подписок + модернизация README (#96).

### Added — Phase 6: Operating Rules (self-improving)

- Таблицы `agent_rules` + `rule_proposals` + `rule_votes`, сид 4 платформенных
  core-правил (#79), позднее +5-е `rule-media-via-pipeline` (миграция 0012).
- Инжект per-owner Operating Rules в LLM-prompt оркестратора (#82).
- CRUD `/user/rules` + предложения + `/admin/rule-proposals` (#80), UI-секция
  правил в `/ui/profile` + страница админ-предложений (#81).
- Weekly rule-analyzer + systemd-timer — DeepSeek еженедельно ревьюит
  предложения правил (#83).

### Added — Accounts & Auth

- OTP-логин (6-значный код на email, TTL 15 мин, single-use, rate-limit) вместо
  magic-link, страница `/ui/login`.
- Сессии с парсингом `device_info`, управление «Мои устройства», редактирование
  аккаунта (имя/аватар), коллапс-карточка «Аккаунт» в профиле.
- Actionable 401-сообщение для API/CLI-клиентов (#97).
- Email-backend для OTP и дайджестов.

### Added — Media pipeline

- Анализатор медиа: видео → кадры + транскрипция Whisper, плюс изображения.
- GPU-Whisper на CUDA 12.2 + cuDNN8 + ffmpeg (`Dockerfile.gpu`), env-driven
  device/compute_type (#64, #70-#74).
- Vision-стадия Qwen-VL → `mechanics_summary` в L1-payload (#51), с DeepSeek
  text-only fallback при недоступности Qwen (#52).
- Per-tenant external vision-ключи (opt-in): Qwen / MiniMax / GigaChat /
  YandexGPT / Claude / OpenAI / Gemini (#53).
- MCP-инструмент `cognitive_media_upload` (25-й tool) + universal base64-вход
  `/api/media/upload_b64` (#108 предшественник).
- Resumable upload (`cognitive_media_upload_init/_finalize`) — curl PUT в обход
  base64 context-cap LLM (#108) + hourly-очистка orphan-файлов (#109).
- Публичный `GET /api/media/info/{id}` + grid-карточка «Мои медиа» в профиле.

### Added — Rooms & multi-agent

- 7 MCP-инструментов `room_*` + helper `_call_rooms` (#90), backend
  `cognitive-rooms.service` на :9098.
- Rooms CRUD в `/ui/profile` (создание/переименование/удаление по образцу
  машин) (#102).
- Direct messages между агентами, `my_team`, авто-DM, presence-индикатор
  (зелёная точка) в «Мои помощники».

### Added — AI Video Generation

- Скелет генерации видео Kling / Sora: MCP-обёртка `cognitive_video_generate`
  + 17 тестов (#93).

### Added — Orchestrator

- Server-side AI-диспетчер (daemon) для multi-agent сценариев (#42).
- Multi-step planning chains (#49) + действия `room_join` / `room_read` /
  `cognitive_recall` / `analyze_media` (#47).
- Прямая SQL-регистрация агентов (обход сломанного `/agents/register`) (#44).

### Added — Onboarding & agent discovery

- Unified Agent Onboarding v1: визард `/ui/connect` + claim-token флоу (агент
  сам себя подключает), Redis-backend для claim-token (переживает рестарт API).
- Peek-endpoint `claim/peek` + обогащение manifest (`peers[]`) + чистый
  claim-prompt (Phase O, #101).
- Idempotency claim-token + one-liner self-hosted установка
  `curl /static/install-self-hosted.sh | sudo bash` (#106).
- 🟢-canary в prompt + UI-предпроверка pending-агента (#110).
- Поддержка 6 платформ (Claude Code, Cursor, ChatGPT, LangChain, Telegram-бот,
  self-hosted VPS); browser-stable machine fingerprint через JS (#78).

### Added — RU enterprise

- 152-ФЗ compliance pack: DPA-шаблон + ФСТЭК-21 УЗ-3, docs `compliance-152fz.md`
  (#84).
- Адаптеры GigaChat / YandexGPT как vision-провайдеры (#53).

### Added — Docs

- Quickstart'ы: rooms, video-generation, billing, self-hosted VPS,
  external-providers, agent-discovery, memory-scope.
- `docs/index.md` TOC, гайд миграции конкурентов + кейсы.
- Launch-материалы (Habr / VC.ru / Twitter).

### Changed

- `cognitive_recall` / `analyze_media`: таймаут 60s под медленный semantic KNN
  (#48).
- nginx: модернизация `http2`-директивы (синтаксис 1.25+), вступление в сеть
  `ai-crm_aicrm`, lazy DNS-resolver, split `me-ai.ru` в redirect-only блок для
  Yandex Browser (#40, #61, #62, #65-#67).
- Postgres tuning под 32 GB хост (4 GB shared_buffers, 32 MB work_mem) (#68);
  лимит памяти API 3G→8G под 4 воркера + pre-cache Whisper (#69).
- agent_id: разрешены кириллица и любые Unicode-буквы (#32, #87).
- Sanitizer: снят `SQL_PATTERN`-чек (запросы параметризованы, em-dash больше не
  блокируется).
- UI: унифицированный top-nav (Главная · AI-чат · Комнаты · API), Apple Liquid
  Glass дизайн, view-transitions, head-bootstrap против мерцания.

### Fixed

- claim-token: короткий `agent_id` (`'8'`) ломал `_create_agent_core` → 5xx;
  ON CONFLICT-баг при claim (#85).
- Миграция 0007: `accounts.id` → `accounts.user_id` (#31); `/user/usage`
  AttributeError `user.id` → `user.user_id` (#29).
- CI полностью зелёный + 4 латентных F821-бага в `connect.py` (#86).
- Whisper compute_type=int8 для Pascal-GPU (GTX 1050 Ti поддерживает только
  int8 на GPU) (#76, #77).
- vision_analyzer: обработка пустых env-строк из docker-compose `${VAR:-}` (#63).
- Replication: подавлен TimeoutError-флуд при отсутствии `nats-py` (#56).
- MCP/SSE: session-based routing сообщений + `event:ping` keepalive (фикс
  32s-таймаута Claude Code SDK).
- Rooms: invite-шаблон `sender` → `from_agent` (#103).
- Auto-deploy self-heal для dirty git-tree + диагностика rooms (#88).

### Security

- Billing security hardening (#89).
- Media-cleanup v2: HARD-DELETE (файлы + L1-строка) per owner-decision (#30).
- Sanitize state_data, quota-enforcement как защитный механизм.

## Unreleased — v1.0 roadmap (in progress, 2026-05-27 → 28)

Overnight-спринт к v1.0: ~36 PR, частично параллельными агентами. M1 и M2
завершены, M3-M6 частично. План: `~/.claude/plans/iridescent-enchanting-rabbit.md`.
Известные блокеры: scope PAT для workflow, Docker IPv6 (см. фикс ниже).

### Added — M1: Test Foundation ✅

- Session-cookie fixture-инфраструктура (#115) + admin-fixture, полный
  claim-флоу с session-фикстурой (#116).
- Юнит-тесты: media peek + resumable upload (#113), CircuitBreaker (#114),
  vault encrypt/decrypt в `user_settings` (#117), quota_enforcer с mock-pool
  (#118), email_client + email_templates (#119), pure-функции `prompts.py`
  (#134).

### Added — M2: Observability ✅

- SRE incident-runbook (#120).
- `scripts/restore-from-l4.sh` + design-doc восстановления из L4 (#121).
- Grafana dashboard + Prometheus alerts (#122).
- SLO-tracking endpoint `/admin/slo` (#123).

### Added — M3: UI i18n (partial)

- Инфраструктура интернационализации RU/EN UI: `sandbox/i18n.js` + локали
  ru/en (#124).

### Added — M4: Features (partial)

- `me-ai-edge` container stub + architecture doc (#127).
- Admin audit-log read endpoint `/admin/audit` (#131).
- Tenant webhook notifications с anti-SSRF + HMAC-подписью (#133).

### Changed — Ops

- Docs `*.md` отдаются по HTTP (`/docs/X.md`) — починены битые ссылки; файлы
  включены в Docker-образ (#129, #130).
- Weekly docker-prune cron + документация memory-scope (#105).

### Fixed

- **Docker IPv6 build (permanent fix)**: buildkit резолвил `registry-1.docker.io`
  по IPv6 → «network is unreachable» на РФ VPS → падал auto-deploy. Решение —
  авто-pre-pull base-образов (`docker pull` с IPv4-fallback) перед каждым build,
  что праймит cache (#132).
- Машина-rename pencil переехал в шапку группы + broadcast label (#100).
- `my_rooms`-запрос: убрана несуществующая `p.user_id` (#126).
- Восстановлены helper'ы `agentDot` + `agentChip` в `profile.html` (регрессия
  из PR #102) (#125).
- Claim-token idempotency (#106).

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
