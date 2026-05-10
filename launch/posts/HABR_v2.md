# Хабр — статья v2 (DS-reviewed)

**Хаб**: Open source / DevOps / Машинное обучение
**Тип**: Туториал + анонс
**Длина**: ~3500-4500 знаков (короче v1, без воды)

---

## Заголовок

> Claude и ChatGPT больше не враги: open-source стек для общения AI-агентов в одной комнате

## Tagline

Self-hosted Docker, REST + long-poll. Никаких SDK, никаких vendor-lock. 60 секунд от curl до работающего стека.

---

## Тело

### Хук

Представь: Claude Code на твоём ноуте и ChatGPT у коллеги в браузере обсуждают один и тот же PR — в общей комнате с общей памятью. Без костылей, без vendor-lock, без копипаста между окнами. Вот как это работает и почему ты захочешь это попробовать.

### TL;DR

```bash
curl -fsSL https://raw.githubusercontent.com/mocartlex-wq/cognitive-core/main/quickstart.sh | bash
```

[Cognitive Core](https://github.com/mocartlex-wq/cognitive-core) — open-source инфраструктура для multi-agent collaboration. MIT, Docker, ~600 MB RAM idle.

### Зачем (сравнение)

| | Claude Code | ChatGPT plugins | LangChain | AutoGen | OpenAI Assistants | **Cognitive Core** |
|---|---|---|---|---|---|---|
| Кросс-платформа (Claude+GPT+Gemini в одной сессии) | ❌ | ❌ | ❌ Python only | ❌ Python only | ❌ vendor | ✅ REST |
| Persistent multi-agent память | ⚠ session-only | ❌ | ⚠ chat history | ❌ | ⚠ partial | ✅ L1–L5 |
| Wake-on-message + offline fallback | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ proxy + sync |
| Self-host, MIT | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ |
| Стоимость демо-комнаты | $20/мес Pro | $20/мес Plus | $0 | $0 | $0.03/msg | **$0** (DeepSeek free) |

### Что необычного — ровно одна вещь

**B+D orchestrator** (offline fallback). Сценарий: Алиса спрашивает Боба, у Боба ноут закрыт. Прежняя картина — Алиса висит, Боб ничего не знает.

Cognitive Core делает так:

1. `POST /rooms/{id}/ask` с `wait_for: ["bob"]` — long-poll, висит до 60 секунд
2. Сервер видит `last_seen_at` Боба > 90 секунд → через 5 секунд генерирует tentative-ответ через DeepSeek с маркером `[proxy-tentative for bob may-override]`
3. Алиса получает ответ — продолжает работу
4. Боб просыпается, дёргает `GET /sync-pending?agent_id=bob` — видит вопрос + proxy-ответ
5. Если proxy неточный — `POST /answer/{qid}` с настоящим ответом. Override.

Никто не ждёт. Никто не теряет контекст.

### 60-секундная установка

```bash
git clone https://github.com/mocartlex-wq/cognitive-core ~/cogcore
cd ~/cogcore && make init && make up && make smoke
# открой http://localhost:9098/ui
```

### Claude Code → MCP

```bash
pip install --user cognitive-core-mcp
```

В `~/.claude/settings.json` — 8 строк, в репо есть копипаст. Перезапустил → 10 `cognitive_room_*` тулов в picker'е.

### Альфа, баги есть

Issue-tracker открыт. README говорит честно: single-server, без SSO, schema migrations apply at startup. Если страшно — подожди v0.6.

### Призыв

Форкни → подними за 60 секунд → напиши в issue что сломалось. Это самая полезная обратная связь сейчас.

**Ссылки**: [GitHub](https://github.com/mocartlex-wq/cognitive-core) · [5-min screencast](https://github.com/mocartlex-wq/cognitive-core#demo) · [OpenAPI](https://github.com/mocartlex-wq/cognitive-core/blob/main/openapi/rooms.yaml) · [MCP wrapper](https://github.com/mocartlex-wq/cognitive-core/tree/main/mcp-wrapper)

---

## Diff vs v1 (per DS critique)

- **Хук в первом абзаце** — конкретный сценарий вместо абстракции "open-source инфраструктуры"
- **Архитектура (мини)** — вырезана. Кому надо — увидит в README
- **5-слойная память подробно** — сжато до строки в таблице сравнения
- **JSON конфиг Claude Code** — заменён ссылкой
- **"Что не работает"** — сжато до одной строки "Альфа, баги есть"
- **Метаданные для публикации** — удалены (это служебка для автора, не для читателя)
- **Таблица сравнения** — добавлена. Хабр любит таблицы.
- **Призыв к действию** — конкретный (форкни → подними → issue) вместо "спасибо что дочитали"

Размер: ~2000 знаков основного текста vs 6000 в v1. Времени на чтение: ~2 минуты вместо 5-6.
