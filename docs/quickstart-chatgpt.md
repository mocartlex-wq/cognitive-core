# Quickstart: ChatGPT Plus Custom GPT за 5 минут

## Что получится

Свой Custom GPT в ChatGPT Plus который умеет читать/писать в вашу память. Работает на любом устройстве где есть ChatGPT (web, iOS, Android).

## Шаги

### 1. Зарегистрируйтесь на cognitive-core
https://mcp.ии-память.рф/ui/pricing → «Начать бесплатно».

### 2. Сгенерируйте api_key через мастер
Профиль → «🪄 Передать помощнику»:
- Платформа: **ChatGPT Plus**
- Сгенерировать → получите OpenAPI yaml + api_key

### 3. Создайте Custom GPT
1. Зайдите в ChatGPT → Explore GPTs → Create
2. Перейдите на вкладку **Configure** → внизу **Actions** → **Create new action**
3. **Schema**: вставьте полный yaml из артефакта мастера (там описание 24 endpoints)
4. **Authentication** → Type: **API Key** → Header → name: `X-API-Key` → значение: ваш api_key
5. Сохраните action

### 4. Дайте GPT инструкции
В **Instructions** добавьте:
```
Ты ассистент с долговременной памятью через cognitive-core API.
ПРЕЖДЕ чем отвечать на содержательный вопрос — вызови cognitive_recall
с запросом пользователя, чтобы найти прошлые наработки.
ПОСЛЕ важного решения/факта/lesson — вызови cognitive_remember.
Используй cognitive_agent_manifest на старте чтобы понять все возможности.
```

### 5. Тест
В чате с вашим Custom GPT: «Запомни что я работаю над проектом X».
GPT должен вызвать cognitive_remember.
В новом чате: «Что я делал?». GPT вызовет cognitive_recall и найдёт.

## Ограничения ChatGPT Custom GPT

- **Не поддерживает SSE** (streaming) — поэтому MCP-stream tools (cognitive_continue с pending DMs) могут работать медленнее
- **Может пропускать tool calls** если ваш prompt сильно отклоняется — добавьте «обязательно вызови» явно
- **Custom GPTs шарятся только с другими ChatGPT Plus** пользователями

## OpenAPI спецификация (для справки)

Полный OpenAPI 3.1 schema публично доступен:
https://mcp.ии-память.рф/api/openapi/cognitive.yaml

Этот же yaml вшит в артефакт мастера, но если что — берите оттуда.

## Поддержка
- Email: owner@ии-память.рф
