# Cognitive Core — Roadmap

Актуальный план работ. Обновлён после совета трёх (Claude + DeepSeek + owner) и UX-итерации.

## Релизный поток

```
v0.2.0 (есть) ──► v0.2.5 UX ──► v0.3 Reliability ──► v0.4 Reach ──► v0.5 Production ──► v1.0 Scale ──► v2.0 Advanced
                  частично          1 неделя          2 недели        1 месяц          2-3 мес        4-6 мес

Дизайн-линия (параллельно всем версиям):
  v0.2.5 базовая дизайн-система shared.css → v0.4 Apple Liquid Glass (главная) →
  v0.5 glass-режим для дашборда и песочницы → v1.0 light/dark темы → v2.0 анимированный constellation L3
```

## Дизайн-pillar (отдельная линия работ)

Дизайн — не «после релиза», а параллельная линия с тремя горизонтами:

| Горизонт | Что | Статус |
|---|---|---|
| **0** Дизайн-система | shared.css + glass.css, переменные, компоненты | ✅ v0.2.5 + v0.4 |
| **1** Apple Liquid Glass | Главная (`/`): floating orbs, frosted glass cards, animated flow | ✅ v0.4 |
| **2** Glass-режим везде | Перенести glass на `/ui` дашборд и `/sandbox` (toggle) | ⏳ v0.5 |
| **3** Theme switcher | Light/dark режимы, system-pref auto-detect | ⏳ v0.5 |
| **4** Memorable hero | Анимированные частицы L1→L4 (есть) → расширить до constellation L3 | ⏳ v2.0 |
| **5** Motion guidelines | Стандарт переходов и hover (cubic-bezier, длительности) | ⏳ постоянно |
| **6** Иконки SF-style | Заменить эмодзи (📊 ⚙ ▶) на консистентный SVG-набор | ⏳ v0.5 |

**Дизайн-принципы (зафиксировано):**
- Apple Liquid Glass: frosted glass на тёмной градиентной базе + floating orbs
- Glass: карточки, кнопки, индикаторы. **Solid: H1, body, числа, навигация** (правило DeepSeek для accessibility)
- Скругления: 14px (small), 22px (default), 32px (XL)
- Шрифты: SF Pro / системные (`-apple-system, BlinkMacSystemFont, ...`)
- Цвета акцента: `#58a6ff` (синий) + `#bc8cff` (фиолетовый) + `#ff8c42` (оранжевый, redko)
- Анимации: `cubic-bezier(0.4, 0, 0.2, 1)`, длительность 0.15s/0.22s/0.3s
- Hover: `translateY(-2px)` + усиление shadow
- Производительность: `backdrop-filter` только на ограниченных элементах, не на body


---

## v0.2.0 — Стабильный MVP (есть)

**Состояние на 2026-05-03:**
- 31 .py, ~4400 LOC, 69/69 тестов passing
- 5 слоёв: L1=200 events, L2=13 buffers, L3 knowledge=61, L3 tools=41, L4=25 snapshots
- 13 доменов с реальными данными
- Docker-стек: Postgres 16 + Redis Stack + MinIO + FastAPI
- LLM: DeepSeek V4 Pro, эмбеддинги: fastembed multilingual-e5-small (384 dim, CPU)
- 8 языков промптов (ru/en/zh/ja/ko/es/ar/pt)

---

## v0.2.5 — UX & Interfaces (в процессе)

**Цель:** новичок понимает что это за проект и как им пользоваться за 5 минут.

