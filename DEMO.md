# Cognitive Core — Демо за 5 минут

Покажем работу системы от первого `docker compose up` до KNN-поиска по выученным знаниям.

## Подготовка (1 минута)

```bash
git clone <repo-url> cognitive-core && cd cognitive-core
cp .env.example .env
# Откройте .env и впишите DEEPSEEK_API_KEY (получить на platform.deepseek.com — бесплатные кредиты)
docker compose up -d --build
```

Дождитесь, пока 4 контейнера будут healthy:

```bash
docker compose ps
# cognitive_api      Up (healthy)   0.0.0.0:9001->8000/tcp
# cognitive_postgres Up (healthy)   0.0.0.0:5432->5432/tcp
# cognitive_redis    Up (healthy)   0.0.0.0:6379->6379/tcp
# cognitive_minio    Up (healthy)   0.0.0.0:9000->9000/tcp
```

## Сценарий 1 — Демо одной кнопкой (рекомендуется)

1. Откройте `http://localhost:9001/`
2. Нажмите **«Запустить демо»**
3. Появится модалка с прогрессом — 12 шагов за ~1.5-2 минуты:
   - 18 событий в L1
   - 3 инструмента
   - Daily консолидация по 3 доменам (DeepSeek)
   - Weekly консолидация по 3 доменам (DeepSeek + Curator)
   - 3 KNN-запроса
4. После завершения цифры в hero обновятся: L1 +18, L2 +3, L3 +29 знаний
5. Перейдите на `http://localhost:9001/ui` — увидите данные на дашборде

## Сценарий 2 — Из терминала

```bash
python scripts/seed_demo.py --full
```

То же что и кнопка, но в консоли с подробным выводом.

## Сценарий 3 — Свои данные руками

### Шаг 1. Записать опыт агента

```bash
curl -X POST http://localhost:9001/events \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{
    "source_agent": "agent_designer",
    "domain": "my_project",
    "payload": {
      "task": "Сделать кнопку Submit на форме регистрации",
      "result": "Использовал primary button + disabled state на время отправки",
      "feedback": "positive",
      "tools_used": ["react", "tailwind"]
    }
  }'
# {"id": "uuid-...", "status": "accepted"}
```

Повторите 5+ раз с разными вариациями (подсказка: используйте `/sandbox` чтобы не печатать).

### Шаг 2. Запустить дневной анализ

```bash
curl -X POST "http://localhost:9001/memory/consolidate/daily?domain=my_project" \
  -H "X-API-Key: key-design-001"
# {"status":"ok","results":[{"domain":"my_project","status":"consolidated","buffer_id":"uuid-..."}]}
```

DeepSeek прочитал ваши события и сохранил резюме дня в L2.

### Шаг 3. Запустить недельный синтез

```bash
curl -X POST "http://localhost:9001/memory/consolidate/weekly?domain=my_project" \
  -H "X-API-Key: key-design-001"
# {"status":"consolidated","new_items":N,"deprecated":0,"tools_added":M,"vectors_indexed":K}
```

Куратор отфильтровал слабые паттерны, DeepSeek синтезировал из L2 → L3 знания.

### Шаг 4. Найти знание через KNN

```bash
curl -X POST http://localhost:9001/operative/query \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{
    "domain": "my_project",
    "context": "Как делать кнопки Submit?",
    "top_k": 3
  }'
```

В ответе — релевантные L3-записи + инструменты с distance (чем меньше — тем ближе по смыслу).

### Шаг 5. Закрыть сессию (опционально, обратная петля)

```bash
SID="<session_id из шага 4>"
curl -X POST "http://localhost:9001/operative/sessions/$SID/close" \
  -H "X-API-Key: key-design-001" \
  -H "Content-Type: application/json" \
  -d '{"keep_results": true, "source_agent": "agent_designer"}'
```

Если `keep_results=true` — результат поиска вернётся в L1 как новый опыт и пойдёт по циклу заново.

