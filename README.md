# Cognitive Core

> **AI memory + multi-agent rooms + git-сервер + video-generation для AI-агентов.**
> 5-слойная память, MCP-native, RU-first, Stripe/ЮKassa billing, 152-ФЗ compliance.
> Работает с Claude Desktop, Cursor, Cherry Studio, ChatGPT через MCP/Custom Connector.

## ⚡ За 1 минуту: попробовать без установки

**Публичный инстанс:** [https://mcp.me-ai.ru](https://mcp.me-ai.ru) (legacy: https://mcp.ии-память.рф)

1. Email + OTP → [`/ui/login`](https://mcp.me-ai.ru/ui/login)
2. «🪄 Передать помощнику» в [`/ui/profile`](https://mcp.me-ai.ru/ui/profile) → 1-клик настройка для Claude Code / Cursor / ChatGPT
3. После рестарта твоего AI-клиента — 29 MCP-инструментов (`cognitive_remember`, `cognitive_recall`, `room_post`, `cognitive_video_generate`, `cognitive_media_upload_init`...)

Free tier: 10k событий/день, 1 GB медиа, 10 агентов. Pro (490₽/мес или $5/mo) — 10x всё + приоритетная поддержка. Тарифы: [`/ui/pricing`](https://mcp.me-ai.ru/ui/pricing).

## 📦 Что внутри (2026-05-26)

| Феичер | Endpoint / Tool | Документация |
|---|---|---|
| **5-слойная память** (L1→L2→L3→L4 + OP) | `cognitive_remember`, `cognitive_recall` | [concepts.md](docs/concepts.md) |
| **Multi-agent комнаты** + DM | `room_*` (7 tools), `cognitive_send` | [quickstart-rooms.md](docs/quickstart-rooms.md) |
| **Media pipeline** (video→frames+Whisper) | `cognitive_media_upload` | server-side, через nginx |
| **AI Video Generation** (Kling / Sora) | `cognitive_video_generate` | [quickstart-video-generation.md](docs/quickstart-video-generation.md) |
| **External vision providers** (per-tenant keys) | Qwen / MiniMax / GigaChat / YandexGPT / Claude / OpenAI / Gemini | [external-providers.md](docs/external-providers.md) |
| **Self-hosted git** (Gitea на git.me-ai.ru) | Standard git protocol | [gitea-tenant-onboarding.md](docs/gitea-tenant-onboarding.md) |
| **Operating Rules** (Phase 6) | Auto-inject 5 core rules в system_prompt | rules через [`/user/rules`](https://mcp.me-ai.ru/ui/profile) |
| **152-ФЗ compliance** (РФ enterprise) | DPA + ФСТЭК-21 УЗ-3 | [compliance-152fz.md](docs/compliance-152fz.md) |
| **Billing** (Stripe + ЮKassa) | `/api/billing/checkout/{tier}` | [quickstart-billing.md](docs/quickstart-billing.md) |
| **Multi-tenant isolation** (Phase 4) | `owner_user_id` на всех L1-L4 queries | per-tenant MinIO prefix |
| **Resumable upload** (PR #108, no context-cap) | `cognitive_media_upload_init/_finalize` | curl PUT обходит base64 в LLM context |
| **Agent discovery + onboarding** (PR #101, #106, #110) | `claim/peek`, `cognitive_agent_manifest.peers[]`, 🟢-canary, idempotent claim | [agent-discovery.md](docs/agent-discovery.md), [memory-scope.md](docs/memory-scope.md) |
| **Self-hosted instance** (one-liner) | `curl /static/install-self-hosted.sh \| sudo bash` | [onboarding-vps.md](docs/onboarding-vps.md) |

Полный TOC документации: [docs/index.md](docs/index.md).

## Зачем это

Большинство AI-памятей — `add()` + `search()` поверх vector DB. Cognitive Core делает иначе: **сырое событие → дневной анализ → недельный синтез → долговременное знание**, а на каждом переходе LLM-куратор отсеивает шум. Результат: ваш агент **помнит выученные уроки, а не каждое нажатие**.

## Кому

| Профиль | Зачем |
|---|---|
| Solo AI-developer | Persistent память для Claude Desktop / Cursor / Cherry Studio через MCP |
| AI-стартап (5-50 человек) | Аккумуляция опыта команды агентов с audit log |
| Enterprise (regulated) | Self-hosted compliance (GDPR, SOC2), полный audit L5, on-prem |

## За 30 секунд (local)

```bash
git clone <repo-url> cognitive-core && cd cognitive-core
cp .env.example .env  # вставьте DEEPSEEK_API_KEY
docker compose up -d
open http://localhost:9001/   # главная с кнопкой "Запустить демо"
```

## Свой VPS (one-liner, ~10 мин)

Для tenant'ов кому нужна **полная изоляция** (не shared cloud):

```bash
curl -fsSL https://mcp.me-ai.ru/static/install-self-hosted.sh | sudo bash
```

Скрипт спросит домен + email + SMTP creds → выпустит TLS-сертификаты → поднимет docker-compose → прогонит миграции → создаст admin-аккаунт. Полная инструкция: [`docs/onboarding-vps.md`](docs/onboarding-vps.md).

## Production server (1 команда)

```bash
ssh user@your-vps
git clone <repo-url> /opt/cognitive-core && cd /opt/cognitive-core
DOMAIN=cognitive.example.com EMAIL=admin@example.com bash install-server.sh
```

10 минут — рабочий HTTPS-стек с авто-бэкапами, MCP SSE для удалённых клиентов, systemd auto-start. См. [`DEPLOY-SERVER.md`](DEPLOY-SERVER.md).

[![Version](https://img.shields.io/badge/version-v0.5.0--rc1-blue.svg)](CHANGELOG.md)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Postgres 16 + pgvector](https://img.shields.io/badge/postgres-16+pgvector-336791.svg)](https://github.com/pgvector/pgvector)
[![Tests](https://img.shields.io/badge/tests-114%20passing-success.svg)](#тестирование)
[![Stress](https://img.shields.io/badge/stress-171%20ev%2Fs%20p95%20111ms-success.svg)](scripts/stress_test.py)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Что это

Большинство AI-памятей — это «положил вектор → нашёл по сходству». Cognitive Core делает иначе:
**сырое событие → дневной анализ → недельный синтез → долговременное знание**, и на каждом переходе работает LLM-куратор, отсеивающий шум.

Результат: ваш агент **помнит уроки, а не каждое нажатие**.

Через минуту локального демо в системе будет 18 событий, 3 дневных буфера и ~30 выученных знаний.

## Сравнение с конкурентами

| | Mem0 | Letta | Zep | **Cognitive Core** |
|---|---|---|---|---|
| Self-hosted | ✅ | ⚠️ heavy | ⚠️ paid | ✅ один docker compose |
| Multi-layer consolidation | ❌ | ❌ | ❌ | ✅ **3 LLM-уровня** |
| Per-agent state checkpoint | ❌ | ✅ | ❌ | ✅ + общая память |
| Audit log L5 | ❌ | ❌ | ❌ | ✅ append-only |
| Snapshot + restore SHA-256 | ❌ | ❌ | ❌ | ✅ |
| Multi-language | ❌ | ❌ | ❌ | ✅ 8 языков |
| MCP server | ⚠️ | ❌ | ❌ | ✅ stdio + SSE |
| Production install за 10 мин | ❌ | ❌ | ⚠️ | ✅ install-server.sh |

Детальное сравнение с примерами кода — [`COMPARISON.md`](COMPARISON.md). Анализ рынка — [`MARKET.md`](MARKET.md).

## Главная идея — 5 слоёв памяти

```
L1 сырые события  →  L2 дневные срезы  →  L3 эталонные знания  →  L4 архив (S3)
        ↓                  ↓ (LLM)                ↓ (LLM+куратор)         ↓
  POST /events        ежедневно ночью          еженедельно           снапшоты
                                                    ↓
                                              OP — KNN-поиск из агентов
```

| Слой | Что хранит | Когда обновляется | Кто пишет |
|---|---|---|---|
| **L1** raw events | Сырой лог опыта агентов | Сразу при `POST /events` | Агенты |
| **L2** daily buffers | Резюме дня по доменам | Каждую ночь (02:00 UTC) | LLM Daily Analyzer |
| **L3** master knowledge | Подтверждённые знания + tools | Каждую неделю + по аудиту | LLM Weekly Consolidator + Куратор |
| **L4** snapshots | Бэкапы L3 в S3/MinIO | После weekly если L3 изменилась | System |
| **L5** audit log | Каждое действие в системе | Постоянно | Все компоненты |
| **OP** operative | Сессия KNN-поиска по L3 | По запросу `POST /operative/query` | Агенты |

## Почему это лучше обычного RAG

| Обычный RAG | Cognitive Core |
|---|---|
| Кладёт всё подряд | Куратор-LLM фильтрует шум на каждом переходе |
| Знание = текст + вектор | Знание = `{pattern, mistake, rule} + confidence + version + history` |
| Векторный поиск | Векторный поиск + temporal-aware + tools-registry |
| Без аудита | Полный L5 audit-log: кто-когда-что |
| Без бэкапов | L4 snapshot/restore с SHA-256 |
| 1 контекст на всё | Изоляция по доменам, мульти-агент |

## Стек

- **Python 3.11** + **FastAPI** + **asyncpg**
- **PostgreSQL 16** с расширением **pgvector** (HNSW для KNN)
- **Redis Stack** (RediSearch для быстрого KNN с TAG-фильтром)
- **MinIO** (S3-совместимое хранилище для L4)
- **DeepSeek V4 Pro** как основной LLM (с fallback на OpenAI / Ollama)
- **fastembed** `multilingual-e5-small` для эмбеддингов (CPU, 384-dim, 8 языков)
- **Docker Compose** — поднимается одной командой

Без npm/build для UI: vanilla HTML+CSS+JS, ~600 LOC.

## Установка и запуск

### Требования
- Docker + Docker Compose v2
- 4 GB RAM (для контейнеров) + 1 GB для fastembed model cache

### Шаги

```bash
# 1. Клонировать
git clone <repo-url> cognitive-core
cd cognitive-core

# 2. Конфигурация
cp .env.example .env
# Откройте .env и установите DEEPSEEK_API_KEY (получить на platform.deepseek.com)

# 3. Запустить
docker compose up -d --build

# 4. Проверить
curl http://localhost:9001/health
# {"healthy":true,"version":"0.2.0",...}

# 5. Загрузить демо-данные (опционально — можно из UI)
python scripts/seed_demo.py --full
```

### Порты по умолчанию
- `9001` — Web UI + API (FastAPI)
- `5432` — PostgreSQL
- `6379` — Redis
- `9000` / `9002` — MinIO API / Console (admin: `minioadmin`/`minioadmin`)

## Web-интерфейс

| URL | Назначение |
|---|---|
| `http://localhost:9001/` | Главная — объяснение, диаграмма, кнопка демо |
| `http://localhost:9001/ui` | Дашборд — live-метрики, графики, обозреватель знаний |
| `http://localhost:9001/sandbox` | Песочница API — все эндпоинты по этапам |
| `http://localhost:9001/docs` | OpenAPI Swagger |

## API кратко

```bash
# Записать событие в L1
curl -X POST http://localhost:9001/events \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{"source_agent":"my_agent","domain":"my_domain","payload":{"task":"...","result":"...","feedback":"positive"}}'

# Найти знания по KNN
curl -X POST http://localhost:9001/operative/query \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{"domain":"my_domain","context":"how to ...","top_k":5}'

# Запустить daily консолидацию
curl -X POST "http://localhost:9001/memory/consolidate/daily?domain=my_domain" \
  -H "X-API-Key: key-design-001"
```

Полный список — в [DEMO.md](DEMO.md) или открыв `/docs`.

## Python SDK

```python
from cognitive import AsyncMemoryClient

async with AsyncMemoryClient(
    base_url="http://localhost:9001",
    api_key="key-design-001",
) as memory:
    # Записать опыт
    await memory.remember(
        domain="codegen",
        payload={"task": "...", "result": "...", "feedback": "positive"},
    )
    # Найти знания
    results = await memory.recall(domain="codegen", context="how to ...")
```

См. [cognitive-client/README.md](cognitive-client/) для полного API.

## MCP-сервер для Claude Desktop / Cursor / Code

Cognitive Core можно подключить к любому MCP-совместимому ИИ-клиенту:

```json
// claude_desktop_config.json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/cognitive-core",
      "env": {
        "COGNITIVE_API_URL": "http://localhost:9001",
        "COGNITIVE_API_KEY": "key-design-001"
      }
    }
  }
}
```

После перезапуска Claude получает 7 инструментов: `cognitive_remember`, `cognitive_recall`, `cognitive_list`, `cognitive_tools`, `cognitive_consolidate`, `cognitive_health`, `cognitive_domains`.

Подробности в [mcp_server/README.md](mcp_server/README.md).

## Тестирование

```bash
# Все тесты в контейнере
docker exec cognitive_api python -m pytest tests/ -v

# Только юнит-тесты (без сети/LLM)
docker exec cognitive_api python -m pytest tests/test_sanitizer.py tests/test_embedder.py tests/test_models.py -v

# Только API + интеграция
docker exec cognitive_api python -m pytest tests/test_api.py tests/test_dashboard.py -v
```

Текущие категории:
- `test_models.py` — Pydantic-схемы (12 тестов)
- `test_sanitizer.py` — фильтр SQL/JS/XSS/shell (20 тестов)
- `test_embedder.py` — эмбеддинги (3 теста)
- `test_api.py` — интеграция API (34 теста)
- `test_dashboard.py` — read-only обозреватели (8 тестов)
- `test_advisory_lock.py` — защита от двойной консолидации (3 теста)
- `test_pgvector.py` — pgvector интеграция (6 тестов)
- `test_demo.py` — streaming /demo/run (2 теста)

## Безопасность

- **Аутентификация**: X-API-Key per-agent через `AGENT_API_KEYS` в `.env`
- **Rate limiting**: 100 событий/сек на агента (Redis INCR + TTL)
- **Sanitizer**: блокирует SQL-инъекции, JS, XSS, shell-команды до записи в L1
- **JSON depth limit**: ≤10 уровней, ≤500 ключей, ≤256KB payload
- **Audit log L5**: каждое действие логируется (агент, время, цель, успех/ошибка)
- **L4 snapshots**: SHA-256 проверка целостности при восстановлении

Для production:
- TLS через nginx — конфиг в [nginx/](nginx/) + [docker-compose.prod.yml](docker-compose.prod.yml)
- Бэкапы Postgres: [scripts/backup_postgres.sh](scripts/backup_postgres.sh)
- Подробнее: [DEPLOY.md](DEPLOY.md)

## Hot-reload эмбеддингов

При смене эмбеддинг-модели в `app/services/embedder.py`:

```bash
# Удалит stale-векторы из Redis + переиндексирует все домены
curl -X POST http://localhost:9001/memory/reindex \
  -H "X-API-Key: key-design-001"

# Cold-start после рестарта Redis (без LLM-вызовов)
curl -X POST http://localhost:9001/memory/restore-redis \
  -H "X-API-Key: key-design-001"
```

## Производительность

На обычном ноутбуке (M2 / 16GB / без GPU):
- POST /events: **~5 ms**
- KNN-запрос (RediSearch, до 1000 знаний): **~3-5 ms**
- KNN-запрос (pgvector HNSW, до 1000 знаний): **~5-15 ms**
- Daily consolidation (DeepSeek): **~10 сек на домен**
- Weekly consolidation: **~20-30 сек на домен**
- fastembed на CPU: **~50-100 ms на embed**

## Дорожная карта

| Версия | Статус | Что |
|---|---|---|
| v0.2.0 | ✅ | MVP: 5 слоёв, 13 эндпоинтов, 69 тестов |
| v0.2.5 | ✅ | UX: главная, дашборд, песочница, общий дизайн |
| v0.3.0 | ✅ | Reliability: pgvector, advisory lock, hot-reload |
| v0.4.0 | 🔄 | Reach: MCP-сервер, GitHub publish, видео-демо |
| v0.5.0 | ⏳ | Production: Alembic, TLS, CI, бэкапы, runbook |
| v1.0.0 | ⏳ | Scale: мультитенантность, Celery, шардирование, Ollama+GPU |
| v2.0.0 | ⏳ | Advanced: граф знаний, temporal queries, активное обучение |

Полный план — [roadmap.md](roadmap.md).

## Структура проекта

```
cognitive-core/
├── app/                       # FastAPI приложение
│   ├── api/                   # Роутеры: events, operative, memory, tools, dashboard, demo
│   ├── db/                    # Адаптеры: postgres (asyncpg), redis, s3 (minio)
│   ├── models/                # Pydantic-схемы
│   ├── security/              # auth, sanitizer, validator, audit
│   ├── services/              # Бизнес-логика: ingestor, analyzer, consolidator, curator,
│   │                          #    embedder, llm_client, operative, tools, prompts, metrics
│   ├── main.py                # FastAPI + lifespan + middleware
│   ├── config.py              # Pydantic-settings из .env
│   └── worker.py              # Фоновый scheduler (daily/weekly/monthly)
├── sandbox/                   # Web UI: home.html, dashboard.html, index.html, shared.css, tour.js
├── mcp_server/                # MCP-сервер для Claude Desktop / Cursor / Code
├── cognitive-client/          # Python SDK (sync + async)
├── tests/                     # 80+ pytest
├── scripts/                   # seed_demo.py, delegate_deepseek.py, consult_deepseek.py
├── nginx/                     # nginx.conf для production TLS
├── docker-compose.yml         # Локальная разработка
├── docker-compose.prod.yml    # Production (с nginx + TLS)
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md                  # ← вы здесь
├── DEMO.md                    # Пошаговая инструкция демо
├── DEPLOY.md                  # Runbook production-деплоя
├── roadmap.md                 # План развития
└── CLAUDE.md                  # Заметки для AI-помощников
```

## Вклад в проект

PR'ы приветствуются. Перед PR:
1. `docker exec cognitive_api python -m pytest tests/` должен быть зелёным
2. Новый код покрыт тестами
3. Изменение API → обновить `DEMO.md`
4. Изменение архитектуры → обновить `roadmap.md`

## Лицензия

MIT — см. [LICENSE](LICENSE).

## Благодарности

- [DeepSeek](https://www.deepseek.com/) — основной LLM
- [pgvector](https://github.com/pgvector/pgvector) — векторный поиск в Postgres
- [Redis Stack](https://redis.io/docs/stack/) — KNN с TAG-фильтрами
- [fastembed](https://github.com/qdrant/fastembed) — лёгкие эмбеддинги без GPU
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP-сервер на Python