| # | Задача | Статус | Файлы |
|---|---|---|---|
| 1 | Единая дизайн-система (shared.css) | ✅ | `sandbox/shared.css` |
| 2 | Главная страница с объяснением идеи | ✅ | `sandbox/home.html` (`/`) |
| 3 | Унифицированная навигация (top-bar на всех страницах) | ✅ | все .html |
| 4 | Контекстные подсказки (tooltips) к каждому слою/действию | ✅ | shared.css `.help` |
| 5 | Dashboard переделан под общий стиль + объяснения каждой вкладки | ✅ | `sandbox/dashboard.html` |
| 6 | Sandbox перегруппирован по этапам жизненного цикла (5 stages) | ✅ | `sandbox/index.html` |
| 7 | Маршруты: `/` → home, `/ui` → dashboard, `/sandbox` → API | ✅ | `app/main.py` |
| 8 | StaticFiles mount для shared.css | ✅ | `app/main.py` |
| 9 | Live-индикаторы статуса PG/Redis/S3 на всех страницах | ✅ | top-bar |
| 10 | Inline-объяснение жизненного цикла события на главной | ✅ | home.html steps |
| **Осталось:** | | | |
| 11 | Кнопка "Запустить демо" на главной должна вызывать API напрямую | ⏳ | home.html |
| 12 | Auto-refresh домены/события/аудит в дашборде на ws/SSE | ⏳ | dashboard.html |
| 13 | Embedded onboarding-tour (1-й визит → подсказки по UI) | ⏳ | новый файл |
| 14 | Скриншоты + GIF в README для GitHub | ⏳ | docs/ |

**Definition of Done v0.2.5:** новый разработчик открывает `/`, понимает идею за 1 минуту, кликает «Тестировать API», нажимает «Bulk 5 событий» → «Запустить daily» → видит результат в дашборде.

---

## v0.3 — Reliability (1 неделя)

**Цель:** проект корректно работает при росте нагрузки и при простое.

| # | Задача | Эффорт | Done-критерий |
|---|---|---|---|
| 1 | `git init` + первый коммит + .gitignore-аудит | 30 мин | публичная история, ничего не теряется |
| 2 | Postgres advisory lock для consolidator | 3 часа | при 2+ инстансах daily не дублируется |
| 3 | pgvector колонка в L3 + HNSW индекс | 1 день | эмбеддинги переживают рестарт Redis, KNN O(log N) |
| 4 | Cold-start: загрузка эмбеддингов из Postgres → Redis | 4 часа | после рестарта Redis — KNN сразу работает без LLM-вызовов |
| 5 | Hot-reload эмбеддингов: версия модели в индексе | 4 часа | смена модели в .env → старые векторы помечаются stale |
| 6 | Тесты на advisory lock + pgvector | 4 часа | 75+ тестов |

**Definition of Done v0.3:** push в git, `docker compose up --scale api=2` — daily срабатывает только в одном инстансе.

---

## v0.4 — Reach (2 недели)

**Цель:** проект становится видимым и используемым извне.

| # | Задача | Эффорт | Эффект |
|---|---|---|---|
| 1 | MCP-сервер поверх operative+events (FastMCP) | 3 дня | доступ из Claude Desktop / Cursor / Code |
| 2 | GitHub repo + README + DEMO.md + CONTRIBUTING | 1 день | находимость, первые звёзды |
| 3 | 2-минутное demo-видео (loom/asciinema) | 4 часа | показать end-to-end за 2 минуты |
| 4 | Embedded onboarding-tour в `/` | 1 день | первый визит → подсказки по UI |
| 5 | Dashboard: вкладка "Use cases" с готовыми сценариями | 1 день | onboarding для новых пользователей |
| 6 | docker-compose + один .env.example для quickstart | 4 часа | `docker compose up` → всё за 30 сек |
| 7 | CHANGELOG + semver-tag v0.4.0 | 2 часа | публичный релиз |

**Definition of Done v0.4:** репо публичное, MCP работает в Claude Desktop у тестера, видео-демо есть, релиз на GitHub.

---

## v0.5 — Production (1 месяц)

**Цель:** можно дать клиенту в self-hosted без стыдных пробелов.

