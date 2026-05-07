# Cognitive Core vs alternatives

> Детальное сравнение по техническим характеристикам.
> Если нашли неточность — открывайте PR с источником.

## TL;DR таблица

| | **Cognitive Core** | Mem0 | Letta | Zep | OpenAI Memory |
|---|---|---|---|---|---|
| Self-hosted одной командой | ✅ docker compose up | ✅ | ⚠️ heavy | ⚠️ paid for prod | ❌ |
| Multi-layer consolidation | ✅ **5 слоёв L1-L4 + OP** | ❌ flat | ❌ flat | ❌ graph | ❌ |
| AI-куратор как quality gate | ✅ температура 0.1 | ❌ | ❌ | ❌ | ❌ |
| Per-agent state checkpoint | ✅ recovery + history | ❌ | ✅ memory blocks | ❌ | ❌ |
| Audit log L5 (compliance) | ✅ append-only | ❌ | ❌ | ❌ | ❌ |
| Snapshot + verifiable restore | ✅ SHA-256 | ❌ | ❌ | ❌ | ❌ |
| Multi-language prompts | ✅ **8 языков** | ❌ EN | ❌ EN | ❌ EN | ❌ EN/zh |
| MCP server | ✅ stdio + SSE | ⚠️ partial | ❌ | ❌ | ❌ |
| pgvector + HNSW | ✅ | ⚠️ optional | ⚠️ Postgres | ❌ кастом | — |
| Production install за 10 мин | ✅ install-server.sh | ❌ manual | ❌ | ❌ | — |

## Архитектурные различия

### Mem0 — flat memory + tagging

**Подход:** «add(), search()» через vector store. Tags + metadata.

**Сильное:**
- Простота API: 3 метода
- 30k+ stars, большое community
- MCP-сервер из коробки

**Слабое vs Cognitive Core:**
- Нет консолидации — каждое событие = отдельная запись (шум растёт)
- Нет audit log — невозможно для регулируемых индустрий
- Нет «memory layers» — паттерны и raw events лежат рядом
- Нет per-agent state recovery

```
Mem0:           Event ──► Vector ──► Search

Cognitive Core: Event ──► Curator ──► Daily ──► Weekly ──► L3 ──► KNN
                              ↓
                         filtered noise
```

### Letta (MemGPT) — agent OS-метафора

**Подход:** агент с persistent state через "memory blocks". OS-style paging.

**Сильное:**
- Per-agent persistence работает прозрачно
- Архитектура inspired by operating systems

**Слабое vs Cognitive Core:**
- Heavy: требует свой runtime, не только memory
- Сложен для интеграции (нельзя просто `pip install` и пользоваться)
- Нет audit log
- Нет multi-layer consolidation
- Нет self-hosted production deployment automation

### Zep — temporal knowledge graph (Graphiti)

**Подход:** Knowledge graph с bi-temporal queries («что я знал на дату X»).

**Сильное:**
- Лучшее в мире решение для temporal queries
- Graph queries для сложных связей

**Слабое vs Cognitive Core:**
- Платный для production
- Тяжелый stack (Neo4j-like)
- Нет multi-layer consolidation
- Нет AI-куратора — graph учится сам, шум растёт
- Vendor-lean (paid SaaS пушится)

### OpenAI Memory — proprietary

**Подход:** Built-in memory в ChatGPT. Zero-config UX.

**Сильное:**
- Идеально для consumer-сценария

**Слабое vs Cognitive Core:**
- Только внутри ChatGPT
- Не для агентов
- Нет данных контроля (data sovereignty)
- Закрытый исходный код

### Cognee — knowledge graph + ECL

**Подход:** Extract-Cognify-Load pipeline + graph.

**Сильное:**
- Современная архитектура

**Слабое vs Cognitive Core:**
- Молодой (~3k stars)
- Мало adapters
- Нет explicit consolidation cycles

## Сравнение API простоты

### Mem0

```python
from mem0 import Memory
m = Memory()
m.add("Я люблю кофе", user_id="user1")
results = m.search("что я люблю", user_id="user1")
```