## Проверка через дашборд

Откройте `http://localhost:9001/ui`:

| Вкладка | Что показывает |
|---|---|
| Обзор | Общие счётчики L1-L4 + график активности за 7 дней |
| Слои памяти | Сравнение размеров слоёв на bar-chart |
| События L1 | Live-таблица последних событий с фильтром по домену |
| Знания L3 | Все выученные знания с типом (pattern/mistake/rule) |
| Аудит L5 | Журнал действий (можно фильтровать по «только ошибки») |
| Домены | Таблица всех доменов со счётчиками по слоям |
| LLM / A/B | Статистика моделей (если настроен A/B) |

## Подключение из Claude Desktop

1. Установите MCP-сервер:
   ```bash
   pip install fastmcp httpx
   ```

2. Добавьте в `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "cognitive-core": {
         "command": "python",
         "args": ["-m", "mcp_server.server"],
         "cwd": "/абсолютный/путь/к/cognitive-core",
         "env": {
           "COGNITIVE_API_URL": "http://localhost:9001",
           "COGNITIVE_API_KEY": "key-design-001"
         }
       }
     }
   }
   ```

3. Перезапустите Claude Desktop. В чате попробуйте:
   > Используй cognitive_recall чтобы найти что мы знаем про React-кнопки.

## Подключение из Python-кода

```python
import asyncio
from cognitive import AsyncMemoryClient

async def main():
    async with AsyncMemoryClient(
        base_url="http://localhost:9001",
        api_key="key-design-001",
    ) as memory:
        # Записать
        await memory.remember(
            domain="my_project",
            payload={"task": "...", "result": "...", "feedback": "positive"},
        )

        # Найти
        results = await memory.recall(
            domain="my_project",
            context="как сделать ...",
            top_k=5,
        )
        for r in results:
            print(r["distance"], r.get("content"))

asyncio.run(main())
```

## Что дальше

- **Поработать с системой**: пусть ваш агент пишет свои события несколько дней. Куратор автоматически фильтрует шум, weekly создаст реальные знания.
- **Подключить агента**: используйте Python SDK или MCP-сервер.
- **Локальный LLM**: установите `LOCAL_AI_ENABLED=true` и поднимите Ollama с `qwen3:14b` — рутинные задачи (curator) уйдут на локальную GPU.
- **Production**: см. [DEPLOY.md](DEPLOY.md) — TLS, бэкапы, мониторинг.

## Полезные команды для отладки

```bash
# Логи API
docker logs -f cognitive_api

# Логи всех контейнеров
docker compose logs -f --tail=50

# Зайти в Postgres
docker exec -it cognitive_postgres psql -U cognitive -d cognitive_core
# \dt — список таблиц
# SELECT COUNT(*) FROM l3_master_knowledge WHERE effective_to IS NULL;

# Зайти в Redis
docker exec -it cognitive_redis redis-cli
# KEYS op:* — векторные ключи
# FT.INFO idx:operative — состояние индекса

# Прогнать тесты
docker exec cognitive_api python -m pytest tests/ -v

# Сбросить всю память (полная очистка)
docker compose down -v && docker compose up -d --build
```

## Что-то не работает?

| Симптом | Решение |
|---|---|
| `cognitive_api` не стартует | `docker logs cognitive_api` — обычно нет `DEEPSEEK_API_KEY` в `.env` |
| KNN ничего не находит | Запустите daily + weekly чтобы появились L3 знания. Проверьте `/dashboard/knowledge` |
| Daily вернул `lock_held` | Это норма — параллельный вызов на тот же домен. Подождите завершения первого |
| Тесты `test_pgvector.py` падают | Старая версия `postgres:16` без pgvector. `docker compose down && up -d --build` |
| Браузер показывает старую страницу | `Ctrl+Shift+R` (форс-перезагрузка без кэша) |

Подробнее: [DEPLOY.md → Troubleshooting](DEPLOY.md#troubleshooting).