| # | Задача | Эффорт | Зачем |
|---|---|---|---|
| 1 | Alembic миграции (вместо CREATE TABLE IF NOT EXISTS) | 1 день | управляемая эволюция схемы |
| 2 | TLS через nginx + сертификаты | 1 день | без HTTPS не возьмёт enterprise |
| 3 | Закрытые секреты: Docker secrets / Vault | 1 день | убрать `cognitive_secret` из docker-compose |
| 4 | CI: pytest + ruff + mypy на каждый PR | 1 день | защита от регрессий |
| 5 | Reactivate l_arbitration: процесс конфликтов в дашборде | 2 дня | таблица есть, но не используется |
| 6 | L4 restore: проверка целостности (total_records vs JSON) | 4 часа | DeepSeek нашёл этот gap |
| 7 | Rate-limit конфигурируемый (per-domain, per-agent) | 1 день | enterprise-кастомизация |
| 8 | Структурированные ошибки + error-codes | 1 день | клиентам нужен предсказуемый API |
| 9 | Бэкап PostgreSQL по расписанию + restore | 1 день | DR-готовность |
| 10 | Документация: deploy guide, runbook, troubleshooting | 2 дня | обязательно для self-hosted |
| 11 | Onboarding-tour полноценный (intro.js / shepherd.js) | 1 день | enterprise-демо |
| 12 | Темы оформления (light/dark) | 4 часа | предпочтение пользователя |

**Definition of Done v0.5:** одна команда `make deploy` → запущенный сервер с TLS + бэкапами + мониторингом + CI зелёный + 90+ тестов.

---

## v1.0 — Scale (2-3 месяца)

**Цель:** обрабатывает десятки тысяч событий в день, мульти-тенантность.

| # | Задача | Эффорт |
|---|---|---|
| 1 | Мультитенантность: организации, квоты | 1 неделя |
| 2 | Worker как отдельный сервис (вытащить из API-процесса) | 3 дня |
| 3 | Celery + Redis-broker для асинхронных задач | 1 неделя |
| 4 | Партиционирование L1/L5 по времени | 3 дня |
| 5 | Шардирование L3 по домену (если домены > 100k записей) | 1 неделя |
| 6 | Локальный ИИ (Ollama) с GPU-пробросом — Этап 8 плана | 3 дня |
| 7 | A/B-стат в Postgres (вместо in-memory dict) | 4 часа |
| 8 | Prometheus + Grafana дашборды | 2 дня |
| 9 | Alerting через Alertmanager | 1 день |
| 10 | UI: настройка модели через дашборд (без редактирования .env) | 2 дня |
| 11 | UI: визуальный редактор L3 знаний | 1 неделя |

**Definition of Done v1.0:** Kubernetes Helm-чарт, нагрузочный тест 10k events/min, 3+ инстанса API + worker отдельно.

---

## v2.0 — Advanced (4-6 месяцев)

**Цель:** обогнать Mem0/Letta/Zep по уникальности.

| # | Задача | Эффорт |
|---|---|---|
| 1 | Граф знаний поверх L3 (`prerequisite/contradicts/generalizes`) | 2 недели |
| 2 | Temporal queries («что я знал на дату X») | 1 неделя |
| 3 | Контекст-сборка с приоритизацией (умный токен-budget) | 1 неделя |
| 4 | Активное обучение: gap-detection в L3 | 2 недели |
| 5 | Кросс-доменный перенос паттернов | 1 неделя |
| 6 | Streaming/WebSocket для real-time operative | 1 неделя |
| 7 | Полноценная админ-панель (тогда уже SPA: React/Solid) | 3 недели |
| 8 | Visual editor для L3 с merge-конфликтами | 2 недели |
| 9 | UI: timeline-view знаний во времени | 1 неделя |
| 10 | UI: graph-view связей между знаниями (cytoscape.js) | 2 недели |

---

## Что НЕ делаем (явно отложено)

