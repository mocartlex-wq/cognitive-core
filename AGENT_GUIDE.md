# Cognitive Core — Agent Integration Guide

> Как ваш AI-агент работает с Cognitive Core: запрашивает знания, использует, возвращает опыт.

## Концепция в одной картинке

```
[Агент]                                      [Cognitive Core]
   │                                              │
   │ 1. POST /operative/query?grouped=true       │
   │    {domain:"design", context:"лендинг"}     │
   ├─────────────────────────────────────────────►│
   │                                              │ KNN по L3 +
   │                                              │ группировка
   │   {session_id, frame:{                       │
   │      patterns: [...],                        │
   │      mistakes: [...],                        │
   │      rules:    [...],                        │
   │      tools:    [...]                         │
   │   }}                                         │
   │◄─────────────────────────────────────────────┤
   │                                              │
   │ 2. Работает с frame:                         │
   │    - читает patterns                         │
   │    - использует tools                        │
   │    - избегает mistakes                       │
   │    - следует rules                           │
   │                                              │
   │ 3. POST /operative/sessions/{id}/close       │
   │    {keep_results:true,                       │
   │     results_summary:{                        │
   │       task, result, feedback, lessons,       │
   │       tools_used                             │
   │     }}                                       │
   ├─────────────────────────────────────────────►│
   │                                              │ Обратная петля:
   │                                              │ создаётся новое L1
   │                                              │ событие из summary
   │   {status:"closed", kept_results:true}       │
   │◄─────────────────────────────────────────────┤
   │                                              │
   │                              Через сутки:    │
   │                              daily L1→L2     │
   │                              Через неделю:   │
   │                              weekly L2→L3    │
   │                                              │
   │ 4. Следующий агент делает query               │
   │    на ту же тему — получает обновлённые     │
   │    знания с учётом нашего опыта             │
   ▼                                              │
```

## Три способа подключения

### A. REST API напрямую (любой язык)

```bash
# 1. Запрос памяти по разделам
curl -X POST http://localhost:9001/operative/query?grouped=true \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{"domain":"design","context":"лендинг продукта","top_k":5}'

# Response:
# {
#   "session_id": "uuid",
#   "domain": "design",
#   "expires_in": 86400,
#   "frame": {
#     "patterns": [{"id":"...","content":{...},"distance":0.23}, ...],
#     "mistakes": [...],
#     "rules":    [...],
#     "tools":    [{"id":"...","tool_name":"figma-api",...}, ...],
#     "all":      [...]
#   },
#   "counts": {"patterns":3,"mistakes":1,"rules":1,"tools":2,"total":7}
# }

# 2. После работы — обратная петля
curl -X POST http://localhost:9001/operative/sessions/{session_id}/close \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{
    "keep_results": true,
    "source_agent": "agent_designer",
    "results_summary": {
      "task": "лендинг продукта Y",
      "result": "сделал, метрики +15% conversion",
      "feedback": "positive",
      "lessons": "primary CTA выше fold даёт лучший CTR",
      "tools_used": ["figma-api"]
    }
  }'
```

### B. Python SDK (cognitive-client)

```python
from cognitive import AsyncMemoryClient

async def design_agent(brief: str):
    async with AsyncMemoryClient(
        base_url="http://localhost:9001",
        api_key="key-design-001",
        agent_name="agent_designer",
    ) as memory:
        # Запрос памяти (с группировкой)
        async with memory.session(
            domain="design",
            context=brief,
            top_k=5,
            grouped=True,
        ) as op:
            patterns = op.frame["patterns"]
            mistakes = op.frame["mistakes"]
            rules    = op.frame["rules"]
            tools    = op.frame["tools"]

            # Логика агента: использует знания + инструменты
            result = await my_design_logic(brief, patterns, mistakes, rules, tools)

            # Обратная петля
            await op.close(
                keep_results=True,
                results_summary={
                    "task": brief,
                    "result": result.summary,
                    "feedback": "positive" if result.success else "negative",
                    "lessons": result.lessons,
                    "tools_used": [t["tool_name"] for t in tools if t["used"]],
                }
            )

        return result
```

### C. MCP сервер (Claude Desktop / Cursor / Code)

Уже доступно через `mcp_server/`. Установка — [`mcp_server/QUICKSTART.md`](mcp_server/QUICKSTART.md).

