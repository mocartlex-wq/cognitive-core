# MCP подключение к Claude Desktop за 5 минут

> Подключите долговременную память Cognitive Core к Claude Desktop одной командой.
> Не нужны Python, pip, PYTHONPATH — только Docker.

## 5 шагов

### 1. Установите Docker Desktop

Если ещё не установлен: https://www.docker.com/products/docker-desktop/

После установки запустите его (он должен висеть в трее).

### 2. Скачайте проект

```powershell
# Например, в D:\cognitive-core
git clone <repo-url> cognitive-core
cd cognitive-core
```

### 3. Запустите автоустановщик

**Windows (PowerShell):**

```powershell
.\installer.ps1
```

**Linux / macOS:**

```bash
bash installer.sh
```

Скрипт:
- Проверит Docker
- Спросит DeepSeek API key (получить: https://platform.deepseek.com)
- Поднимет 4 контейнера (api + postgres + redis + minio)
- Дождётся пока система готова
- Создаст / обновит `claude_desktop_config.json` (без потери других MCP серверов)

### 4. Перезапустите Claude Desktop

**Windows:**
```powershell
Stop-Process -Name Claude -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-Process "shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude"
```

**macOS:**
```bash
pkill -f Claude && sleep 2 && open -a Claude
```

### 5. Проверьте в чате

Откройте новый чат и напишите:

> Используй cognitive_health и покажи статус системы.

Claude вызовет инструмент и вернёт JSON с layers. Если видите `healthy: True` — подключено.

---

## Как это работает

```
Claude Desktop
    │ stdio (JSON-RPC)
    ▼
docker exec -i cognitive_api python -m mcp_server.server
    │ HTTP localhost:8000 (внутри контейнера)
    ▼
Cognitive Core API (FastAPI)
    │
    ▼
L1-L4 + L5 + OP
```

**Главное:** `mcp_server` запускается **внутри** существующего Docker-контейнера через `docker exec`. На хосте нужен **только Docker**.

## Конфиг что получится в `claude_desktop_config.json`

```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "docker",
      "args": ["exec", "-i", "cognitive_api", "python", "-m", "mcp_server.server"],
      "env": {
        "COGNITIVE_API_KEY": "key-design-001",
        "COGNITIVE_AGENT_NAME": "claude_desktop",
        "CC_IN_CONTAINER": "1"
      }
    }
  }
}
```

7 инструментов которые получит Claude:
- `cognitive_remember` — записать опыт
- `cognitive_recall` — найти знания (с группировкой patterns/mistakes/rules/tools)
- `cognitive_list` — все знания домена
- `cognitive_tools` — инструменты домена
- `cognitive_consolidate` — ручной запуск daily/weekly
- `cognitive_health` — статус системы
- `cognitive_domains` — все активные домены

## Если что-то не работает

| Симптом | Решение |
|---|---|
| MCP индикатор красный, "docker: command not found" | Docker Desktop не установлен или не в PATH. Перезалогиньтесь после установки |
| MCP красный, "No such container: cognitive_api" | `docker compose up -d` из папки проекта |
| MCP красный, "Container not running" | `docker compose start` |
| Claude не предлагает использовать tools | Прямо просить: «используй cognitive_remember с domain=...» |
| Tool вернул "Connection refused" | API контейнер не запустился. Логи: `docker logs cognitive_api` |
| "Permission denied" на docker exec | На Linux: `sudo usermod -aG docker $USER` + перезалогин |

## Альтернатива — нативный запуск (для разработчиков)

Если хотите запускать MCP-сервер на хосте (без `docker exec`) — используйте конфигурацию с `PYTHONPATH`:

```json
{
  "mcpServers": {
    "cognitive-core": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:\\path\\to\\cognitive-core",
      "env": {
        "PYTHONPATH": "C:\\path\\to\\cognitive-core",
        "COGNITIVE_API_URL": "http://localhost:9001",
        "COGNITIVE_API_KEY": "key-design-001",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Требует:
- Python 3.11+ на хосте
- `pip install fastmcp httpx`
- Полный путь к `python.exe`
- `PYTHONPATH` указывающий на проект

**Не рекомендуется для большинства пользователей** — лишние зависимости и тонкости с Windows кодировками. Используйте `docker exec` (default).

## Полезные доменные соглашения

Группируйте опыт по типу работы:
- `coding_python`, `coding_react`, `coding_sql`
- `ops_docker`, `ops_postgres`, `ops_aws`
- `bugfix_log` — где какой баг был
- `decisions_arch` — архитектурные решения с обоснованием
- `meeting_notes_<project>`

В одном домене должно быть **минимум 3 события** в день для срабатывания daily-консолидации.

## Что дальше

Используйте Claude Desktop как обычно. Иногда просите: «и запиши это в память». Через 7 дней:

```powershell
cd "D:\path\to\cognitive-core"
.\dogfooding\save_daily.cmd        # ежедневный отчёт
python scripts\dogfood_check.py    # 4 индикатора готовности
```

Подробнее в [`AGENT_GUIDE.md`](../AGENT_GUIDE.md) и [`dogfooding/README.md`](../dogfooding/README.md).