| Задача | Причина |
|---|---|
| Дельта-снапшоты L4 | 25 × 8MB = 200MB — не проблема. Сделать по достижении 100GB |
| Vanilla → React/Vue SPA сейчас | Ломает self-hosted USP, текущий dashboard покрывает кейсы. Vue/React — только в v2.0 для admin-panel |
| MongoDB / любая другая БД | Postgres + Redis + S3 закрывают всё |
| LangChain / LlamaIndex как основа | Своя архитектура — это и есть USP |
| Embedded vector DB (Qdrant/Milvus standalone) | pgvector в Postgres достаточно до ~10M векторов |

---

## Критический путь к ценности (минимум для запуска в свет)

```
git init (30мин)
  └──► advisory lock (3ч)
         └──► pgvector (1д)
                └──► онбординг-тур (1д)
                       └──► MCP-сервер (3д)
                              └──► GitHub publish + видео (1д)
                                     └──► публичный v0.4.0
```

**Итого:** 6 рабочих дней до публичного релиза с UX-полировкой и MCP.

---

## Архитектура UI (зафиксировано)

| Страница | URL | Назначение | Кому |
|---|---|---|---|
| Главная | `/` | Объяснение идеи + жизненный цикл + быстрый старт | Новый посетитель |
| Дашборд | `/ui` | Live-метрики, обозреватель слоёв, графики, аудит | Оператор / демо клиенту |
| Песочница | `/sandbox` | Все API-эндпоинты по этапам жизненного цикла | Разработчик при отладке |
| Health | `/health` | JSON статус всех сервисов | Мониторинг / автоматизация |
| Metrics | `/metrics` | Prometheus-формат | Grafana / алерты |
| MCP | `/mcp` (TODO v0.4) | Model Context Protocol endpoint | Claude Desktop / Cursor / Code |
| Docs | `/docs` | OpenAPI Swagger UI (FastAPI default) | Разработчик-интегратор |

**Дизайн-система:**
- Один `shared.css` для всех страниц (переменные, типографика, компоненты)
- Тёмная тема, акцент `#58a6ff`
- Tooltip-подсказки `.help` для контекстной справки
- Live-индикаторы для real-time статуса
- Без зависимостей кроме Chart.js для графиков

---

## v0.5.0-prod — Production-readiness sprint (3 дня, до v0.5 release)

**Trigger:** owner требует чтобы приложение было (1) обновляемо удалённо, (2) публично доступно и просто для конечного пользователя-агента, (3) стабильно для production. Сформулировано 2026-05-07. Согласовано two-voice (я + DeepSeek), оба независимо выявили одни и те же gaps.

| # | Задача | Эффорт | Качество |
|---|---|---|---|
| 1 | **Smoke-test + auto-rollback** в `scripts/auto-deploy.sh`: после rebuild проверить `curl /health` 30 сек подряд, если 5xx — `git reset --hard <prev-sha>` + reload | 4 часа | remote update |
| 2 | **Circuit breaker + LLM graceful degradation** для DeepSeek API (упал — daily/weekly помечают как pending, /health возвращает healthy=true с warning, KNN продолжает работать) | 1 день | stability |
| 3 | **Per-agent API keys** + endpoint `POST /agents/issue-key` (генерирует key per agent, привязан к `agent_id` для аудита и отзыва) | 4 часа | public + simple |
| 4 | **Deep health-check per layer** (postgres queries timing, Redis index existence, MinIO ListBuckets, last consolidation timestamp, disk free, llm reachability) | 4 часа | stability |
| 5 | **Notifications о deploy-failure** (Telegram bot или Discord webhook): успех — silent, ошибка — alert | 2 часа | remote update |
| 6 | **Versioned configs** — на сервере хранить `nginx.conf.<sha>` снапшоты последних 10 версий, для отката конфига без отката кода | 2 часа | remote update (DS) |
| 7 | **Backup retention** — TTL 14 дней в `scripts/cron-backup.sh`, старые pg_dump удаляются автоматически | 1 час | stability |
| 8 | **One-line agent install** — `curl https://mcp.ии-память.рф/onboard.sh \| bash` генерирует per-agent key + готовый JSON для Claude Desktop / Cursor | 4 часа | public + simple (DS) |

