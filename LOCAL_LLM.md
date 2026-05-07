# Local LLM — Hybrid Cloud+Local на RTX 24GB

> Когда можно полностью отказаться от DeepSeek API? **Никогда** — но >70% вызовов мы можем делать локально, оставив cloud только для критичных по качеству задач.

## TL;DR

```bash
# 1. Поставить RTX (24GB), драйверы, container-toolkit (см. DEPLOY-SERVER.md шаг 10)
# 2. Запустить стек с GPU + Ollama
docker compose \
  -f docker-compose.yml \
  -f docker-compose.prod.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.local-llm.yml \
  up -d --build

# 3. Скачать модели (один раз, ~30 GB)
docker exec cognitive_ollama ollama pull deepseek-r1:14b
docker exec cognitive_ollama ollama pull qwen2.5:14b

# 4. Переключить функции в .env
sed -i 's|^LLM_DAILY_ANALYZER=.*|LLM_DAILY_ANALYZER=ollama:deepseek-r1:14b|' .env
docker compose restart api

# 5. Проверить
curl -k https://localhost/health | jq '.embedding.provider'   # CUDA
docker exec cognitive_api curl -s ollama:11434/api/tags | jq  # модели в Ollama
```

## Архитектура: что локально, что cloud

```
┌──────────────────────────────────────────────────────────────┐
│ EVENTS (от агентов)                                          │
│   ↓ POST /events                                             │
│ L1 raw_events (Postgres)                                     │
│   ↓ daily cycle                                              │
│ ╔══════════════════════════════════════════════════╗         │
│ ║ L1→L2 daily consolidation                        ║         │
│ ║ → ollama:deepseek-r1:14b (LOCAL)  ✅              ║         │
│ ║   • Часто (каждые 24ч)                           ║         │
│ ║   • Простая summarization                        ║         │
│ ║   • Q5 квант = 12GB VRAM, latency 2-4s           ║         │
│ ╚══════════════════════════════════════════════════╝         │
│   ↓                                                          │
│ L2 daily_buffers (Postgres)                                  │
│   ↓ weekly cycle                                             │
│ ╔══════════════════════════════════════════════════╗         │
│ ║ L2→L3 weekly consolidation                       ║         │
│ ║ → deepseek-chat (CLOUD)  💰                      ║         │
│ ║   • Редко (раз в неделю)                         ║         │
│ ║   • Сложная агрегация на длинном контексте       ║         │
│ ║   • Quality net поверх локального daily          ║         │
│ ╚══════════════════════════════════════════════════╝         │
│   ↓                                                          │
│ L3 master_knowledge (Postgres + RediSearch vectors)          │
│   ↓ monthly                                                  │
│ ╔══════════════════════════════════════════════════╗         │
│ ║ Curator audit (filter + quality)                 ║         │
│ ║ → deepseek-chat (CLOUD)  💰                      ║         │
│ ║   • Фильтрация ложных знаний — критична точность ║         │
│ ║   • temperature=0.1                              ║         │
│ ╚══════════════════════════════════════════════════╝         │
│                                                              │
│ Embeddings (fastembed)                                       │
│   → MultilingualMiniLM 384-dim (LOCAL CUDA)  ✅              │
│   • На каждый event                                          │
│   • На каждый recall query                                   │
│                                                              │
│ Vision-LLM (v0.7 multimodal, planned)                        │
│   → ollama:qwen2.5vl:7b (LOCAL)  ✅                          │
│   • OCR + image description                                  │
│   • $0/изображение vs $0.003 OpenAI                          │
│                                                              │
│ Whisper (v0.7 voice, planned)                                │
│   → faster-whisper-large-v3-int8 (LOCAL)  ✅                 │
│   • Транскрипция голосовых заметок                           │
│   • $0/час vs $0.36 OpenAI                                   │
└──────────────────────────────────────────────────────────────┘
```

## Decision matrix: что куда

| Функция | Частота | Сложность | Куда | Модель |
|---|---|---|---|---|
| Embedding events | очень часто | простая | **LOCAL** | fastembed CUDA |
| Embedding queries | очень часто | простая | **LOCAL** | fastembed CUDA |
| L1→L2 daily | часто (1×/день/domain) | средняя | **LOCAL** | deepseek-r1:14b Q5 |
| L2→L3 weekly | редко (1×/неделю) | сложная | **CLOUD** | deepseek-chat |
| Curator filter | редко (1×/мес) | критична | **CLOUD** | deepseek-chat |
| Curator quality | редко | критична | **CLOUD** | deepseek-chat |
| Vision OCR (v0.7) | по запросу | средняя | **LOCAL** | qwen2.5vl:7b |
| Whisper voice (v0.7) | по запросу | простая | **LOCAL** | whisper-large-v3 |