В чате Claude:
> Используй cognitive_recall с domain="design" context="лендинг продукта"
> [Claude получает frame, делает дизайн]
> Запиши результат в память через cognitive_remember

## Структура frame для агента

| Раздел | Что содержит | Когда использовать |
|---|---|---|
| `patterns` | Подтверждённые приёмы что работает | Начало работы — прямые рекомендации |
| `mistakes` | Известные ошибки чего избегать | Перед действием — проверка не попадаешь ли в ловушку |
| `rules` | Жёсткие правила | Constraint checking — что нельзя нарушить |
| `tools` | Инструменты с описанием когда применять | Выбор инструмента под задачу |
| `all` | Плоский список всего | Совместимость со старым кодом |

Каждый item в разделе содержит:
- `id` — уникальный ID знания/инструмента
- `domain` — предметная область
- `distance` — семантическая близость (0=точно, 1=далеко)
- `confidence` — уверенность куратора
- `content` или `tool_name` + `usage` — содержимое

## Лучшие практики для агентов

### 1. Один query на задачу — не дёргайте память постоянно
```python
# ХОРОШО
async with memory.session(domain, brief) as op:
    do_all_work_with(op.frame)

# ПЛОХО — каждый шаг = новый KNN
result1 = await memory.recall("step1")
result2 = await memory.recall("step2")  # лишний LLM-вызов
```

### 2. Всегда возвращайте опыт через obratnuyu петлю
```python
# Без keep_results=true система не учится
await op.close(
    keep_results=True,  # ← важно
    results_summary={...detailed...},  # ← с фидбеком
)
```

### 3. Используйте feedback по конкретной записи если что-то особенно сработало или нет
```python
await op.feedback(record_id="...", record_type="tool", useful=True)
# → confidence этого инструмента в OP повышается
```

### 4. Грамотно выбирайте domain
- ❌ `domain="general"` для всего — теряется специфичность
- ✅ Специализированные: `design`, `coding_python`, `bugfix_log`, `ops_aws`
- Минимум 3 события в день в одном домене → daily-консолидация заработает

### 5. Payload должен быть структурным
```python
# ХОРОШО
{"task": "...", "result": "...", "feedback": "positive", "lessons": "..."}

# ПЛОХО  
{"text": "сделал то-то"}  # куратор не сможет извлечь паттерн
```

## Работа на принципах живой памяти

Cognitive Core — не просто vector DB. Между запросом и сохранением:

1. **Перед записью в L1** — sanitizer + rate-limit + validation
2. **Раз в день**: куратор-фильтр выкидывает шум, daily-analyzer LLM создаёт L2 буфер
3. **Раз в неделю**: pre-weekly-check проверяет повторяемость (≥2 раз) и confidence (≥0.6), weekly-consolidator LLM синтезирует L3
4. **Раз в месяц**: monthly-audit удаляет stale (>90 дней) и dead tools (>60 дней)
5. **При каждом query**: KNN через RediSearch (быстро) или pgvector (если Redis пуст)

То есть знания, попадающие в `frame`, — это **проверенный временем опыт**, а не сырые события.

## Метрики использования (для мониторинга)

Откройте `/ui` → вкладка «Аудит L5» — увидите все вызовы агента с timestamp, success/error.

```bash
# Агентская активность за день
curl http://localhost:9001/dashboard/audit-tail?limit=200 | \
  jq '.items[] | select(.action=="operative_query")'

# Какие домены чаще всего запрашивают
curl http://localhost:9001/dashboard/domains
```

## Persistent agent state — recovery после срыва сессии

Агент может **сохранять своё рабочее состояние** и **восстанавливать его в новом сеансе**. Это решает классическую проблему: токены закончились / сессия прервалась → агент в новом чате начинает с нуля.

### MCP инструменты

| Tool | Когда использовать |
|---|---|
| `cognitive_save_state` | Периодически или после важного шага — сохранить current_task + state_data + active_session_ids |
| `cognitive_continue` | В начале нового сеанса — «где я остановился?». Возвращает last task + recent events + recent knowledge |
| `cognitive_my_history` | Посмотреть свои последние N checkpoints (можно откатиться) |

### Best practice (из DeepSeek-консультации)