**Definition of Done v0.5.0-prod:** auto-deploy переживает любой битый коммит без вмешательства человека; падение DeepSeek не валит endpoint; новый агент подключается одной командой; все алерты о problem-ах приходят в Telegram.

После этого можно ставить тег **v0.5.0** и считать публично-готовым.

---

## v0.5.5 — Multi-agent collaboration (1-2 недели)

**Trigger:** owner озвучил 2026-05-07 — нужно чтобы AI-агенты на разных ПК (Claude Code на разных машинах, Cursor, custom) работали **под одним проектом**, общаясь через сервер. Разблокирует параллельную разработку самой системы и объединение разных проектов экосистемы под общей memory-инфраструктурой.

**Цель:** MVP набор примитивов для inter-agent communication поверх Cognitive Core, без полноценного chat-UI или workflow-engine.

**MVP scope** (финальный после двух раундов DS):

Делится на два слоя — durable agent-protocol (через L1) и fast realtime (через Redis L0).

**Durable layer — agent protocol:**

| # | Задача | Эффорт | Done-критерий |
|---|---|---|---|
| 1 | Agent identity / registry (`POST /agents/register`) | 4 часа | уникальный agent_id + project + capabilities в `agent_states` |
| 2 | Direct messages (durable, через L1 events `domain=agent_inbox`) | 1 день | агент A пишет, агент B видит через `cognitive_recall` или `GET /agents/<id>/inbox`, переживает рестарт |
| 3 | Realtime push через SSE (`GET /agents/<id>/stream`) | 1 день | сообщения приходят без polling |
| 4 | Per-agent rate-limit на write-операции | 4 часа | spam от одного агента не валит общий поток |

**Fast L0 layer (Redis) — все 5 примитивов:**

| # | Примитив | Эффорт | Use-case |
|---|---|---|---|
| 5 | Blackboard (Redis hash `project:<name>:state` + TTL) | 4 часа | общее состояние проекта, current_branch / phase / blockers |
| 6 | Presence + heartbeat (Redis hash с TTL 60s, ping каждые 30s) | 4 часа | `GET /agents/online?project=X` |
| 7 | Scratchpad (Redis LIST capped 1000, TTL 7 дней) | 4 часа | быстрый координационный чат проекта |
| 8 | Pub/Sub channels по project | 4 часа | мгновенные уведомления (commit, deploy, build done) |
| 9 | Coordination locks (Redis SETNX с TTL) | 4 часа | take-lock-before-edit на shared files |

**Обвязка и тесты:**

| # | Задача | Эффорт |
|---|---|---|
| 10 | `cognitive-client` Python SDK: `heartbeat()`, `lock()`, `chat()`, `state[]`, `events.subscribe()` | 1 день |
| 11 | Тесты на concurrent messages, race conditions на locks, presence TTL | 1 день |
| 12 | Документация — `MULTI_AGENT.md` с примерами + `AGENT_HANDOFF.md` (готов) + `FAST_MEMORY.md` (готов) | 4 часа |

**Эффорт суммарно:** ~7-8 рабочих дней.

**Definition of Done v0.5.5:** запускаем 3 агента на разных машинах, каждый видит presence остальных через `GET /agents/online`, шлёт direct messages, новые messages приходят push-ом через SSE без polling-а. Документация — в `MULTI_AGENT.md`.

**Anti-patterns (НЕ делаем — финал после DS):**
- Полноценный chat-UI (текстовое API достаточно)
- Сложная аутентификация поверх существующих API keys (среда self-hosted, доверенная)
- Дублирование message history — она и так в L1 + L5 audit
- Distributed locks / broadcast в первом релизе (откладываем до реального use case)
- Workflow-engine с DAG, email/Slack/Telegram интеграции
- Распределённый consensus (Raft, Paxos) — для одного сервера не нужно