## Модели для RU+EN на 24GB

DeepSeek-валидированные рекомендации:

### Daily analyzer (часто, мультиязычный)

| Модель | VRAM Q5 | Pros | Cons |
|---|---|---|---|
| **deepseek-r1:14b** ⭐ | ~12 GB | Reasoning chain, мощный RU+EN | Медленнее (chain-of-thought) |
| qwen2.5:14b | ~10 GB | Очень быстрый, отличный RU | Чуть слабее на reasoning |
| llama3.1:8b | ~6 GB | Самый быстрый, хороший EN | RU средний |

### Vision (v0.7)

| Модель | VRAM | Что делает |
|---|---|---|
| **qwen2.5vl:7b** ⭐ | ~10 GB | OCR + описание сцены, RU+EN |
| llama3.2-vision:11b | ~14 GB | Качественнее, но медленнее |

### Voice (v0.7)

| Модель | VRAM | Latency |
|---|---|---|
| **faster-whisper-large-v3-int8** ⭐ | ~6 GB | 0.1× realtime |
| whisper-medium | ~3 GB | 0.05× realtime, RU слабее |

## Как переключить функцию на локальную модель

```bash
# .env
LOCAL_AI_ENABLED=true
LLM_DAILY_ANALYZER=ollama:deepseek-r1:14b

# Применить
docker compose restart api

# Проверить
docker logs cognitive_api --tail 30 | grep -i daily
# Должны увидеть base_url=http://ollama:11434/v1
```

## Откат если локальная модель плохая

Cognitive Core имеет встроенный **fallback chain** в `app/services/llm_client.py`:

```
primary (ollama)  →  если упала  →  fallback (deepseek)  →  если упала  →  cached response
```

Так что при сбоях Ollama цикл daily не останавливается — переключается на DeepSeek и делает запись в audit log.

## A/B тестирование локальной модели против cloud

```bash
# Прямое сравнение качества: 30% трафика идёт на ollama, 70% на cloud
LLM_DAILY_ANALYZER=deepseek-chat
LLM_DAILY_ANALYZER_B=ollama:deepseek-r1:14b
LLM_AB_TRAFFIC_PERCENT=30

# Через неделю проверить статистику
curl -s -k https://localhost/metrics | grep -E "llm_(success|fail)_total"
# или
docker exec cognitive_api python -c "from app.services.llm_client import get_ab_stats; import json; print(json.dumps(get_ab_stats(), indent=2))"
```

## Экономика гибрида

```
ДО (DeepSeek-only, 1000 events/день):
  • Daily 30 calls/день × $0.0005   = $0.45/мес
  • Weekly 5 calls/нед × $0.003     = $0.06/мес
  • Audit 1 call/мес × $0.005       = $0.005/мес
  • Embeddings 30k/день × $0.00001  = $9.00/мес  (если бы платили)
  ИТОГО: ~$10/мес

ПОСЛЕ (гибрид с RTX):
  • Daily local                     = $0/мес
  • Weekly cloud                    = $0.06/мес  (оставляем)
  • Audit cloud                     = $0.005/мес (оставляем)
  • Embeddings local CUDA           = $0/мес
  ИТОГО: $0.07/мес  +  амортизация RTX 3090 ~$700 / 36 мес = $19.5
  ITOGO REAL: ~$20/мес
```

**Гибрид не для экономии** на маленькой нагрузке — для **приватности**, **offline-работоспособности** и **готовности к multimodal v0.7** где локальный vision/voice экономит реально.

## Risks & mitigations

| Риск | Что сделать |
|---|---|
| Local 14B Q5 хуже cloud → cascade ошибок в L3 | Weekly cloud-pass корректирует. Мониторить через A/B |
| Ollama crash → daily ломается | Auto-fallback на DeepSeek (встроен в LLMClient) |
| VRAM OOM при параллельных запросах | `OLLAMA_MAX_LOADED_MODELS=1` если 24GB маловато |
| Долгий cold-start модели (~30s) | `OLLAMA_KEEP_ALIVE=30m` держит в VRAM |
| Качество quant Q5 хуже Q8 | Если VRAM позволяет — переключить на Q8 (`deepseek-r1:14b-q8_0`) |

## Связанные документы

- [`DEPLOY-SERVER.md`](DEPLOY-SERVER.md) — установка GPU драйверов и container-toolkit
- [`docker-compose.local-llm.yml`](docker-compose.local-llm.yml) — overlay с Ollama
- [`docker-compose.gpu.yml`](docker-compose.gpu.yml) — overlay с CUDA для api
- [`Dockerfile.gpu`](Dockerfile.gpu) — образ api с onnxruntime-gpu
- [`app/services/llm_client.py`](app/services/llm_client.py) — fallback chain
