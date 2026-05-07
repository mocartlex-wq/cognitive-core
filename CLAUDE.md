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

## Запущенные тесты (не чини что не сломано)

Все проверки в `test_api.py` используют домены `test_api`, `test_bulk` — не удаляй эти данные.
Daily/Weekly/Monthly тесты вызывают реальный LLM (DeepSeek) — нужно 60 сек таймаут.
