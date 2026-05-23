# Fast Shared Memory — L0 layer (Redis)

> Realtime координационная память для AI-агентов: presence, scratchpad, project state, locks, pub/sub. Ephemeral by default. Дополняет (не заменяет) 5-слойную consolidation-memory.

## Зачем отдельный layer

5-слойная память Cognitive Core — для **знаний** и **истории**:
- L1 raw events → L2 daily → L3 master knowledge → L4 snapshots → L5 audit
- Цикл консолидации: **сутки/неделя/месяц** — slow, but durable & semantic

Fast-memory — для **сиюминутной координации**:
- «Я сейчас правлю nginx.conf — не трогайте»
- «Релиз v0.5.0 идёт build, ждите 30 сек»
- «Билд упал, причина в conditional_reload.sh:55»

Это **ephemeral**. TTL и size cap. Если агент упадёт — данные пропадут (это OK для координации, не OK для знаний).

---

## L0 примитивы (5 ключей в Redis)

### 1. Project Blackboard — общее состояние проекта

Redis hash `project:<name>:state`:

```
HSET project:ai-crm:state current_branch "feat/payment"
HSET project:ai-crm:state last_deploy_sha "08576bf"
HSET project:ai-crm:state test_status "passing"
HSET project:ai-crm:state last_blocker ""
HSET project:ai-crm:state phase "v0.5-rc"

EXPIRE project:ai-crm:state 86400  # 24h TTL — рефрешится при каждом write
```

Любой агент проекта читает: `HGETALL project:ai-crm:state`.

**Что класть:** статус-флаги, current pointers (branch, last commit, current task), флаги-заметки. Размер каждого значения ≤ 1KB.

### 2. Realtime presence — кто online

Redis hash `presence:agent:<agent_id>` с **TTL 60 сек**:

```
HSET presence:agent:claude-cogcore-laptop project "cognitive-core"
HSET presence:agent:claude-cogcore-laptop machine "LpTop"
HSET presence:agent:claude-cogcore-laptop current_task "fixing nginx mcp messages 404"
HSET presence:agent:claude-cogcore-laptop started_at "2026-05-07T06:30:00Z"
EXPIRE presence:agent:claude-cogcore-laptop 60
```

Агент пингует **каждые 30 сек** (TTL 60 — двойной запас). Если пропустил 2 пинга подряд — Redis сам выкинет ключ.

`KEYS presence:agent:*` → список online агентов.
`KEYS presence:agent:* | filter project="ai-crm"` → агенты конкретного проекта.

### 3. Project scratchpad — короткий чат для координации

Redis LIST `project:<name>:chat`:

```
LPUSH project:ai-crm:chat "[2026-05-07T12:30:00Z][claude-cogcore-laptop] starting work on nginx fix"
LTRIM project:ai-crm:chat 0 999  # capped at 1000 entries
EXPIRE project:ai-crm:chat 604800  # 7-day TTL
```

`LRANGE project:ai-crm:chat 0 49` → последние 50 сообщений.

**Не для долгосрочного хранения!** Если хочешь чтобы запись пережила неделю — `POST /events` в L1.

### 4. Coordination locks — взять ресурс

Redis SETNX с TTL:

```
SET lock:ai-crm:resource:nginx.conf "claude-cogcore-laptop" NX EX 300
# → "OK" если занят успешно, (nil) если уже занят
```

Атомарная проверка-и-установка. TTL 300 сек = 5 минут (если агент упал, lock сам отпустится).

Снять лок: `DEL lock:ai-crm:resource:nginx.conf` (только если ты держишь).

Кто держит: `GET lock:ai-crm:resource:nginx.conf`.

### 5. Pub/Sub channels — push-уведомления

Redis PUBSUB `project:<name>:events`:

```
# subscriber:
SUBSCRIBE project:ai-crm:events

# publisher:
PUBLISH project:ai-crm:events '{"type":"deploy_complete","sha":"08576bf","by":"claude-cogcore-laptop"}'
```

Агент подписан на свой проект и/или на конкретные topics. Получает push мгновенно.

**Не persistent** — если subscriber не online в момент publish, сообщение пропадает. Для гарантированной доставки — `POST /events` (L1).

---

## Правило разграничения: Fast vs L1

| Признак | → Куда |
|---|---|
| Ephemeral status, координация, presence | **Fast (Redis)** |
| Решение принято, нужно запомнить | **L1 events** (через `POST /events`) |
| Знание о домене (как правильно делать X) | **L1 events** → консолидируется в **L3** |
| Срочное уведомление другому агенту | **Fast (pub/sub)** + копия в **L1** для durability |
| Чат-сообщения «давай созвонимся» | **Fast scratchpad** |
| Reasoning лога / decision rationale | **L1 events** (домен `decisions`) |
| Текущий current_task агента | **Fast presence** |
| История твоих решений за сегодня | **L1 events** (тоже сами по дню в L2 потом) |

