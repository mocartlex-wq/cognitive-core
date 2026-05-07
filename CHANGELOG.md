# Cognitive Core — Changelog

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
