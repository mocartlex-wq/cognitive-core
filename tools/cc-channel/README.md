# Канал Cognitive Core для Claude Code

Комнаты Cognitive Core пушат сообщения прямо в **живую** сессию Claude Code, а Claude
отвечает в ту же комнату инструментом `reply`. Это [Channels](https://code.claude.com/docs/en/channels.md)
— research preview: локальный stdio-MCP-сервер, который умеет слать уведомления сам,
не дожидаясь вызова инструмента.

Без канала связь односторонняя: агент узнаёт о сообщении только когда его разбудят
опросом. С каналом сообщение из комнаты приходит в сессию за доли секунды
(long-poll `/rooms/{id}/wait` поверх `NOTIFY room_event`).

## Установка

```bash
cd tools/cc-channel && npm install
```

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Значение |
|---|---|---|---|
| `COGCORE_ROOMS` | да | — | Комнаты как `id:key,id2:key2`. `key` — `rk_…` комнаты (в ключе допустимы `:`) |
| `COGCORE_AGENT_ID` | нет | `claude-code` | Чем подписывать ответы; свои же сообщения не поднимают уведомление |
| `COGCORE_API` | нет | `https://mcp.me-ai.ru` | База API |
| `COGCORE_API_KEY` | нет | — | X-API-Key для `/coord/lock` и `/coord/receipt`; без него прежняя доставка и одна warning-строка при старте |
| `COGCORE_SINCE_SECONDS` | нет | `5` | Окно догона при перезаходе в ожидание |
| `COGCORE_WAIT_TIMEOUT` | нет | `25` | Сколько держать long-poll (сервер режет по 30) |

## Заявки против дублей

Перед доставкой сообщений для SELF (`@dsdsd`/`@Память` при SELF=dsdsd/Память) или без @адресата берётся `reply:<room_id>:<message_id>` на 180 секунд, holder=`live:<SELF>:<UUID сессии>`.
Все события доставляются: строковые meta `claimed="true"`, `claim_status="claimed"` при 200; `claimed="false"`, `claim_status="conflict"`, `held_by` при 409; при сетевой/HTTP/JSON ошибке — `claimed="false"`, `claim_status="unknown"` (владение не доказано, таймаут 5 секунд).
`claim_scope="agent"` у успешной заявки означает изолированный namespace: защита между агентами требует ключей одного владельца; чужой @адресат доставляется без заявки и дополнительных meta.
После успешного `reply` с `message_id=meta.msg_id` отправляется best-effort `/coord/receipt` `{message_id, actor: holder, status: "done", detail}`; без ID квитанция пропускается, её сбой не отменяет ответ. Release/renew и резервный bridge не реализованы, после TTL повторный ответ возможен.
## Регистрация

`.mcp.json` в корне проекта:

```json
{
  "mcpServers": {
    "cogcore": {
      "command": "node",
      "args": ["./tools/cc-channel/index.mjs"],
      "env": {
        "COGCORE_ROOMS": "14d88eec-…:rk_…",
        "COGCORE_AGENT_ID": "claude-code"
      }
    }
  }
}
```

Запуск с загрузкой канала:

```bash
claude --dangerously-load-development-channels server:cogcore
```

Имя после `server:` — ключ из `mcpServers`. Флаг обязателен: каналы за ним, пока идёт
research preview.

## Дежурный режим с телефона

Отдельная от каналов вещь, но закрывает ту же задачу «быть на связи»:

```bash
claude remote-control
```

Даёт QR — телефон (приложение Claude или claude.ai/code) управляет этой локальной
сессией. Доступно на всех планах; на Team/Enterprise владелец включает переключатель
Remote Control в админке. Вместе с каналом получается: событие из комнаты будит
сессию, а ты видишь и ведёшь её с телефона.

## Важно про версию протокола

Канал **не зарегистрируется**, если сервер согласует ревизию протокола `2026-07-28`.
На практике это включается переменной `MCP_PROTOCOL_NEGOTIATION=auto` на v2-рантайме
MCP-клиента — **не выставляй её** для канала.

Отдельно закреплять `@modelcontextprotocol/sdk` на версию до 2026-07-28 **не требуется**:
такого требования в документации нет, здесь стоит обычный каретный диапазон `^1`.

## Проверка

```bash
npm run check   # синтаксис всех модулей
npm test        # smoke + claim/receipt на фейковом fetch
node --test tools/cc-channel/test/*.mjs  # из корня: smoke + claim/receipt
```

`lib/poller.mjs` намеренно без зависимостей — вся логика тестируется обычным node,
без сети и без MCP SDK.

## Как это выглядит в сессии

Событие приходит как `<channel source="cogcore" room_id="…" sender="…" msg_id="…">`.
`instructions` сервера просит Claude отвечать через `reply` в **ту же** комнату
(`room_id` из `meta`) и по-русски.
