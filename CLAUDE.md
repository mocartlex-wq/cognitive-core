# CLAUDE.md — Cognitive Core

## Build & Run

```bash
# Сборка и запуск всех сервисов
docker compose up -d --build

# Пересборка только API
docker compose up -d --build api

# Просмотр логов
docker logs cognitive_api --tail 50

# Прогнать все тесты (56 шт.)
docker exec cognitive_api python -m pytest tests/ -v

# Прогнать только юнит-тесты (без LLM/сети)
docker exec cognitive_api python -m pytest tests/test_sanitizer.py tests/test_embedder.py tests/test_models.py -v

# Прогнать только API-тесты
docker exec cognitive_api python -m pytest tests/test_api.py -v

# Проверить здоровье
curl http://localhost:9001/health
```

**Порты:**
- `9001` — FastAPI
- `9002` — MinIO console
- `5432` — PostgreSQL
- `6379` — Redis Stack

**API ключи:** `key-design-001` (agent_designer), `key-dev-001` (agent_developer)

## Архитектура

5-слойная система памяти для AI-агентов:

```
L1 (raw_events)   → сырые события от агентов
L2 (daily_buffers) → дневная консолидация через LLM
L3 (master_knowledge + tools_registry) → эталонные знания и реестр инструментов
L4 (snapshots)     → полные/дельта-снапшоты в MinIO (S3)
L5 (audit_log)     → журнал всех операций
```

**Operative workspace (OP):** KNN-поиск по L3 + сессии в Redis.
Первый запрос создаёт векторы → последующие идут через RediSearch.

## Структура ключевых файлов

```
app/
├── main.py              # Точка входа, lifespan, middleware, роутеры
├── config.py            # Все настройки через pydantic-settings
├── worker.py            # Фоновый планировщик (daily/weekly/monthly циклы)
├── api/
│   ├── events.py        # POST /events — приём событий
│   ├── operative.py     # POST /operative/query, close, feedback
│   └── memory.py        # POST /memory/consolidate/daily|weekly, /memory/audit/monthly, snapshots, cleanup
├── db/
│   ├── postgres.py      # asyncpg pool + CREATE TABLES
│   ├── redis.py         # Redis клиент + get_redis_raw() для векторов
│   └── s3.py            # MinIO клиент
├── models/              # Pydantic-модели для всех запросов
├── security/
│   ├── auth.py          # X-API-Key проверка + rate limiting
│   ├── sanitizer.py     # Санитизация payload (SQL/XSS/JS/shell)
│   └── audit.py         # Запись в L5 аудит-лог
├── services/
│   ├── ingestor.py      # Сохранение в L1
│   ├── analyzer.py      # L1→L2 дневной анализ (LLM)
│   ├── consolidator.py  # L2→L3 недельная консолидация (LLM)
│   ├── curator.py       # L3 аудит (месячный), фильтрация, качество (LLM)
│   ├── operative.py     # OP: KNN поиск, сессии, фидбек
│   ├── embedder.py      # Эмбеддинги: OpenAI → Ollama → хеш-fallback
│   ├── llm_client.py    # Единый LLM-клиент с A/B-тестом и fallback-цепочкой
│   ├── prompts.py       # Мультиязычные промпты (8 языков)
│   ├── tools.py         # CRUD реестра инструментов
│   └── metrics.py       # Prometheus + JSON-логирование
├── sandbox/
│   └── index.html       # API-песочница (доступна по GET /)
└── cognitive-client/    # Python SDK для внешних агентов
    ├── pyproject.toml
    └── cognitive/
        ├── __init__.py
        └── client.py    # AsyncMemoryClient + MemoryClient
```

## Важные конвенции

### JSONB — всегда строки!
asyncpg 0.30 требует JSON-строки для JSONB колонок.
- **Запись:** `json.dumps(dict_or_list, ensure_ascii=False)` всегда
- **Чтение:** `_parse_jsonb(val)` в operative.py — парсит строку в dict/list

### Redis — два клиента
- `get_redis()` — `decode_responses=True`, для текстовых данных (сессии, фидбек)
- `get_redis_raw()` — `decode_responses=False`, для бинарных векторов (KNN)

### RediSearch
- Один индекс `idx:operative` (PREFIX `op:`) с VECTOR FLAT FLOAT32 1536 COSINE
- `init_redis()` автосоздаёт индекс при старте
- KNN синтаксис: `@domain:{...}=>[KNN N @embedding $vec]`

### LLM
- Функции куратора: `analyzer.py` (daily), `consolidator.py` (weekly), `curator.py` (audit/filter/quality)
- Промпты: `prompts.py` с авто-выбором языка из `settings.system_language`
- `LLMClient` сам выбирает модель по `llm_<function>` настройкам

### Безопасность
- Все эндпоинты (кроме `/`, `/health`, `/metrics`) требуют `X-API-Key`
- Санитайзер отклоняет SQL/JS инъекции, экранирует HTML и shell-команды
- Rate-limit через Redis INCR с TTL 1 сек

## Подводные камни (gotchas из аудита 2026-06-14)

Заметки, чтобы новая сессия не повторила мои ошибки прошлого аудита:

- **`scripts/cognitive-rooms.py:3010` — это `ThreadingHTTPServer`, не single-thread.** Concurrency-bottleneck'а на уровне модели потоков НЕТ. `_PG_CONN_LOCK` защищает shared psycopg-коннекцию. Не предлагать «async refactor» как BLOCKING — это уже не нужно.
- **`launch/extras/cognitive-rooms.py` — это public reference**, не дубль. Используется `launch/docker-compose.public.yml` и `launch/E2E_RESULTS.md`. Не удалять. Security-фиксы из `scripts/` нужно бэкпортить туда.
- **SQL-проверка в санитайзере убрана сознательно** (`app/security/sanitizer.py:93`, коммит 2026-05-26). Все SQL-запросы параметризованы asyncpg. Старый фильтр блокировал валидные em-dash и shell-args. Не «возвращать как фикс».
- **L4-снапшоты по дизайну каждые 4 недели** (`settings.l4_full_snapshot_interval_weeks=4`). Если `last_l4_snapshot` ≤ 4 недель назад и hash не менялся → skip корректен. Не паника.
- **HMAC-хеширование agent_keys включается флагом** `COGCORE_KEY_LOOKUP_SECRET`. Без env — старое поведение (plaintext lookup). С env — dual-path (HMAC OR plaintext) для миграционного периода. Инфра в `app/security/key_hash.py`, миграция `0018`, backfill `scripts/backfill_agent_key_hmac.py`. Если не задано — никакого Argon2/HMAC-логики не активируется.
- **Subprocess `docker exec printenv POSTGRES_PASSWORD` (`scripts/cognitive-rooms.py:~98`) — документированный 3-й fallback** после `env` → `.env`. Срабатывает только когда первые два пусты. Это корректное поведение, не security-risk.
- **`agent_keys` cache TTL 60s** в `mcp_protocol.py:_KEYS_CACHE` — только для env-JSON. DB-ключи не кешируются, revoke мгновенный.
- **CLAUDE.md «56 тестов» — устарело**, реально 388 функций в 33 файлах. Цифра не контракт.
- **Кириллические `agent_id`** (например, `сервер_память`) корректно работают с MCP room-вызовами после фикса `8449f64`. Если в логах виден `UnicodeEncodeError: 'ascii' codec` на `X-Agent-Id` — значит, прод не обновлён.

## Запущенные тесты (не чини что не сломано)

Все проверки в `test_api.py` используют домены `test_api`, `test_bulk` — не удаляй эти данные.
Daily/Weekly/Monthly тесты вызывают реальный LLM (DeepSeek) — нужно 60 сек таймаут.

> Заметка по числу тестов: CLAUDE.md ранее упоминал «56 тестов» — на 2026-06-14
> фактически 388 функций в 33 файлах (`pytest --collect-only`). Цифру в команде
> сборки оставил как было — это не контракт, просто ориентир.

## Deploy runbook для серверного агента (`mcp.me-ai.ru`)

Ветка `claude/festive-newton-WhrlH` содержит аудит-фиксы за 2026-06-14
(4 раунда: `8449f64` → `a66c4d5` → `dae2a0e` → `ca0e5aa` + последний).

### Безопасный деплой кода (без миграций, без env)

```bash
cd /opt/cognitive-core
git fetch origin claude/festive-newton-WhrlH
git merge --ff-only origin/claude/festive-newton-WhrlH   # или merge в main и pull
systemctl restart cognitive-api                          # ~5 сек downtime
# rooms (если в отдельном systemd-юните):
systemctl restart cognitive-rooms || true

# Проверка после рестарта:
curl -s https://mcp.me-ai.ru/health | jq '{healthy, embedding, llm_circuit_breakers}'
```

Все фиксы обратно-совместимы:
- `CORS_ORIGINS_CSV` пуст → `*` (как было).
- `ROOMS_ADMIN_KEY` пуст → теперь `503` вместо тихой утечки (это **намеренно**).
- `COGCORE_KEY_LOOKUP_SECRET` пуст → plaintext-lookup как раньше.

### Опциональный шаг: включить HMAC-lookup для API-ключей

Закрывает риск plaintext-ключей в `agent_keys` при дампе/SQLi.

```bash
# 1) сгенерировать секрет (≥ 32 байт случайных)
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
# → положить значение в /opt/cognitive-core/.env как
#   COGCORE_KEY_LOOKUP_SECRET=<тот-самый-секрет>
#   (Не ротировать без полной переуэйчи ключей.)

# 2) применить alembic-миграцию 0018 (добавляет колонку + индекс)
docker exec cognitive_api alembic upgrade head

# 3) backfill активных ключей (idempotent, можно повторять)
docker exec -e DATABASE_URL=$DATABASE_URL cognitive_api \
    python scripts/backfill_agent_key_hmac.py --dry-run     # сначала dry
docker exec -e DATABASE_URL=$DATABASE_URL cognitive_api \
    python scripts/backfill_agent_key_hmac.py               # потом боевой

# 4) рестарт чтобы verify_api_key подхватил env-переменную
systemctl restart cognitive-api

# Проверка: успешный логин через старый и новый путь (curl с любым валидным
# X-API-Key) — оба должны вернуть 200. После backfill оба пути активны
# (dual-lookup), DROP старой колонки — отдельная будущая миграция.
```

### Откатить (если что-то сломалось)

```bash
cd /opt/cognitive-core
git checkout main      # или previous-good-tag
systemctl restart cognitive-api
docker exec cognitive_api alembic downgrade -1   # только если 0018 уже применена
```

### Что НЕ сделано в этой ветке (требует отдельной работы)

- **DROP колонки `api_key`** (после полного успешного backfill и confidence-периода) — отдельная миграция, не в этой ветке.
- **rooms async refactor** — оказался не нужен: `scripts/cognitive-rooms.py:3010` уже использует `ThreadingHTTPServer` с `_PG_CONN_LOCK`. Bottleneck в аудите был переоценкой.
- **rooms tests с фикстурами** — отдельный эпик.
