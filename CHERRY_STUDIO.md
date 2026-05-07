# Cherry Studio + Cognitive Core — самый простой способ

## Если запускали `installer.ps1`

Установщик уже **скопировал MCP-конфиг в буфер обмена**. Осталось 3 действия:

1. Откройте **Cherry Studio**
2. **Settings** (шестерёнка слева внизу) → **MCP Server**
3. Кнопка **Add Server** или **Import from JSON** → нажмите **Paste** или `Ctrl+V`

Готово. Включите toggle напротив `cognitive-core` чтобы статус стал зелёный.

## Если пришли сюда напрямую (без installer)

### Что скопировать в Cherry Studio

```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "docker",
      "args": ["exec", "-i", "cognitive_api", "python", "-m", "mcp_server.server"],
      "env": {
        "COGNITIVE_API_KEY": "key-design-001",
        "COGNITIVE_AGENT_NAME": "cherry_studio",
        "CC_IN_CONTAINER": "1"
      }
    }
  }
}
```

### Куда вставить

1. Cherry Studio → Settings → **MCP Server**
2. **Add** → выберите тип **stdio** (или JSON Import если есть кнопка)
3. **Paste** (`Ctrl+V`)
4. **Save**
5. Включить toggle (`cognitive-core` стало зелёным)

## Перед использованием

Убедитесь что Cognitive Core запущен:

```powershell
cd "D:\ИИ\память\память 1\cognitive-core"
docker compose up -d
```

Или через алиас (после `install_alias.ps1`):

```powershell
cc-up
```

## Проверка в чате Cherry Studio

Откройте новый чат **с моделью которая поддерживает Tools**:
- Claude Sonnet/Opus 3.5+
- GPT-4 / GPT-4o
- DeepSeek-Chat (рекомендуется — у вас уже есть ключ)

Выберите модель → **активируйте toggle MCP сервера** в боковой панели чата → напишите:

```
Используй cognitive_health и покажи статус системы
```

Если ИИ вызовет инструмент и вернёт JSON с `healthy: true` — **подключено**.

## Ручное заполнение полей (если нет JSON Import)

Некоторые версии Cherry Studio требуют ручного заполнения. Тогда:

| Поле | Значение |
|---|---|
| **Name** | `cognitive-core` |
| **Description** (опционально) | `5-layer AI memory system` |
| **Type** | `stdio` |
| **Command** | `docker` |
| **Arguments** | `exec -i cognitive_api python -m mcp_server.server` |
| **Environment Variables** | (см. ниже) |

Environment Variables (3 строки):

| Key | Value |
|---|---|
| `COGNITIVE_API_KEY` | `key-design-001` |
| `COGNITIVE_AGENT_NAME` | `cherry_studio` |
| `CC_IN_CONTAINER` | `1` |

## Распространённые проблемы

| Симптом | Решение |
|---|---|
| Сервер появился но красный | Проверьте `docker ps` — должен быть `cognitive_api`. Если нет: `docker compose up -d` |
| "command failed: docker" | Docker Desktop не запущен или не в PATH. Перезапустите Docker Desktop |
| Cherry Studio не видит инструменты в чате | Toggle MCP-сервера не включён ИЛИ модель не поддерживает tools — выберите Claude/GPT/DeepSeek |
| `Connection refused` при первом вызове | API контейнер только что запустился — подождите 5 секунд и попробуйте ещё раз |
| Tool вернул ошибку 401 | `COGNITIVE_API_KEY` не совпадает с одним из `AGENT_API_KEYS` в `.env` |

## Зачем это нужно

После подключения ваш Cherry Studio (с любой моделью) получает 7 инструментов:

| Инструмент | Что делает |
|---|---|
| `cognitive_remember` | Запомнить опыт работы |
| `cognitive_recall` | Найти знания по теме (KNN-поиск) |
| `cognitive_list` | Все знания домена |
| `cognitive_tools` | Реестр инструментов |
| `cognitive_consolidate` | Ручной запуск daily/weekly |
| `cognitive_health` | Статус системы |
| `cognitive_domains` | Активные домены |

Любая модель в Cherry Studio (DeepSeek/Claude/GPT/локальная Ollama) теперь имеет **долговременную память** которая накапливается из вашей работы.

**Особенно полезно с DeepSeek-Chat**: дёшево + долгая память = работа на уровне дорогих моделей за копейки.

## Подробности

- [`AGENT_GUIDE.md`](AGENT_GUIDE.md) — как агент работает с памятью (полный цикл)
- [`mcp_server/QUICKSTART.md`](mcp_server/QUICKSTART.md) — общая инструкция MCP для всех клиентов
- [`README.md`](README.md) — обзор проекта
