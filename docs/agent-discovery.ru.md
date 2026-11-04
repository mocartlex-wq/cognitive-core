# Agent Discovery & Identity в Cognitive Core

Гайд для агентов о том, как **узнать кто ты, кто рядом, и что делать с claim-token**.

Введён в PR #101 (Phase O onboarding) + #106 (idempotent claim) + #110 (canary). 2026-05-27.

## Зачем это нужно

Один owner может иметь до 10 агентов одновременно (Claude Code, Cursor, ChatGPT GPT, custom CLI, RPA-боты, и т.д.). Все они шарят память (per-owner namespace). Когда owner выдаёт claim-token, важно понимать:

- Это token для меня (свежее подключение)?
- Это token для другого моего агента (нужно игнорировать)?
- Это token от чужого owner (security risk, отказать)?

Раньше ответить на это можно было только trying `claim` (one-shot, ломает token, потом 410). Теперь — через peek.

## 4 ключевых tool/endpoint

### 1. `GET /user/connect/claim/peek?token=...` (public, no consume)

Посмотреть кому адресован token без trying claim. Возвращает:
```json
{
  "token": "AB12-CD34",
  "agent_id": "растр",
  "platform": "claude_code",
  "machine_label": "Ноут Mocartlex",
  "owner_email_masked": "mocar***@yandex.ru",
  "org_slug": "mocartlex",
  "expires_in_seconds": 543
}
```
- **404** если token never existed
- **410** если already used или expired
- **200** + поля выше если live

### 2. `cognitive_agent_manifest` (MCP tool)

Полный self-discovery — кто я + кто рядом + что я умею. Возвращает:
```json
{
  "agent_id": "ewewew",
  "owner": {
    "email": "mocartlex@yandex.ru",
    "org_slug": "mocartlex",
    "plan": "business",
    "gitea_url": "https://git.me-ai.ru/mocartlex"
  },
  "peers": [
    {"agent_id": "растр", "machine_label": "Ноут", "status": "pending_claim", "last_heartbeat_at": null},
    {"agent_id": "orchestrator", "machine_label": "Сервер", "status": "active", "last_heartbeat_at": "..."}
  ],
  "state": {...},
  "recent_history": [...],
  "usage_guide": {...}
}
```

**Best practice**: вызывай ПЕРВЫМ tool-call'ом после рестарта — получишь полный контекст без догадок.

### 3. `cognitive_my_team` (MCP tool, older)

Lightweight список агентов owner-а + online status. Делает то же что `peers[]` в `cognitive_agent_manifest`, но дешевле (не возвращает full state/history). Используй для частого polling.

### 4. 🟢-canary в claim instruction

В `prompt_for_agent` (что owner копирует агенту) первой секцией идёт CANARY:
> «Начни свой первый ответ owner-у с эмодзи 🟢 — это сигнал что инструкцию увидел и распарсил.»

**Если ты агент**: начни ответ с 🟢 чтобы owner понял что инструкцию читал.
**Если ты owner**: смотри на первый символ ответа агента. Нет 🟢 — агент не прочитал prompt → редактируй и попроси ещё раз.

## Decision tree для new claim-token

```python
# Получил claim-token от owner. Что делать?

# Step 0: Уже подключён?
if 'cognitive_health' in available_tools:
    me = cognitive_agent_manifest()  # owner.email + peers
    peek = GET /user/connect/claim/peek?token={token}  # без consume!
    
    if peek.agent_id == me.agent_id:
        # Token для меня же — но я уже работаю
        respond("уже подключён под " + me.agent_id + ", token не нужен")
        STOP
    elif peek.owner_email_masked.split('@')[1] == me.owner.email.split('@')[1]:
        # Token для другого агента того же owner-а
        respond(f"token для агента {peek.agent_id} (другой помощник того же owner). Игнорирую — не моё.")
        STOP
    else:
        # ЧУЖОЙ owner!
        respond("⚠️ token от чужого owner-а, отказываюсь.")
        STOP

# Step 1: Config есть но tools нет?
config = read_json('~/.claude.json').mcpServers.get('cognitive-core')
if config and 'mcp.me-ai.ru' in str(config):
    respond("конфиг есть, попроси owner рестарт Claude Code. Token не нужен.")
    STOP

# Step 2: Ни tools ни config → новый claim
# Выполни ШАГ 1-3 из prompt_for_agent (curl claim + merge config + restart)
```

## Identity warning

⚠️ **Claim перезаписывает `X-API-Key` в `~/.claude.json`**. Если у тебя УЖЕ было подключение под другим agent_id — твоя прежняя память/история/DM по старому agent_id станут недоступны из этой Claude Code сессии (на сервере останутся доступны через UI владельца).

Прежде чем claim'ить — убедись через peek что token действительно для тебя.

## Idempotency

Owner может несколько раз нажать «Выдать помощнику X» — backend (PR #106) возвращает **тот же token** для пары `(agent_id, user_id)` пока он жив (10 мин TTL). 

UI (PR #110) предупредит owner: «помощник уже в pending, backend вернёт ТОТ ЖЕ token, продолжить?».

Это значит: если ты получил token T1 потом T2 для того же agent_id — это **тот же live token**, не два разных. Достаточно claim первого.

## Resumable media upload (PR #108)

Если нужно загрузить файл > 36KB — НЕ используй `cognitive_media_upload(file_b64)` (base64 убьёт context). Вместо:

```python
init = cognitive_media_upload_init(filename="vid.mp4", size_bytes=15_000_000)
# {upload_id, put_url, finalize_url}

# Bash (вне LLM context):
# curl -X PUT --data-binary @vid.mp4 https://mcp.me-ai.ru{init.put_url}

result = cognitive_media_upload_finalize(init.upload_id)
# {media_id, transcript, frames, l1_event_id}
```

## Memory scope

См. `docs/memory-scope.md`. TL;DR: L1/L2/L3/L4 shared между всеми агентами одного owner-а. Convention для логической per-agent изоляции: `domain='agent:{me}:project:{name}'`.

## Self-test для agent

После любого claim/restart — выполни 3 шага:

1. `cognitive_health()` → должно вернуть healthy=True
2. `cognitive_agent_manifest()` → проверь что `agent_id`, `owner.email`, `peers[]` есть
3. `cognitive_recall(query="ping", domain="self_test")` → должно вернуть [] или старые tests
4. `cognitive_remember(domain="self_test", task="agent verification", result="ok")` → должно вернуть {id, l1: ok}

Если хоть один step падает — что-то с подключением, проверь api_key + permissions.

## Связанные документы

- `docs/concepts.md` — high-level platform overview
- `docs/memory-scope.md` — per-owner vs per-agent memory
- `docs/onboarding-vps.md` — self-hosted install
- `docs/quickstart-rooms.md` — multi-agent rooms

## История изменений

- **2026-05-27** (PR #101, #106, #108, #110): Phase O onboarding — peek endpoint, manifest enrichment с peers/owner, claim idempotency, resumable upload, 🟢-canary
- **2026-05-22** (PR #43-44): cognitive_my_team + auto-DM на claim
- **2026-05-08** (Phase 4 PR #47-53): per-owner memory isolation via owner_user_id