### Cognitive Core

```python
from cognitive import AsyncMemoryClient

async with AsyncMemoryClient(...) as memory:
    # Записать
    await memory.remember(
        domain="preferences",
        payload={"task": "обсуждение напитков",
                 "result": "люблю кофе",
                 "feedback": "positive"}
    )

    # Найти структурированный пакет
    results = await memory.recall(
        domain="preferences",
        context="что я люблю",
        grouped=True  # patterns/mistakes/rules/tools отдельно
    )
```

**Mem0 проще для toy-проекта.** Cognitive Core структурированнее для production где важна schema + audit.

## Сравнение recall качества

⚠️ **На 2026-05** независимый бенчмарк не опубликован. Планируется в v0.7.

Methodology (planned):
- **Dataset**: FineMemBench (memory recall standard)
- **Metrics**: recall@5, recall@10, NDCG@10
- **Baseline**: BM25 (текстовый поиск)
- **Comparators**: Mem0 default, Zep с graph, Cognitive Core с/без consolidation

См. `scripts/benchmark.py` (в работе).

## Сравнение latency

Локальные тесты Cognitive Core (см. `scripts/stress_test.py`):

| Operation | p50 | p95 | p99 |
|---|---|---|---|
| POST /events (с sanitize, audit) | 48ms | 111ms | 131ms |
| POST /operative/query (KNN) | 140ms | 188ms | 192ms |
| Daily consolidation (DeepSeek call) | ~10s | ~30s | — |
| Weekly consolidation | ~20s | ~45s | — |

Mem0 / Zep — данные сравнимы по KNN latency (~100-300ms p95), но нет публичных бенчмарков с deep consolidation.

## Сравнение стоимости

### Cognitive Core (self-hosted)

- **Infrastructure**: $20-40/мес (4 CPU / 16 GB VPS)
- **DeepSeek API**: ~$1-3/мес для small use (1000 events/день)
- **Total**: ~$25-45/мес

### Mem0 cloud

- **Free tier**: ограничения
- **Production**: $99-499/мес

### Zep cloud

- **Free tier**: 1k нод
- **Production**: от $200/мес

### OpenAI Memory

- Включено в ChatGPT Plus ($20/мес)
- Не для агентов

**Cognitive Core самый дешёвый** для production использования, потому что self-hosted и DeepSeek дешевле OpenAI.

## Когда **не** выбирать Cognitive Core

Будьте честны:

| Случай | Лучше использовать |
|---|---|
| Вы строите toy-проект на пару дней | **Mem0** (проще API) |
| Вам нужен temporal graph («что я знал на дату X») | **Zep** (лучше в этом) |
| Вы хотите memory только в ChatGPT | **OpenAI Memory** (zero-config) |
| Вам нужен полноценный agent runtime, не только memory | **Letta** (built-in agent OS) |

## Когда **выбрать** Cognitive Core

| Случай | Почему |
|---|---|
| Self-hosted обязательно (compliance) | Один из немногих с production-ready deploy |
| Multi-language (ru/zh/ar/...) | Уникально на рынке |
| Audit log L5 для регуляторов | Уникально |
| Multiple agents с общей памятью | Domain isolation + общая консолидация |
| Хотите structured knowledge (не просто vector) | Multi-layer + curator |

## Когда **смешать**

Можно использовать **Cognitive Core + Mem0** или **+ Zep**:
- Cognitive Core для structured memory с audit
- Mem0/Zep для специфических сценариев (graph, simple)

Cognitive Core REST API совместим с другими — через `POST /events` можно писать из любого места, через `cognitive_recall` читать в свой код.

## Версии источников

- Mem0: 0.1.x, 30k+ stars (2026-05)
- Letta: 0.5.x, 15k+ stars
- Zep: 1.x, ~5k stars
- OpenAI Memory: built-in 2024-2026
- Cognitive Core: 0.5.0-rc2

## Update policy

Этот документ обновляется при:
- Новой major release любого конкурента
- Каждые 6 месяцев минимум
- При нахождении неточности (PR welcome!)
