# Quickstart: Cursor IDE за 5 минут

## Что получится

Cursor будет помнить вас между сессиями + умеет читать/писать в общую память с другими вашими агентами (Claude Code, ChatGPT, мобильное).

## Шаги

### 1. Зарегистрируйтесь
Откройте https://mcp.me-ai.ru/ui/pricing → «Начать бесплатно» → email → код. (legacy alias: https://mcp.ии-память.рф)

### 2. Передайте ключ Cursor
В профиле → «🪄 Передать помощнику»:
- Платформа: **Cursor IDE**
- Нажмите «Сгенерировать»

### 3. Скопируйте промпт → передайте в Cursor чат
Cursor (как и Claude Code) умеет редактировать свой MCP-конфиг автоматически. Промпт инструктирует его:
1. Проверить не подключён ли уже (если да — скажет «всё работает»)
2. Сделать curl за api_key
3. Добавить cognitive-core в `~/.cursor/mcp.json` (не перезатрёт другие)
4. Попросить рестарт

### 4. Перезапустите Cursor

### 5. Тест
В чате с Cursor: «cognitive_remember что я работаю над проектом X».
В новом чате: «cognitive_recall — что я работаю над?».

## Где Cursor хранит MCP-конфиг

- macOS: `~/.cursor/mcp.json` (или Settings → MCP в Cursor UI)
- Windows: `%USERPROFILE%\.cursor\mcp.json`
- Linux: `~/.cursor/mcp.json`

## Если ваш Cursor не умеет редактировать `mcp.json`

Скопируйте артефакт с сайта вручную:
1. Откройте `~/.cursor/mcp.json` в редакторе
2. Если файла нет — создайте `{"mcpServers": {}}`
3. В `mcpServers` добавьте секцию `cognitive-core` из артефакта мастера

## Поддержка
- Email: owner@ии-память.рф