**Архитектурное замечание:** MVP — это в основном API-обвязка над существующими примитивами (`agent_states`, L1 events, MCP SSE). Большой сложности нет, главный риск — race conditions на heartbeat-ах и spam от заброшенного агента (mitigation: per-agent rate-limit + TTL на presence).

**Совет двух (зафиксировано 2026-05-07):**
- Claude (я) предлагал 6 примитивов в MVP: registry, DM, broadcast, locks, presence, SSE-push
- DeepSeek сократил до 4 (a, b, e, f), отложив broadcast и locks как преждевременную сложность
- Owner — третий голос, должен утвердить sized scope перед началом v0.5.5

---

## v0.6 — GPU & Hybrid Local+Cloud LLM

**Trigger:** owner's RTX 24GB upgrade (планируется после dogfooding на текущем железе).

**Что входит:**
- ✅ `Dockerfile.gpu` + `docker-compose.gpu.yml` — fastembed на CUDA (5-7× ускорение)
- ✅ `docker-compose.local-llm.yml` — Ollama overlay с GPU
- ✅ `LOCAL_LLM.md` — гайд переключения функций на местные модели
- ✅ `/health.embedding.provider` — наблюдаемость CPU/CUDA/unavailable
- ⏳ A/B тестирование `ollama:deepseek-r1:14b` vs `deepseek-chat` на daily
- ⏳ Дашборд: график latency CPU vs CUDA, успешность local vs cloud

**Decision matrix (DeepSeek-validated):**
- Локально на 24GB: embeddings, daily L1→L2, vision (v0.7), whisper (v0.7)
- Cloud остаётся: weekly L2→L3, curator audit, quality scoring (точность важнее экономии)

**Risk mitigation:** weekly cloud-pass корректирует ошибки локального daily — quality cascade не происходит.

---

## v0.7 — Multimodal layer

**Зачем:** агенты работают не только с текстом — скриншоты, голос, диаграммы. Multimodal расширяет use case.

**Компоненты:**
- Vision: `qwen2.5vl:7b` (Ollama) — OCR + image description, $0/изображение
- Voice: `faster-whisper-large-v3-int8` — транскрипция, $0/час
- Storage: MinIO bucket `l4-multimodal/` для оригиналов
- Ring retention: circular buffer + importance escape (см. memory feedback)
- API: `POST /events/multimodal` — принимает image/audio + текстовый контекст

**Зависимость:** v0.6 (GPU stack должен быть готов).

---

## Метрики успеха

| Релиз | Метрика | Цель |
|---|---|---|
| v0.2.5 UX | Время до первого «понял» (новый пользователь) | < 1 мин |
| v0.2.5 UX | Время до первого успешного API-вызова | < 3 мин |
| v0.3 | Тесты | 75+ passed |
| v0.3 | Race-condition при 2 инстансах | 0 дублей в L2 |
| v0.4 | GitHub stars (3 месяца) | 100+ |
| v0.4 | MCP в Claude Desktop у независимого тестера | работает |
| v0.5 | Pilot deployment | 1+ self-hosted у внешнего пользователя |
| v0.6 | Embedding latency на CUDA | <5 ms (было 15-30ms CPU) |
| v0.6 | Local daily quality vs cloud (A/B) | >= 0.95 от cloud baseline |
| v0.7 | Vision OCR accuracy на RU+EN скриншотах | >= 0.90 |
| v0.7 | Whisper точность на RU аудио | WER < 0.10 |
| v1.0 | Throughput | 10 000 events/min |
| v1.0 | Multi-tenant orgs на одном инстансе | 10+ |
| v2.0 | Бенчмарк vs Mem0/Zep | выигрыш на specific задачах |

---

**Последнее обновление:** 2026-05-03 (после UX-итерации, добавлен раздел v0.2.5)