Гибридная стратегия checkpoint:
1. **Manual** — после каждого важного шага агент явно вызывает `cognitive_save_state`
2. **Auto** — каждые ~5 минут (если делает auto-tooling)
3. **Heartbeat** — keep-alive в долгих задачах
4. **Session_close** — после `operative.close` автоматически
5. **Event_milestone** — после крупных изменений

### Пример полного цикла с recovery

```python
# Сеанс 1 (агент работает над задачей)
async with AsyncMemoryClient(...) as memory:
    # Restore state from previous session (если есть)
    state = await memory.continue_state()
    if state["exists"]:
        plan = state["state_data"].get("plan", [])
        step = state["state_data"].get("step", 0)
        print(f"Continuing: {state['current_task']}, step {step}")
    else:
        plan = ["analyze", "design", "code", "test"]
        step = 0

    # Working loop
    while step < len(plan):
        do_step(plan[step])
        step += 1

        # Checkpoint после каждого шага
        await memory.save_state(
            current_task="разработка лендинга",
            state_data={"plan": plan, "step": step, "context": gathered_data},
            trigger="event_milestone",
        )

# === Срыв сессии: токены кончились / OOM / disconnect ===

# Сеанс 2 (новый чат)
async with AsyncMemoryClient(...) as memory:
    state = await memory.continue_state()
    # state["current_task"] = "разработка лендинга"
    # state["state_data"] = {"plan": [...], "step": 3, "context": {...}}
    # state["recent_events"] = [last 10 L1 events]
    # state["recent_knowledge"] = [5 L3 entries from active domains]
    # → продолжаем с шага 3 без потерь
```

### REST API

```bash
# Save checkpoint
curl -X POST http://localhost:9001/agents/my_agent/checkpoint \
  -H "X-API-Key: key-design-001" -H "Content-Type: application/json" \
  -d '{
    "current_task": "research market data",
    "state_data": {"phase": 2, "collected_sources": ["x.com", "y.com"]},
    "active_session_ids": ["uuid-from-operative-query"],
    "notes": "выходные — продолжить в понедельник",
    "trigger": "manual"
  }'

# Restore state
curl -H "X-API-Key: key-design-001" \
  "http://localhost:9001/agents/my_agent/state?recent_events=20&recent_knowledge=10"

# History (откат)
curl -H "X-API-Key: key-design-001" \
  "http://localhost:9001/agents/my_agent/history?limit=20"

# Все активные агенты (для дашборда)
curl -H "X-API-Key: key-design-001" "http://localhost:9001/agents"
```

### Лимиты и безопасность

- `state_data` ≤ **256 KB** (sanitized, как L1 payload — SQL/JS/XSS защита)
- `current_task` ≤ 2000 chars
- `notes` ≤ 500 chars
- История checkpoints не удаляется автоматически (можно для отката)

### Связь с другими слоями

| Что | Куда |
|---|---|
| Checkpoint state | `agent_states` table (Postgres) |
| История | `agent_state_history` table |
| Recent events для restore | L1 (`l1_raw_events` WHERE source_agent=?) |
| Recent knowledge для restore | L3 (`l3_master_knowledge` from active domains) |
| Все вызовы | L5 audit-log (action='checkpoint_save' etc.) |

Это **per-agent persistence layer**, дополняющий existing L1-L5 + OP. Не замещает их — расширяет.

## Часто задаваемые вопросы

### Что если агент запросил тему, по которой пока нет знаний?
Вернётся пустой frame (`patterns: []`, `tools: []`). Агент должен работать без памяти и записать новый опыт через обратную петлю — он создаст первые knowledge для следующих агентов.

### Сколько живёт session?
24 часа в Redis. Если агент не успел закрыть — сессия удаляется автоматически (опыт теряется).

### Можно ли подключить несколько агентов одновременно?
Да. Каждый со своим X-API-Key. Все события идут в общую L1 с пометкой `source_agent`.

### Как изолировать домены между агентами?
Domain-based isolation: `agent_a` пишет в `domain_a`, `agent_b` в `domain_b`. KNN-поиск ограничен своим доменом.

### Куда пожаловаться если что-то не так?
Сначала: `/ui` → «Аудит L5» → фильтр «Только ошибки». Если что-то непонятно — записать в `dogfooding/friction.md` и разобрать на weekly review.