**Правило большого пальца:** если данные нужны **через час и больше** — L1. Если только **прямо сейчас для координации** — Fast.

**Уточнение (DeepSeek-validated):** данные, которые должны **пережить перезапуск Redis** или нужны для **аудита** — *только* в Postgres. Redis — in-memory, durability not guaranteed. Если в задаче «надо точно сохранить» — выбор всегда L1.

---

## Анти-паттерны fast-memory

### Никогда в Redis НЕ класть:

1. **Личные данные пользователя** (PII) — в Redis нет аудита, нет шифрования at rest по умолчанию
2. **Секреты / API keys / пароли** — есть .env и Docker secrets для этого
3. **Большие блоки** (> 100 KB на ключ) — Redis не оптимизирован для blobs, есть MinIO
4. **Долгосрочные знания** — без TTL, забьют память; для знаний — L3
5. **Бизнес-критичные транзакции** — Redis может потерять данные при сбое; для критики — Postgres
6. **Файлы, медиа** — в S3/MinIO
7. **Без TTL** — каждое значение должно иметь время жизни. Иначе мусор копится навсегда.

### Эксплуатационные ограничения:

- Total Redis usage cap: **1 GB** на инстанс (sane default; tune later)
- Каждый ключ ≤ 100 KB
- Pub/sub channels: < 100 на проект
- Не делать `KEYS *` в production — блокирует. Использовать `SCAN` с pattern.

---

## Использование в коде агента

```python
import redis
import json
import time
from datetime import datetime

r = redis.Redis(host='redis', port=6379, decode_responses=True)
AGENT_ID = "claude-ai-crm-laptop"
PROJECT = "ai-crm"

# 1. Heartbeat (вызывать каждые 30 сек в фоне)
def heartbeat(current_task: str):
    key = f"presence:agent:{AGENT_ID}"
    r.hset(key, mapping={
        "project": PROJECT,
        "current_task": current_task,
        "machine": "LpTop",
        "ping_at": datetime.utcnow().isoformat() + "Z",
    })
    r.expire(key, 60)

# 2. Take lock before editing shared file
def take_lock(resource: str, ttl: int = 300) -> bool:
    return bool(r.set(
        f"lock:{PROJECT}:resource:{resource}",
        AGENT_ID, nx=True, ex=ttl
    ))

# 3. Post a coordination message
def post_chat(text: str):
    msg = f"[{datetime.utcnow().isoformat()}Z][{AGENT_ID}] {text}"
    r.lpush(f"project:{PROJECT}:chat", msg)
    r.ltrim(f"project:{PROJECT}:chat", 0, 999)
    r.expire(f"project:{PROJECT}:chat", 604800)

# 4. Update project state
def set_state(key: str, value: str):
    r.hset(f"project:{PROJECT}:state", key, value)
    r.expire(f"project:{PROJECT}:state", 86400)

# 5. Subscribe to events
def listen_events():
    pubsub = r.pubsub()
    pubsub.subscribe(f"project:{PROJECT}:events")
    for msg in pubsub.listen():
        if msg['type'] == 'message':
            data = json.loads(msg['data'])
            handle_event(data)
```

Все эти примитивы будут обёрнуты в `cognitive-client` Python SDK к v0.5.5 — агенты получат `client.heartbeat(task)`, `client.lock(res)`, `client.chat(text)`, `client.state[key]`, `client.events.subscribe()`.

---

## Когда переходить с Fast → L1 (durable)

Сценарий: один агент пишет важное в chat:

```
"[claude-cogcore-laptop] Решили: nginx-фикс через sub_filter, не location override. Аргумент: keep namespace clean. См коммит 9203ab5"
```

Через 7 дней chat исчезнет. Чтобы это решение пережило — параллельно:

```python
# тот же агент дополнительно пишет в L1:
requests.post("https://mcp.me-ai.ru/events", headers=auth, json={
    "domain": "cognitive-core/architecture/decisions",
    "event_type": "decision",
    "payload": {
        "title": "MCP /messages/ через sub_filter",
        "context": "FastMCP отдаёт relative path в SSE init",
        "decision": "Использовать sub_filter в nginx",
        "rejected_alternatives": ["location override без префикса"],
        "rationale": "Keep namespace clean",
        "commit": "9203ab5",
    }
})
```

Это попадёт в L2 после daily-консолидации, в L3 после weekly. Через год можно будет спросить «почему мы так сделали» и L3 ответит.

---

## Метрики

| Метрика | Цель | Метод |
|---|---|---|
| Латентность операций | < 5 ms | Redis OBJECT ENCODING |
| Online agents | tracked | `KEYS presence:agent:*` count |
| Активных проектов | tracked | unique `project` field из presence |
| Использование памяти | < 1 GB | `INFO memory` |
| Hit rate (заметка vs L1) | tracked | сколько событий ушло fast vs durable |

Дашборд для этого появится в v0.5.5 на `/ui/agents` (вкладка «Online»).
