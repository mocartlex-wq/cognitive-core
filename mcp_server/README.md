# Cognitive Core — MCP Server

Подключите долговременную память Cognitive Core к Claude Desktop, Cursor, Claude Code через Model Context Protocol.

## Что это даёт

Любой MCP-совместимый клиент получает 7 инструментов работы с памятью:

| Tool | Что делает |
|---|---|
| `cognitive_remember` | Записать опыт в L1 (память запомнит после daily/weekly консолидации) |
| `cognitive_recall` | KNN-поиск по выученным знаниям (L3) + инструментам |
| `cognitive_list` | Просмотреть активные L3-знания |
| `cognitive_tools` | Список инструментов домена |
| `cognitive_consolidate` | Ручной запуск daily/weekly консолидации |
| `cognitive_health` | Статус системы (размеры слоёв, uptime) |
| `cognitive_domains` | Все домены с активными данными |

## Установка

```bash
# 1. Cognitive Core должен быть запущен (docker compose up -d)
# 2. Установить fastmcp + httpx
pip install fastmcp httpx

# 3. Запустить MCP сервер для проверки (stdio mode)
python -m mcp_server.server
```

## Подключение к Claude Desktop

Откройте `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
или `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

Добавьте:

```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:\\path\\to\\cognitive-core",
      "env": {
        "COGNITIVE_API_URL": "http://localhost:9001",
        "COGNITIVE_API_KEY": "key-design-001",
        "COGNITIVE_AGENT_NAME": "claude_desktop"
      }
    }
  }
}
```

Перезапустите Claude Desktop. В чате появится индикатор подключения MCP-сервера.

## Подключение к Cursor

Cursor использует HTTP-transport. Запустите сервер в HTTP-режиме:

```bash
python -m mcp_server.server --http
# слушает на 0.0.0.0:8765
```

В Cursor settings → MCP добавьте:

```json
{
  "mcpServers": {
    "cognitive-core": { "url": "http://localhost:8765" }
  }
}
```

## Подключение к Claude Code

```bash
claude mcp add cognitive-core -- python -m mcp_server.server \
  -e COGNITIVE_API_URL=http://localhost:9001 \
  -e COGNITIVE_API_KEY=key-design-001
```

## Примеры использования (в Claude/Cursor чате)

**Запомнить что-то:**
> Запомни в памяти: я только что починил баг в payments — оказалось проблема в timezone-aware datetime. Используй cognitive_remember с domain="bugfix_log".

**Спросить что система знает:**
> Используй cognitive_recall и найди что мы знаем про таймзоны в питоне.

**Посмотреть всю текущую память:**
> Покажи список знаний в домене bugfix_log через cognitive_list.

## Переменные окружения

| Переменная | Default | Назначение |
|---|---|---|
| `COGNITIVE_API_URL` | `http://localhost:9001` | URL Cognitive Core API |
| `COGNITIVE_API_KEY` | `key-design-001` | X-API-Key для аутентификации |
| `COGNITIVE_AGENT_NAME` | `claude_via_mcp` | Имя агента в L1 событиях (для аудита) |
| `MCP_PORT` | `8765` | Порт для HTTP/SSE-режима |

## Архитектура

```
Claude Desktop / Cursor / Claude Code
         │
         │ MCP protocol (stdio / HTTP / SSE)
         ▼
   mcp_server.server (FastMCP)
         │
         │ HTTP (X-API-Key auth)
         ▼
  Cognitive Core API (FastAPI)
         │
         ▼
  L1 → L2 → L3 → L4 → L5 (полный цикл памяти)
```

MCP-сервер — тонкая обёртка. Логика памяти, KNN, LLM-консолидация — всё в Cognitive Core API.
