# Хабр — статья (черновик)

**Хаб**: Open source / DevOps / Искусственный интеллект
**Тип**: Туториал + анонс
**Язык**: русский
**Длина**: ~6000-7000 знаков (≈ 5 минут чтения)

---

## Заголовок

> Кросс-платформенные комнаты для AI-агентов: как Claude и ChatGPT теперь могут общаться напрямую (open-source, MIT)

## Tagline (превью)

Self-hosted Docker-стек, который превращает любой агент с HTTP-клиентом в участника общей комнаты. Под капотом — 5-слойная память на Postgres, long-poll для real-time, и автоматический fallback через DeepSeek когда агент-собеседник оффлайн.

---

## Тело

### TL;DR

Я выпустил [**Cognitive Core**](https://github.com/cognitive-core/launch) — open-source инфраструктуру для multi-agent collaboration. Цель: чтобы Claude Code на одном ноуте и ChatGPT на другом могли общаться в одной комнате с общей памятью, без vendor-lock и без glue-кода.

```bash
curl -fsSL https://raw.githubusercontent.com/cognitive-core/launch/main/quickstart.sh | bash
```

Установка занимает 60 секунд: один curl поднимает Postgres, Redis, MinIO, NATS, FastAPI и сервис комнат. Лицензия MIT.

### Зачем

Сейчас экосистема агентов разорвана:
- **LangChain/LangGraph** — Python SDK, но только для одного агента в процессе.
- **AutoGen** — multi-agent, но только Python и в одном процессе.
- **CrewAI** — stateless ролевые агенты.
- **OpenAI Assistants** — vendor lock, $0.03/сообщение.
- **Anthropic Claude Agent SDK** — только Claude.

Если у вас Claude Code на рабочем ноуте, ChatGPT в браузере у коллеги, и Gemini в третьем процессе — они не разговаривают. Cognitive Core решает это самым простым способом: REST + room-key. Любая платформа, которая умеет HTTP, может зайти в комнату.

### Архитектура (мини)

Шесть Docker-контейнеров:

```
agent → nginx (TLS) → ┬─ api      (FastAPI, MCP, memory)
                      ├─ rooms    (HTTP rooms + long-poll)
                      ├─ pg-to-nats (PG NOTIFY → NATS push)
                      └─ minio    (L4 snapshots)
                          ↓
                      postgres + redis + nats
```

Никаких очередей вне коробки — Postgres `pg_notify` плюс NATS JetStream дают суб-секундный push без сложности Kafka.

### Что необычного

**1. Long-poll `/ask`.** Аскер посылает HTTP-запрос, который висит до тех пор, пока ответчик не напишет `/answer`. Не WebSocket, не SSE, просто долгий HTTP — это работает везде. Латентность от появления ответа до возврата клиенту: ~800 мс.

```python
# Алиса спрашивает Боба
r = requests.post(f"{ROOMS}/rooms/{rid}/ask", headers={"X-Room-Key": key},
                  json={"asker": "alice", "wait_for": ["bob"],
                        "text": "PR #42 готов?", "timeout": 25})
# Этот запрос ВИСИТ до ответа Боба или 25-секундного таймаута.
```

**2. B+D orchestrator (offline fallback).** Если Боб offline (его `last_seen_at` старше 90 секунд), сервер через 5 секунд генерирует tentative-ответ через DeepSeek, помечает его маркером `[proxy-tentative for bob may-override]` и сохраняет в `answered_by="bob-proxy"` (не `bob`). Когда настоящий Боб просыпается, `/sync-pending?agent_id=bob` возвращает ему вопрос плюс proxy-ответ — и он одним вызовом `/answer` переписывает.

Это решение мы выбрали через "second voice"-консультацию с DeepSeek после того, как варианты с Wake-on-LAN (хрупко) и multi-device polling (overkill) были отклонены.

**3. 5-слойная память.** L1 raw events (всё, append-only) → L2 daily buffers (DeepSeek summaries) → L3 master knowledge (стабильные факты + KG) → L4 snapshots (MinIO) → L5 audit log (immutable). С дедупом через SHA-256 trigger и pruning через cron. Никакой векторной магии для базового quick-recall — просто хорошо проиндексированный jsonb + триггеры.

### Интеграция с Claude Code

Через MCP (Model Context Protocol). Поставил wrapper:

```bash
pip install --user cognitive-core-mcp
```

В `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "cogcore-rooms": {
      "command": "cognitive_core_mcp",
      "env": {
        "COGCORE_URL": "https://your-server.example",
        "COGCORE_AGENT_ID": "alice",
        "COGCORE_ROOM_KEY": "..."
      }
    }
  }
}
```

Перезапустил Claude Code — в picker'е появились `cognitive_room_create`, `_join`, `_post`, `_ask`, `_answer`, `_pending`, `_sync_pending`. Десять тулов, ~300 строк Python.

### Что не работает (честно)

Это alpha — расскажу о слабых местах сразу:

- **Один сервер.** Горизонтального масштабирования API нет, всё state в Postgres. Подходит для команды до 10-15 человек, не для SaaS.
- **Простая авторизация.** Per-agent ключи + per-room ключи. Никакого SSO, RBAC, audit-only ролей.
- **Schema migrations applies on startup.** Удобно для homelab, страшно для prod ops.
- **Без E2E encryption** — server-side trust required.
- **Built автономно.** Я писал этот стек большей частью autonomous-mode'ом с Claude Code как pair-programmer. Багов в edge-cases точно есть. Issue-tracker открыт.

### Если попробуете

Минимальное железо: 1 vCPU + 1 GB RAM + 5 GB диска. Я гоняю на стареньком i5-7500 с 32 GB DDR4, 19 контейнеров suммарно жрут 4.4 GB.

Что было бы максимально полезным feedback'ом:
- "Документ X запутал" — issue с тегом `docs`
- "У меня сценарий Y, не покрывается" — issue с тегом `feature`
- "Я попробовал, всё работает, спасибо" — звезда на github

### Ссылки

- **GitHub**: https://github.com/cognitive-core/launch
- **5-min screencast**: https://github.com/cognitive-core/launch#demo
- **OpenAPI**: https://github.com/cognitive-core/launch/blob/main/openapi/rooms.yaml
- **MCP wrapper**: https://github.com/cognitive-core/launch/tree/main/mcp-wrapper
- **Discord**: (TBA — после launch'а)

Спасибо что дочитали. Если есть вопросы по архитектурным решениям (особенно по B+D fallback'у или PG-NOTIFY pipeline'у) — задавайте в комментариях, отвечу с деталями.

---

### Метаданные для публикации

- **Хабы**: Open source, DevOps, Машинное обучение, Программирование
- **Теги**: ai-agents, multi-agent, claude, chatgpt, mcp, model-context-protocol, deepseek, postgres, docker, self-hosted, mit-license
- **Превью-картинка**: schematic из README (контейнерная диаграмма)
- **Время чтения** (примерно): 5-6 минут
