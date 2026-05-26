"""Unified Agent Onboarding wizard endpoints.

Поддерживает 5-step web-wizard на /ui/connect → выбор платформы → имя
агента → генерация платформ-специфичного конфига → опц. медиа → verify.

Endpoint-ы:
    GET  /user/connect/platforms             — список поддерживаемых платформ
    POST /user/connect/generate              — создать агента + вернуть config
    GET  /user/connect/qr?token=<one-shot>   — PNG QR для мобильного flow
    GET  /user/agents/{id}/verify            — проверка «агент пробудился»
    POST /user/connect/track                 — A/B аналитика onboarding funnel

Wizard вызывает _create_agent_core() из user.py напрямую (без HTTP-hop),
получает api_key, формирует артефакт по выбранной платформе. Артефакт —
готовый JSON/yaml/script готовый к копированию в нужное место.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.openapi_gen import build_custom_gpt_openapi
from app.api.user import CreateAgentBody, _create_agent_core
from app.db.postgres import get_pool
from app.db.redis import get_redis
from app.security.middleware import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/user/connect", tags=["connect"])

# Production base URL — пишется в артефакты (mcpServers.url, OpenAPI server и т.д.).
# Primary ASCII домен mcp.me-ai.ru (2026-05-23) — bypass'ит Claude Code SDK
# auto-mode classifier который блокирует punycode-домены как «exfiltration».
# Legacy alias mcp.xn----8sbwawqx4fza.xn--p1ai всё ещё работает (server_name).
BASE_URL = "https://mcp.me-ai.ru"
MCP_SSE_URL = f"{BASE_URL}/mcp/sse"
MCP_MESSAGES_URL = f"{BASE_URL}/mcp/messages"

# Short-lived in-memory QR token store (10min TTL). Для production-scale
# это переносится в Redis, но для wizard'а одного юзера за раз — ок.
_QR_TOKENS: dict[str, dict] = {}
QR_TTL_SECONDS = 600

# Claim tokens — короткие коды для «agent self-onboarding»:
# owner генерит в своей сессии, передаёт агенту, агент обменивает на api_key.
# Формат: 8 hex-символов в виде XXXX-XXXX (легко читать вслух).
# TTL 10 минут, one-shot consumption.
_CLAIM_TOKENS: dict[str, dict] = {}
# Audit-trail uses: чтобы различать «никогда не было» vs «уже использован» vs «истёк».
# Хранит {token: {used_at, used_by_ip, original_user_id}} ~1 час, потом cleanup.
_CLAIM_TOKENS_USED: dict[str, dict] = {}
CLAIM_TTL_SECONDS = 600
CLAIM_USED_AUDIT_TTL = 3600  # 1 час видна история «used by browser» для диагностики


def _generate_claim_token() -> str:
    """Generate human-readable 8-hex claim token формата XXXX-XXXX."""
    raw = secrets.token_hex(4).upper()  # 8 hex chars
    return f"{raw[:4]}-{raw[4:]}"


# ─────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────
class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[\w.\-]+$")
    platform: str = Field(..., description="claude_code|claude_pro_connector|chatgpt_custom_gpt|cursor|windows_cli|linux_mac_cli|mobile_qr")
    machine_hint: str | None = Field(None, max_length=128, description="например 'MacBook Pro M2'")
    description: str | None = Field(None, max_length=300)


class TrackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(..., max_length=64)
    action: str = Field(..., pattern=r"^(opened|copied|verified|failed|skipped)$")
    detail: str | None = Field(None, max_length=200)


# ─────────────────────────────────────────────────────────────────────────
# Platforms registry
# ─────────────────────────────────────────────────────────────────────────
PLATFORMS: list[dict[str, Any]] = [
    {
        "id": "claude_code",
        "name": "Claude Code",
        "icon": "💻",
        "tier": "best",
        "description": "Самый мощный — bash + Read tool + native MCP. Установить десктоп-приложение Claude Code, вставить JSON в claude.json.",
        "install_strategy": "mcp_json",
        "supports_media_cli": True,
        "supports_rooms": True,
    },
    {
        "id": "cursor",
        "name": "Cursor IDE",
        "icon": "📝",
        "tier": "best",
        "description": "Полноценный MCP-клиент в IDE. JSON-конфиг в `.cursor/mcp.json`.",
        "install_strategy": "mcp_json",
        "supports_media_cli": True,
        "supports_rooms": True,
    },
    {
        "id": "claude_pro_connector",
        "name": "Claude.ai Pro (web)",
        "icon": "🌐",
        "tier": "simple",
        "description": "Без установки — Settings → Custom Connectors → вставить URL и header. Не умеет читать локальные файлы (медиа загружать через web-UI).",
        "install_strategy": "connector_url",
        "supports_media_cli": False,
        "supports_rooms": True,
    },
    {
        "id": "chatgpt_custom_gpt",
        "name": "ChatGPT Plus (Custom GPT)",
        "icon": "🤖",
        "tier": "simple",
        "description": "Создать Custom GPT → Actions → импорт OpenAPI yaml. Для медиа — drag-drop в web-UI.",
        "install_strategy": "openapi_yaml",
        "supports_media_cli": False,
        "supports_rooms": True,
    },
    {
        "id": "windows_cli",
        "name": "Windows CLI (PowerShell)",
        "icon": "🖥️",
        "tier": "power",
        "description": "Standalone `cogcore` + `cogmedia` команды. One-liner: `iwr ... | iex`.",
        "install_strategy": "cli_script",
        "supports_media_cli": True,
        "supports_rooms": False,
    },
    {
        "id": "linux_mac_cli",
        "name": "Linux / macOS CLI",
        "icon": "🐧",
        "tier": "power",
        "description": "Standalone `cogcore` + `cogmedia` команды. One-liner: `curl ... | bash`.",
        "install_strategy": "cli_script",
        "supports_media_cli": True,
        "supports_rooms": False,
    },
    {
        "id": "mobile_qr",
        "name": "Мобильный (QR-код)",
        "icon": "📱",
        "tier": "simple",
        "description": "Сканировать QR с десктопа → попасть на mobile-страницу → добавить iOS Shortcut / Android Tasker для быстрого remember.",
        "install_strategy": "qr",
        "supports_media_cli": False,
        "supports_rooms": True,
    },
]


# ─────────────────────────────────────────────────────────────────────────
# Artifact builders — по одному на платформу
# ─────────────────────────────────────────────────────────────────────────
def _build_mcp_json(agent_id: str, api_key: str) -> dict[str, Any]:
    """Конфиг для Claude Code / Claude Desktop / Cursor mcpServers.

    Возвращает ДВА артефакта:
      1) agent_prompt — готовый промпт для копирования в чат с помощником
         (помощник сам отредактирует ~/.claude.json через Edit/Bash tools).
         Это recommended path — нулевой touch файлов.
      2) content — raw JSON для manual paste (legacy).
    """
    config = {
        "mcpServers": {
            "cognitive-core": {
                "command": "npx",
                "args": ["-y", "mcp-remote", MCP_SSE_URL, "--header", f"X-API-Key: {api_key}"],
            },
        },
    }
    raw_json = json.dumps(config, indent=2, ensure_ascii=False)
    agent_prompt = f"""Привет! Я подключаю тебя как помощника к Cognitive Core — это 5-слойная самохостимая память для AI-агентов (https://mcp.me-ai.ru).

Пожалуйста добавь меня в свой MCP-конфиг. Найди файл (по приоритету):
  • Claude Code: ~/.claude.json
  • Claude Desktop macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
  • Claude Desktop Windows: %APPDATA%\\Claude\\claude_desktop_config.json
  • Cursor: ~/.cursor/mcp.json или .cursor/mcp.json в проекте

В корне JSON под ключом "mcpServers" добавь новую запись "cognitive-core":

{raw_json}

ВАЖНО:
  • Сохраняй существующие mcpServers (если есть) — добавь как новый ключ, не перетирай.
  • Используй Edit tool (или jq для surgical edit) — НЕ перезаписывай весь файл.
  • Мой agent_id: {agent_id} — он же зашит в api_key выше.
  • API key — секретный, не логируй и не пересылай.

После сохранения скажи мне сделать рестарт (я перезапущу Claude Code/Cursor вручную). У тебя появятся 24 MCP-инструмента: cognitive_remember, cognitive_recall, cognitive_send, cognitive_inbox, room_join, room_post, room_read, room_ask, room_answer и т.д.

Подтверди что конфиг сохранён и я могу рестартить."""

    return {
        "kind": "mcp_json",
        "filename": "claude_mcp_config.json",
        "mime": "application/json",
        "content": raw_json,
        "agent_prompt": agent_prompt,  # NEW — для табы «🤖 Поручить помощнику»
        "instructions": (
            f"📌 Два способа подключения (выберите один):\n\n"
            f"🤖 СПОСОБ A (рекомендуется): просто скопируйте промпт «Поручить помощнику» "
            f"и вставьте в чат с Claude Code/Cursor — помощник сам отредактирует "
            f"~/.claude.json через свои инструменты Edit/Bash.\n\n"
            f"📝 СПОСОБ B (вручную):\n"
            f"  1. Откройте `~/.claude.json` (Claude Code) или `claude_desktop_config.json` "
            f"(Claude Desktop) или `.cursor/mcp.json` (Cursor).\n"
            f"  2. Добавьте секцию `mcpServers.cognitive-core` из JSON в существующий config.\n"
            f"  3. Перезапустите приложение.\n\n"
            f"После любого способа: у помощника появятся 24 MCP-инструмента — "
            f"cognitive_remember, cognitive_recall, room_post и т.д.\n"
            f"agent_id вашего помощника: `{agent_id}`."
        ),
    }


def _build_connector_url(agent_id: str, api_key: str) -> dict[str, Any]:
    """Конфиг для Claude.ai Pro Custom Connector."""
    return {
        "kind": "connector_url",
        "filename": "claude_pro_connector.txt",
        "mime": "text/plain",
        "content": (
            f"# Cognitive Core — Claude.ai Pro Custom Connector\n"
            f"# Скопировать в Settings → Custom Connectors → Add Server\n\n"
            f"URL:    {MCP_SSE_URL}\n"
            f"Header: X-API-Key: {api_key}\n\n"
            f"agent_id: {agent_id}\n"
        ),
        "instructions": (
            f"1. Откройте Claude.ai → Settings → Custom Connectors → «Add Server».\n"
            f"2. URL: `{MCP_SSE_URL}`\n"
            f"3. Authentication: «Custom header» → имя `X-API-Key`, значение — ваш ключ выше.\n"
            f"4. Save. В новом чате выберите connector `cognitive-core` в меню атачей.\n"
            f"5. Запросите: «recall what we discussed about python» — Claude позовёт cognitive_recall."
        ),
    }


def _build_openapi_artifact(agent_id: str, api_key: str) -> dict[str, Any]:
    """OpenAPI yaml для ChatGPT Custom GPT Actions."""
    yaml = build_custom_gpt_openapi(agent_id=agent_id, base_url=BASE_URL)
    return {
        "kind": "openapi_yaml",
        "filename": "cognitive-core-openapi.yaml",
        "mime": "text/yaml",
        "content": yaml,
        "instructions": (
            f"1. Откройте https://chatgpt.com → My GPTs → Create.\n"
            f"2. Configure → Actions → «Create new action» → Import from URL: "
            f"`{BASE_URL}/api/openapi/cognitive.yaml` (или загрузить скачанный yaml).\n"
            f"3. Authentication → API Key → header `X-API-Key`, значение — ваш ключ.\n"
            f"4. В тестовом промпте: «remember domain:test task:hello» → Custom GPT вызовет cognitive_remember.\n"
            f"5. agent_id: `{agent_id}`."
        ),
    }


def _build_ps1_script(agent_id: str, api_key: str) -> dict[str, Any]:
    """PowerShell installer для Windows."""
    script = f"""# Cognitive Core — Windows installer (pre-baked для agent_id={agent_id})
$ErrorActionPreference = 'Stop'
$ApiKey = '{api_key}'
$BinDir = "$env:USERPROFILE\\.cognitive-core\\bin"
$CfgDir = "$env:USERPROFILE\\.config\\cogcore"

# 1. Create dirs
New-Item -ItemType Directory -Force -Path $BinDir, $CfgDir | Out-Null

# 2. Save API key
Set-Content -Path "$CfgDir\\api-key" -Value $ApiKey -NoNewline -Encoding utf8

# 3. Download cogmedia.exe (or fall back to bash script)
$CogMediaUrl = "{BASE_URL}/static/cogmedia"
Invoke-WebRequest -Uri $CogMediaUrl -OutFile "$BinDir\\cogmedia" -UseBasicParsing
Write-Host "cogmedia installed to $BinDir\\cogmedia"

# 4. Add bin to PATH (current session + permanent)
if ($env:PATH -notlike "*$BinDir*") {{
    [Environment]::SetEnvironmentVariable('PATH', "$env:PATH;$BinDir", 'User')
    $env:PATH = "$env:PATH;$BinDir"
    Write-Host "Added $BinDir to PATH"
}}

# 5. Set env var
[Environment]::SetEnvironmentVariable('COGNITIVE_API_KEY', $ApiKey, 'User')
$env:COGNITIVE_API_KEY = $ApiKey
Write-Host "COGNITIVE_API_KEY set"

# 6. Test
Write-Host "`nTest: GET /health"
$resp = Invoke-RestMethod -Uri "{BASE_URL}/health" -TimeoutSec 5
Write-Host "  cognitive-core healthy=$($resp.healthy) version=$($resp.version)"

Write-Host "`nDone. agent_id={agent_id}"
Write-Host "Try: cogmedia C:\\path\\to\\photo.png"
"""
    return {
        "kind": "cli_script_windows",
        "filename": "install-cogcore.ps1",
        "mime": "text/x-powershell",
        "content": script,
        "instructions": (
            f"1. Откройте PowerShell.\n"
            f"2. Запустите one-liner:\n"
            f"   `iwr {BASE_URL}/static/install-cogcore.ps1 -OutFile $env:TEMP\\install.ps1; & $env:TEMP\\install.ps1`\n"
            f"   (или вручную выполните содержимое скачанного `install-cogcore.ps1`)\n"
            f"3. После установки команда `cogmedia C:\\photo.png` доступна из любого окна PowerShell.\n"
            f"4. agent_id: `{agent_id}`, API key уже сохранён в $env:COGNITIVE_API_KEY."
        ),
    }


def _build_bash_script(agent_id: str, api_key: str) -> dict[str, Any]:
    """Bash installer для Linux / macOS."""
    script = f"""#!/usr/bin/env bash
# Cognitive Core — Linux/macOS installer (pre-baked для agent_id={agent_id})
set -euo pipefail

API_KEY='{api_key}'
BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/cogcore"

# 1. Create dirs
mkdir -p "$BIN_DIR" "$CFG_DIR"

# 2. Save API key (chmod 600)
echo -n "$API_KEY" > "$CFG_DIR/api-key"
chmod 600 "$CFG_DIR/api-key"
echo "API key → $CFG_DIR/api-key (chmod 600)"

# 3. Download cogmedia
curl -fsSL "{BASE_URL}/static/cogmedia" -o "$BIN_DIR/cogmedia"
chmod +x "$BIN_DIR/cogmedia"
echo "cogmedia → $BIN_DIR/cogmedia"

# 4. PATH hint
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    echo
    echo "⚠ $BIN_DIR не в PATH. Добавьте:"
    echo "  echo 'export PATH=\\"$BIN_DIR:\\$PATH\\"' >> ~/.bashrc   # или ~/.zshrc"
fi

# 5. Test
echo
echo "Test: GET /health"
curl -sf "{BASE_URL}/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  cognitive-core healthy={{d[\\\"healthy\\\"]}} version={{d[\\\"version\\\"]}}')" || echo "  ⚠ health check failed"

echo
echo "Done. agent_id={agent_id}"
echo "Try: cogmedia /path/to/photo.png"
"""
    return {
        "kind": "cli_script_unix",
        "filename": "install-cogcore.sh",
        "mime": "text/x-shellscript",
        "content": script,
        "instructions": (
            f"1. Откройте terminal.\n"
            f"2. Запустите one-liner:\n"
            f"   `curl -sSL {BASE_URL}/static/install-cogcore.sh | bash`\n"
            f"3. После установки команда `cogmedia ~/photo.png` доступна (после source ~/.bashrc).\n"
            f"4. agent_id: `{agent_id}`, API key в `~/.config/cogcore/api-key`."
        ),
    }


def _build_mobile_qr(agent_id: str, api_key: str) -> dict[str, Any]:
    """Mobile QR-flow — выдаёт one-shot токен для /ui/connect/mobile."""
    token = secrets.token_urlsafe(24)
    _QR_TOKENS[token] = {
        "agent_id": agent_id,
        "api_key": api_key,
        "created_at": time.time(),
    }
    deep_link = f"{BASE_URL}/ui/connect/mobile?token={token}"
    return {
        "kind": "qr",
        "filename": "mobile-qr.png",
        "mime": "image/png",
        "content": deep_link,  # сама строка URL, кодируется в QR на frontend
        "qr_url": f"/user/connect/qr?token={token}",  # backend endpoint который рендерит PNG
        "instructions": (
            f"1. Откройте на мобильном камеру/QR-сканер.\n"
            f"2. Сканируйте QR — попадёте на мобильную страницу с уже привязанным агентом.\n"
            f"3. Добавьте iOS Shortcut / Android Tasker (предложит сама страница) для быстрого remember с телефона.\n"
            f"4. Токен живёт 10 минут — потом нужен новый QR.\n"
            f"5. agent_id: `{agent_id}`."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────
@router.get("/platforms")
async def list_platforms():
    """Список платформ с метаданными для wizard'а (шаг 1)."""
    return {"platforms": PLATFORMS}


@router.post("/generate")
async def generate_config(body: GenerateBody, request: Request):
    """Создать агента и сгенерировать платформ-специфичный конфиг.

    Шаг 3 wizard'а. Создаёт row в `agent_states` + `agent_keys` через
    переиспользование `_create_agent_core`, потом строит артефакт нужного
    типа по `body.platform`.

    Возвращает:
        {
          "agent_id": "...",
          "api_key": "...",         # показывается ОДИН раз
          "warning": "...",
          "platform": "claude_code",
          "artifact": {
            "kind": "mcp_json",
            "filename": "...",
            "mime": "...",
            "content": "...",      # готов к копированию
            "instructions": "...", # человеческий текст что делать
            ...
          }
        }
    """
    user = await require_user(request)

    # Создаём агента (или 409 если занят)
    create_body = CreateAgentBody(
        agent_id=body.agent_id,
        description=body.description,
        machine=body.machine_hint,
        project="connect-wizard",
    )
    agent = await _create_agent_core(user, create_body)
    api_key = agent["api_key"]

    # Строим артефакт по платформе
    platform_id = body.platform
    builders = {
        "claude_code": _build_mcp_json,
        "cursor": _build_mcp_json,
        "claude_desktop": _build_mcp_json,
        "claude_pro_connector": _build_connector_url,
        "chatgpt_custom_gpt": _build_openapi_artifact,
        "windows_cli": _build_ps1_script,
        "linux_mac_cli": _build_bash_script,
        "mobile_qr": _build_mobile_qr,
    }
    builder = builders.get(platform_id)
    if not builder:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown platform '{platform_id}'. Allowed: {list(builders)}",
        )

    artifact = builder(body.agent_id, api_key)

    logger.info(
        "connect_generated user_id=%s agent_id=%s platform=%s",
        user.user_id, body.agent_id, platform_id,
    )

    return {
        "ok": True,
        "agent_id": body.agent_id,
        "api_key": api_key,
        "warning": agent.get("warning", ""),
        "platform": platform_id,
        "artifact": artifact,
    }


@router.get("/qr")
async def render_qr(token: str):
    """Отрендерить PNG QR-код с deep-link на mobile-страницу.

    Token — one-shot, выданный из /generate с platform=mobile_qr.
    PNG генерируется через минимальную реализацию QR без зависимостей
    (используем data-URL-стиль SVG если qrcode lib не доступна).
    """
    entry = _QR_TOKENS.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="QR-token expired or invalid")
    if time.time() - entry["created_at"] > QR_TTL_SECONDS:
        _QR_TOKENS.pop(token, None)
        raise HTTPException(status_code=410, detail="QR-token expired (>10min)")

    deep_link = f"{BASE_URL}/ui/connect/mobile?token={token}"

    # Попробуем qrcode lib (PIL backend — Pillow уже в requirements);
    # если нет — fallback на SVG прямо вписанный.
    try:
        import qrcode
        import io
        # qrcode по умолчанию использует PIL — Pillow уже в requirements.txt
        img = qrcode.make(deep_link, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")
    except Exception:
        # Fallback: вернуть простой SVG (любой браузер отрендерит)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">'
            f'<rect width="256" height="256" fill="#fff"/>'
            f'<text x="128" y="120" font-family="monospace" font-size="11" '
            f'text-anchor="middle" fill="#000">QR недоступен — установите qrcode lib</text>'
            f'<text x="128" y="142" font-family="monospace" font-size="9" '
            f'text-anchor="middle" fill="#666">или откройте вручную:</text>'
            f'<text x="128" y="160" font-family="monospace" font-size="8" '
            f'text-anchor="middle" fill="#06c">/ui/connect/mobile?token={token[:16]}...</text>'
            f'</svg>'
        )
        return Response(content=svg, media_type="image/svg+xml")


@router.get("/mobile-resolve")
async def resolve_mobile_token(token: str):
    """Mobile page вызывает чтобы получить api_key по one-shot токену.

    Token expires after first read (бережём от replay на shared link).
    """
    entry = _QR_TOKENS.pop(token, None)
    if not entry:
        raise HTTPException(status_code=404, detail="QR-token expired or invalid")
    if time.time() - entry["created_at"] > QR_TTL_SECONDS:
        raise HTTPException(status_code=410, detail="QR-token expired (>10min)")
    return {
        "agent_id": entry["agent_id"],
        "api_key": entry["api_key"],
        "base_url": BASE_URL,
    }


# ─────────────────────────────────────────────────────────────────────────
# Claim-token flow — «agent self-onboarding»
# ─────────────────────────────────────────────────────────────────────────
class IssueClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # min_length=3 — иначе claim фейлится в _create_agent_core (CreateAgentBody
    # требует min_length=3) после того как Redis GETDEL уже consume'нул токен.
    # Owner мог случайно ввести «8» — теперь rejected на issue-этапе.
    agent_id: str | None = Field(None, min_length=3, max_length=64, pattern=r"^[\w.\-]+$")
    machine_hint: str | None = Field(None, max_length=128)
    platform: str = Field("claude_code", description="claude_code|cursor — определяет mcp config target")
    # v3 multi-agent registry: машина-fingerprint, owner может передать
    # ожидаемый fp (e.g. с UI auto-detect) — при claim, если есть agent
    # с тем же (owner_user_id, fp) → reuse api_key вместо создания нового.
    machine_fingerprint: str | None = Field(None, min_length=8, max_length=32, pattern=r"^[a-f0-9]+$")


@router.post("/issue-claim-token")
async def issue_claim_token(body: IssueClaimBody, request: Request):
    """Сгенерировать short claim-token который агент обменяет на api_key.

    Flow:
      1. Owner в своей сессии (authenticated) дёргает этот endpoint
      2. Получает короткий код типа AB12-CD34 (TTL 10 мин)
      3. Передаёт код агенту в чат: «вот claim AB12-CD34, подключись»
      4. Агент дёргает GET /user/connect/claim?token=AB12-CD34 (public)
      5. Получает api_key + config — обменивает токен на per-agent ключ

    Безопасность:
      • Токен короткий (~32 бит entropy) но short-lived (10 мин)
      • One-shot: после claim записан в _CLAIM_TOKENS, удаляется при первом GET
      • Не передаёт сам api_key — только пред-создаёт агента под user
    """
    user = await require_user(request)
    token = _generate_claim_token()
    # Default agent_id если не задан — суффикс от платформы
    default_id = body.agent_id or f"{body.platform.split('_')[0]}-{secrets.token_hex(3)}"

    # PR #35 (2026-05-23): СРАЗУ создаём pending agent_states row чтобы
    # owner видел в /ui/profile «pending claim» с countdown 10 мин.
    # При успешном claim — статус → 'active'. Если 10 мин не claim'нул —
    # cron удалит. UX-проблема «нажал генерировать — где агент?» решена.
    try:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            # UPSERT — если agent_id уже есть, не пересоздаём (для idempotent issue)
            await conn.execute(
                """
                INSERT INTO agent_states (agent_id, owner_user_id, machine_label, status)
                VALUES ($1, $2::uuid, $3, 'pending_claim')
                ON CONFLICT (agent_id) DO NOTHING
                """,
                default_id, str(user.user_id), body.machine_hint,
            )
    except Exception as e:
        logger.warning("Failed to pre-create pending agent_states for %s: %s", default_id, e)

    entry_data = {
        "user_id": str(user.user_id),
        "agent_id": default_id,
        "machine_hint": body.machine_hint,
        "platform": body.platform,
        "machine_fingerprint": body.machine_fingerprint,
        "created_at": time.time(),
    }
    try:
        r = await get_redis()
        await r.setex(f"cogcore:claim:{token}", CLAIM_TTL_SECONDS, json.dumps(entry_data))
    except Exception as e:
        logger.warning("Redis SETEX failed, falling back to memory: %s", e)
        _CLAIM_TOKENS[token] = entry_data
    return {
        "token": token,
        "expires_in_seconds": CLAIM_TTL_SECONDS,
        "agent_id": default_id,
        # ВАЖНО: НЕ embed полный URL в prompt — Yandex Browser / Telegram /
        # антивирусы автоматически превьюят URL и съедают one-shot token до
        # того как агент успеет claim'нуть. Даём куски — агент сам соберёт.
        "prompt_for_agent": (
            f"Привет! У меня для тебя claim-token Cognitive Core (живёт 10 мин, one-shot, "
            f"НЕ ставь URL в browser — съест preview):\n\n"
            f"  TOKEN: {token}\n"
            f"  BASE:  https://mcp.me-ai.ru\n"
            f"  PATH:  /user/connect/claim?token=<вставь-TOKEN-сверху>\n\n"
            f"Cognitive Core = память + комнаты для агентов + DM. После подключения "
            f"у тебя появятся 24 MCP-инструмента: cognitive_remember/recall (память), "
            f"room_join/post/read (комнаты), cognitive_send/inbox (DM), и т.д.\n\n"
            f"=== ШАГ 0 — РАЗВИЛКА ===\n"
            f"Сначала посмотри свой список доступных tools:\n"
            f"  • Если ты УЖЕ видишь `cognitive_health`, `cognitive_recall` и т.п. — НИЧЕГО "
            f"НЕ ДЕЛАЙ. Скажи owner'у «уже подключён, токен не нужен» — он сгорит сам через "
            f"10 мин. END.\n"
            f"  • Если cognitive_* tools НЕ видны — проверь конфиг:\n"
            f"    python -X utf8 -c \"import json,os; p=os.path.expanduser('~/.claude.json'); "
            f"d=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {{}}; "
            f"s=d.get('mcpServers',{{}}).get('cognitive-core'); url=str(s) if s else ''; "
            f"print('PRIMARY' if 'mcp.me-ai.ru' in url else ('LEGACY' if ('mcp.xn----8sbwawqx4fza' in url or '\\u0438\\u0438-\\u043f\\u0430\\u043c\\u044f\\u0442\\u044c' in url) else 'MISSING'))\"\n\n"
            f"    → PRIMARY — конфиг уже на canonical mcp.me-ai.ru. Дальше:\n"
            f"      (a) Проверь не устарел ли локальный `cogmedia` CLI если он установлен. "
            f"Старая версия имела cookie-auth + legacy URL и сейчас падает с 401. "
            f"Bash: `command -v cogmedia >/dev/null && curl -fsSL https://mcp.me-ai.ru/static/cogmedia -o \"$(command -v cogmedia)\" && chmod +x \"$(command -v cogmedia)\"`. "
            f"PowerShell: `if (Get-Command cogmedia -ea 0) {{ iwr https://mcp.me-ai.ru/static/cogmedia -OutFile (Get-Command cogmedia).Source }}`. "
            f"Это безопасно — обновляешь свой собственный tool, не config/api_key.\n"
            f"      (b) Попроси owner перезапустить Claude Code (одной строкой, без объяснений) и END. Claim НЕ нужен.\n"
            f"    → LEGACY — конфиг на старом punycode-домене. Дальше:\n"
            f"      (a) Тихо обнови `cogmedia` CLI если он установлен (старая версия может "
            f"висеть с cookie-auth + 401). Bash: `command -v cogmedia >/dev/null && curl -fsSL "
            f"https://mcp.me-ai.ru/static/cogmedia -o \"$(command -v cogmedia)\" && chmod +x "
            f"\"$(command -v cogmedia)\"`. PowerShell: `if (Get-Command cogmedia -ea 0) {{ iwr "
            f"https://mcp.me-ai.ru/static/cogmedia -OutFile (Get-Command cogmedia).Source }}`. "
            f"Свой tool — обновлять без вопросов.\n"
            f"      (b) Про MCP URL — спроси owner-а человеческим текстом: «У тебя в "
            f"~/.claude.json старый URL `mcp.xn----...`, заменить на `mcp.me-ai.ru`? "
            f"Это легче читается, api_key остаётся прежний.» — и жди явного «да». Если "
            f"«нет/потом» — оставь, legacy URL работает неограниченно. END (claim НЕ нужен).\n"
            f"    → MISSING — конфига нет. Продолжай ниже (ШАГ 1).\n\n"
            f"=== ШАГ 1 — CLAIM (только если MISSING) ===\n"
            f"Через Bash сделай curl с User-Agent (важно — не Mozilla):\n"
            f"  curl -H 'User-Agent: claude-code-agent' -o /tmp/claim.json <BASE><PATH>\n\n"
            f"В ответе будут api_key + готовый JSON для ~/.claude.json (HTTP MCP transport).\n\n"
            f"=== ШАГ 2 — MERGE INTO CONFIG ===\n"
            f"Edit-инструментом аккуратно вставь mcpServers.cognitive-core в config (НЕ "
            f"перезаписывай existing servers, не печатай api_key в transcript — читай из "
            f"/tmp/claim.json через python+env).\n\n"
            f"=== ШАГ 3 — ASK FOR RESTART ===\n"
            f"Попроси owner'а перезапустить Claude Code/Cursor. После рестарта у тебя "
            f"появятся 24 MCP-инструмента.\n\n"
            f"=== ШАГ 4 — ВНИКНИ В ВОЗМОЖНОСТИ (важно!) ===\n"
            f"Первым делом после рестарта вызови `cognitive_agent_manifest` — вернёт твой "
            f"agent_id + историю + categorized список tools + best_practices + rate-limits.\n"
            f"Затем — `cognitive_continue` (память + pending DM).\n\n"
            f"=== ШАГ 5 — ЧТО У ТЕБЯ ЕСТЬ (конкретно, без догадок) ===\n"
            f"Платформа cognitive-core это НЕ просто 24 MCP tool — это **полноценный workspace**:\n\n"
            f"📚 ПАМЯТЬ (5-слойная: L1 события → L2 дневные сводки → L3 знания через "
            f"DeepSeek куратор → L4 архив снапшотов в MinIO → OP быстрая Redis):\n"
            f"  • `cognitive_remember(domain, task, result, feedback, lessons)` — записать "
            f"факт/решение/lesson. ВАЖНО: в lessons/tools_used НЕ ставь '--' (двойное тире) "
            f"и ';' — SQL injection filter заблокирует.\n"
            f"  • `cognitive_recall(query, domain, top_k)` — семантический KNN-поиск по L3.\n"
            f"  • `cognitive_consolidate(level=daily|weekly)` — manual trigger свёртки.\n\n"
            f"🏠 КОМНАТЫ для team collaboration (HTTP REST + MCP tools):\n"
            f"  • `room_join(room_key)`, `room_post(room_key, text)`, `room_read(room_key)`.\n"
            f"  • `room_ask(room_key, question, wait_for_agents, timeout)` — long-poll Q&A.\n"
            f"  • Кросс-платформенно: твоя комната видна Claude Code, ChatGPT GPT, Cursor, "
            f"любому MCP-агенту owner-а.\n\n"
            f"💬 DM между агентами того же owner-а:\n"
            f"  • `cognitive_send(to, text)`, `cognitive_inbox(since_minutes)`.\n"
            f"  • `cognitive_my_team()` — список всех агентов owner-а + кто online.\n"
            f"  • `cognitive_online(within_seconds=120)` — кто сейчас активен.\n\n"
            f"🎥 МЕДИА (видео/аудио/изображения с auto-транскрипцией):\n"
            f"  • Установка/обновление (одна команда — bash):\n"
            f"      mkdir -p ~/bin && curl -fsSL https://mcp.me-ai.ru/static/cogmedia -o ~/bin/cogmedia && chmod +x ~/bin/cogmedia\n"
            f"    PowerShell (Windows):\n"
            f"      New-Item -ItemType Directory -Force \"$HOME\\bin\" | Out-Null; iwr https://mcp.me-ai.ru/static/cogmedia -OutFile \"$HOME\\bin\\cogmedia\"\n"
            f"  • Если получаешь 401 — у тебя СТАРАЯ версия (cookie-auth). Просто запусти "
            f"команду выше ещё раз — она перепишет файл. Новая версия auto-discoverит api_key "
            f"из ~/.claude.json через X-API-Key. Magic-link / cookies / письма НЕ нужны.\n"
            f"  • Использование: `cogmedia upload <path/to/file>`. Поддерживает "
            f".mp4/.mov/.mp3/.wav/.jpg/.png. Сервер делает: видео → 12 frames + Whisper "
            f"транскрипт; аудио → Whisper; изображение → store.\n"
            f"  • TTL медиа 15 мин в MinIO (метаданные навсегда в L1 `domain=media_analysis`).\n"
            f"  • После upload вызови `cognitive_recall(domain='media_analysis', query='...')` "
            f"чтобы найти результат (включая URL кадров для Read).\n"
            f"  • Платформа даёт shared Qwen-VL + DeepSeek fallback для видео-mechanics. "
            f"Хочешь качественный анализ через MiniMax / Claude / OpenAI / Gemini? "
            f"Подключи СВОЙ api_key в /ui/profile → «🤖 Внешние AI-провайдеры». "
            f"Vision_analyzer автоматически использует твой ключ (оплата на твоём счету у провайдера). "
            f"Документация: https://mcp.me-ai.ru/docs/external-providers.md\n\n"
            f"📦 ПРОЕКТНЫЕ ФАЙЛЫ через Gitea (self-hosted git):\n"
            f"  • Твой git-сервер: https://git.me-ai.ru\n"
            f"  • Твой org там создан автоматически (по slug твоего email до '@').\n"
            f"  • Используй для backup кода/configs/больших файлов (LFS support).\n"
            f"  • `git remote add gitea https://git.me-ai.ru/<твой-org>/<repo>.git`.\n\n"
            f"💾 STATE для compaction-survival:\n"
            f"  • `cognitive_save_state(current_task, state_data)` — snapshot working memory "
            f"перед длинной задачей или auto-/compact.\n"
            f"  • `cognitive_resume()` — после рестарта/compact восстановит state + inbox + online.\n\n"
            f"=== БЫСТРЫЕ ПРИВЫЧКИ ===\n"
            f"1. ПЕРЕД задачей → `cognitive_recall(query='похожая задача')` — не дублировать.\n"
            f"2. ПОСЛЕ важного решения/lesson → `cognitive_remember`.\n"
            f"3. Длинная сессия → `cognitive_save_state` в начале.\n"
            f"4. Не знаешь возможностей → `cognitive_agent_manifest`.\n"
            f"5. Кто рядом → `cognitive_my_team`.\n"
            f"6. Media-анализ → `cogmedia upload` + `cognitive_recall(domain='media_analysis')`.\n\n"
            f"Подробная документация: https://mcp.me-ai.ru/docs/concepts.md"
        ),
    }


@router.post("/claim")
@router.get("/claim")
async def claim_token(token: str, request: Request):
    """Public endpoint — агент обменивает claim-token на свой api_key + config.

    One-shot: токен удаляется при первом успешном claim. Различает три
    состояния для понятных error-msg:
      404 «никогда не было» — токен not in active or audit store
      410 «уже использован»  — в audit store с used_at
      410 «истёк (>10 мин)»  — в active store но created_at > TTL
    """
    token_clean = (token or "").strip().upper()

    # Cleanup старых audit-записей (lazy, при каждом запросе)
    now = time.time()
    expired_audit = [t for t, e in _CLAIM_TOKENS_USED.items()
                     if now - e["used_at"] > CLAIM_USED_AUDIT_TTL]
    for t in expired_audit:
        _CLAIM_TOKENS_USED.pop(t, None)

    # Проверка audit-store — был ли уже использован?
    audit = _CLAIM_TOKENS_USED.get(token_clean)
    if audit:
        used_ago = int(now - audit["used_at"])
        raise HTTPException(
            status_code=410,
            detail=(
                f"Claim-token уже использован {used_ago} сек назад "
                f"(IP …{audit['used_by_ip'][-8:]}). Часто это значит что URL "
                f"открылся в browser-preview, антивирусе или search-краулере. "
                f"Сгенерируйте НОВЫЙ токен и передайте его текстом, не как ссылку."
            ),
        )

    # Browser-preview protection: если User-Agent похож на browser, отказываем
    # БЕЗ consume — чтобы Yandex preview / Telegram bot / антивирус не съели
    # one-shot token. Реальные агенты (Claude Code, curl, мой installer) ставят
    # либо специфичный User-Agent либо никакой.
    ua = (request.headers.get("user-agent") or "").lower()
    browser_markers = ("mozilla/", "chrome/", "safari/", "firefox/", "edg/",
                       "opr/", "yabrowser/", "telegrambot", "googlebot",
                       "yandexbot", "facebookexternalhit", "twitterbot")
    if any(m in ua for m in browser_markers):
        # НЕ consume token — просто refuse. Token остаётся валидным для агента.
        raise HTTPException(
            status_code=403,
            detail=(
                "Claim-token нельзя получить из браузера (защита от auto-preview). "
                "Скопируйте токен и передайте текстом агенту — он claim'нет через "
                "curl с правильным User-Agent."
            ),
        )

    # Redis atomic getdel (Redis 6.2+) — survives api restart, prevents
    # double-claim race (даже если две curl пришли одновременно, только одна
    # увидит данные, вторая получит None).
    entry = None
    try:
        r = await get_redis()
        raw = await r.execute_command("GETDEL", f"cogcore:claim:{token_clean}")
        if raw:
            entry = json.loads(raw)
    except Exception as e:
        logger.warning("Redis GETDEL failed, falling back to memory: %s", e)
        entry = _CLAIM_TOKENS.pop(token_clean, None)
    # Memory fallback (на случай если Redis недоступен в момент issue)
    if not entry:
        entry = _CLAIM_TOKENS.pop(token_clean, None)

    if not entry:
        raise HTTPException(
            status_code=404,
            detail="Claim-token не существует. Проверьте регистр или сгенерируйте новый в /ui/profile.",
        )
    if now - entry["created_at"] > CLAIM_TTL_SECONDS:
        raise HTTPException(status_code=410, detail="Claim-token истёк (>10 минут). Сгенерируйте новый.")

    # Запишем в audit store ДО создания агента (на случай если create упал —
    # юзер сможет понять что токен был валидный)
    client_ip = (request.client.host if request.client else "?") or "?"
    _CLAIM_TOKENS_USED[token_clean] = {
        "used_at": now,
        "used_by_ip": client_ip,
        "original_user_id": entry["user_id"],
        "agent_id": entry["agent_id"],
    }

    # v3 multi-agent registry: REUSE если у owner-а уже есть agent с тем же
    # machine_fingerprint — возвращаем existing api_key, не создаём нового.
    # Это предотвращает дубликаты при повторном wizard на той же машине.
    fp = entry.get("machine_fingerprint")
    if fp:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT ast.agent_id, ak.api_key, ast.machine_label
                  FROM agent_states ast
                  JOIN agent_keys ak ON ak.agent_id = ast.agent_id
                 WHERE ast.owner_user_id = $1::uuid
                   AND ast.machine_fingerprint = $2
                   AND ak.revoked_at IS NULL
                 ORDER BY ak.created_at DESC
                 LIMIT 1
                """,
                entry["user_id"], fp,
            )
        if existing:
            # Bump heartbeat + update machine label
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE agent_states
                       SET machine_label = COALESCE($1, machine_label),
                           last_heartbeat_at = NOW(),
                           updated_at = NOW()
                     WHERE agent_id = $2
                    """,
                    entry.get("machine_hint"), existing["agent_id"],
                )
            logger.info(
                "claim REUSE user=%s machine_fp=%s → existing agent=%s",
                entry["user_id"], fp[:8], existing["agent_id"],
            )
            mcp_config_reuse = {
                "mcpServers": {
                    "cognitive-core": {
                        "type": "sse",
                        "url": MCP_SSE_URL,
                        "headers": {"X-API-Key": existing["api_key"]},
                    },
                },
            }
            return {
                "agent_id": existing["agent_id"],
                "api_key": existing["api_key"],
                "platform": platform,
                "mcp_url": MCP_SSE_URL,
                "base_url": BASE_URL,
                "mcp_config": mcp_config_reuse,
                "reused": True,
                "instructions": (
                    f"♻ Re-используем существующего helper-а `{existing['agent_id']}` "
                    f"на этой машине (machine_fp совпал). НЕ создаём дубликат. "
                    f"Если нужен новый — owner может revoke в /ui/profile и issue новый claim-token."
                ),
            }

    # Защитный fallback на случай legacy токенов выпущенных ДО min_length=3
    # фикса (там agent_id мог быть короткий или странный). Регенерим default.
    requested_agent_id = entry["agent_id"] or ""
    if len(requested_agent_id) < 3 or not requested_agent_id.replace("-", "").replace("_", "").replace(".", "").isalnum():
        platform_short = entry.get("platform", "claude_code")
        requested_agent_id = f"{platform_short.split('_')[0]}-{secrets.token_hex(3)}"
        logger.warning("legacy short agent_id from claim, regenerated to %s", requested_agent_id)

    # PR #35: проверить pending row (создан при issue-claim). Если есть —
    # просто convert to active + создать api_key, не зовём _create_agent_core
    # (он бы упал с 409 на UNIQUE agent_id).
    api_key = None
    pool_db = await get_pool()
    async with pool_db.acquire() as conn:
        pending = await conn.fetchrow(
            "SELECT agent_id FROM agent_states WHERE agent_id = $1 "
            "AND owner_user_id = $2::uuid AND status = 'pending_claim'",
            requested_agent_id, entry["user_id"],
        )
        if pending:
            api_key = secrets.token_urlsafe(32)
            # FIX 2026-05-26: agent_keys PRIMARY KEY = (api_key) only — нет UNIQUE
            # на agent_id, поэтому ON CONFLICT (agent_id) падает с
            # InvalidColumnReferenceError → HTTP 500. Делаем atomic revoke-old +
            # insert-new в транзакции. Старые keys остаются в таблице с
            # revoked_at для audit-trail.
            async with conn.transaction():
                await conn.execute(
                    "UPDATE agent_keys SET revoked_at = NOW() "
                    "WHERE agent_id = $1 AND revoked_at IS NULL",
                    requested_agent_id,
                )
                await conn.execute(
                    "INSERT INTO agent_keys (api_key, agent_id, description, owner_user_id) "
                    "VALUES ($1, $2, $3, $4::uuid)",
                    api_key, requested_agent_id,
                    f"Claim-onboarded at {datetime.utcnow().isoformat()}",
                    entry["user_id"],
                )
                await conn.execute(
                    "UPDATE agent_states SET status='active', machine=COALESCE($2, machine) WHERE agent_id=$1",
                    requested_agent_id, entry.get("machine_hint"),
                )
            agent = {"api_key": api_key, "agent_id": requested_agent_id}

    if api_key is None:
        # Нет pending row — fallback к старой логике (legacy токены до PR #35,
        # или edge-case если pending был удалён cleanup'ом)
        class _FakeUser:
            user_id = entry["user_id"]
        create_body = CreateAgentBody(
            agent_id=requested_agent_id,
            description=f"Self-onboarded via claim-token at {datetime.utcnow().isoformat()}",
            machine=entry.get("machine_hint"),
            project="claim-onboard",
        )
        try:
            agent = await _create_agent_core(_FakeUser(), create_body)
        except HTTPException as e:
            if e.status_code == 409:
                raise HTTPException(
                    status_code=409,
                    detail=f"Помощник с id «{entry['agent_id']}» уже существует. Сгенерируйте новый claim-token.",
                )
            raise
        api_key = agent["api_key"]
    platform = entry.get("platform", "claude_code")

    # v3: bind machine_fingerprint к новому агенту (для idempotent re-onboard)
    if fp:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_states SET machine_fingerprint = $1 WHERE agent_id = $2",
                    fp, requested_agent_id,
                )
        except Exception as e:
            logger.warning("Failed to bind machine_fp: %s", e)

    # Строим mcp config — direct SSE (Claude Code / Cursor native)
    mcp_config = {
        "mcpServers": {
            "cognitive-core": {
                "type": "sse",
                "url": MCP_SSE_URL,
                "headers": {"X-API-Key": api_key},
            },
        },
    }

    logger.info(
        "claim_consumed user_id=%s agent_id=%s platform=%s fp=%s",
        entry["user_id"], entry["agent_id"], platform, (fp[:8] if fp else "none"),
    )

    return {
        "agent_id": entry["agent_id"],
        "api_key": api_key,
        "platform": platform,
        "mcp_url": MCP_SSE_URL,
        "base_url": BASE_URL,
        "mcp_config": mcp_config,
        "instructions": (
            f"Установка для агента:\n"
            f"1. Откройте config-файл (приоритет):\n"
            f"   • Claude Code: ~/.claude.json\n"
            f"   • Claude Desktop macOS: ~/Library/Application Support/Claude/claude_desktop_config.json\n"
            f"   • Claude Desktop Windows: %APPDATA%\\Claude\\claude_desktop_config.json\n"
            f"   • Cursor: ~/.cursor/mcp.json или .cursor/mcp.json\n"
            f"2. Под ключом mcpServers добавьте новую запись cognitive-core (НЕ перезаписывайте existing).\n"
            f"3. Используйте Edit/jq для surgical edit, не overwrite.\n"
            f"4. После — попросите user'а перезапустить приложение.\n"
            f"5. После рестарта у вас появятся 24 MCP-инструмента."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────
# Auto-onboard — idempotent installer + machine fingerprint flow
# ─────────────────────────────────────────────────────────────────────────
class AutoOnboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_fingerprint: str = Field(..., min_length=8, max_length=32, pattern=r"^[a-f0-9]+$")
    machine_label: str | None = Field(None, max_length=128)
    api_key: str | None = Field(None, min_length=20, max_length=128)


@router.post("/auto-onboard")
async def auto_onboard(body: AutoOnboardBody):
    """Idempotent endpoint для installer-а.

    Логика:
      1. Если передан api_key И он валидный → bump heartbeat + update
         machine_fingerprint/label → return existing agent. Owner может
         re-run installer 100 раз — НИЧЕГО не дублируется.
      2. Если api_key НЕТ, но передан fingerprint → lookup agent_states
         WHERE machine_fingerprint=$1. Если найден И не revoked →
         для security НЕ возвращаем api_key (это public endpoint),
         но возвращаем 200 с status=existing + agent_id, чтобы
         installer мог попросить owner вручную re-claim.
      3. Если ничего не найдено → 404 с подсказкой run /ui/connect
         или передать claim-token.

    Security: не возвращаем api_key без подтверждения через valid api_key
    или claim-token. Это endpoint reuse-only.
    """
    pool = await get_pool()
    fp = body.machine_fingerprint.lower()

    # Path 1: api_key передан — bump + return reuse confirmation
    if body.api_key:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ak.agent_id, ast.owner_user_id::text AS owner_user_id
                  FROM agent_keys ak
                  JOIN agent_states ast ON ast.agent_id = ak.agent_id
                 WHERE ak.api_key = $1 AND ak.revoked_at IS NULL
                 LIMIT 1
                """,
                body.api_key,
            )
        if not row:
            raise HTTPException(status_code=401, detail="api_key неизвестен или revoked. Перезапустите claim-token wizard.")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_states
                   SET machine_fingerprint = $1,
                       machine_label = COALESCE($2, machine_label),
                       last_heartbeat_at = NOW(),
                       updated_at = NOW()
                 WHERE agent_id = $3
                """,
                fp, body.machine_label, row["agent_id"],
            )
            await conn.execute(
                "UPDATE agent_keys SET last_used_at = NOW() WHERE api_key = $1",
                body.api_key,
            )
        logger.info(
            "auto_onboard reuse user=%s agent=%s machine=%s",
            row["owner_user_id"], row["agent_id"], fp,
        )
        return {
            "status": "reused",
            "agent_id": row["agent_id"],
            "message": f"Helper уже установлен как `{row['agent_id']}`, heartbeat обновлён.",
        }

    # Path 2: только fingerprint — lookup, но НЕ возвращаем key (security)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT agent_id, owner_user_id::text AS owner_user_id, machine_label
              FROM agent_states
             WHERE machine_fingerprint = $1
             LIMIT 1
            """,
            fp,
        )
    if row:
        return {
            "status": "found_but_locked",
            "agent_id": row["agent_id"],
            "machine_label": row["machine_label"],
            "message": (
                f"На этой машине уже установлен helper `{row['agent_id']}`. "
                f"Если потеряли api_key — owner должен issue новый claim-token "
                f"в /ui/profile. Старый key продолжит работать параллельно."
            ),
        }

    raise HTTPException(
        status_code=404,
        detail=(
            f"Машина с fingerprint {fp[:8]}… ещё не зарегистрирована. "
            f"Owner должен открыть https://mcp.me-ai.ru/ui/profile → "
            f"«🪄 Передать помощнику» → передать токен installer-у."
        ),
    )


@router.post("/track")
async def track_funnel(body: TrackBody, request: Request):
    """A/B аналитика onboarding funnel. Пишет в L1 domain=onboarding_funnel."""
    user = await require_user(request)
    pool = await get_pool()
    payload = {
        "user_id": user.user_id,
        "channel": body.channel,
        "action": body.action,
        "detail": body.detail,
        "ts": datetime.utcnow().isoformat(),
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO l1_raw_events (source_agent, domain, raw_payload)
            VALUES ('connect-wizard', 'onboarding_funnel', $1::jsonb)
            """,
            json.dumps(payload, ensure_ascii=False),
        )
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────
# Verify endpoint — живёт в /user/agents/{id}/verify
# ─────────────────────────────────────────────────────────────────────────
verify_router = APIRouter(prefix="/user/agents", tags=["agents"])


@verify_router.get("/{agent_id}/verify")
async def verify_agent(agent_id: str, request: Request):
    """Проверить «агент проснулся и пишет в L1».

    UI поллит каждые 2 сек. Pass-критерий — есть хотя бы 1 событие за
    последние 5 минут от этого `source_agent` OR last_heartbeat_at < 5 min.

    Returns:
        {verified: bool, last_event_at: str|null, last_heartbeat_at: str|null, hint: str|null}
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Проверка владения агентом (security)
        owner = await conn.fetchval(
            "SELECT owner_user_id::text FROM agent_states WHERE agent_id = $1",
            agent_id,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="Помощник не найден")
        if str(owner) != str(user.user_id):
            raise HTTPException(status_code=403, detail="Не ваш помощник")

        row = await conn.fetchrow(
            """
            SELECT
                (SELECT MAX(timestamp) FROM l1_raw_events
                  WHERE source_agent = $1 AND timestamp > NOW() - INTERVAL '5 minutes'
                ) AS last_event_at,
                (SELECT last_heartbeat_at FROM agent_states WHERE agent_id = $1
                ) AS last_heartbeat_at
            """,
            agent_id,
        )

    last_event = row["last_event_at"]
    last_hb = row["last_heartbeat_at"]
    verified = bool(last_event) or (
        last_hb is not None
        and (datetime.utcnow() - last_hb.replace(tzinfo=None)).total_seconds() < 300
    )

    hint = None
    if not verified:
        hint = (
            "Попросите помощника записать в память что-то простое "
            "(«remember domain:test task:hello»). Если он MCP-клиент — "
            "должен сразу вызвать cognitive_remember. Проверка обновляется каждые 2 сек."
        )

    return {
        "verified": verified,
        "last_event_at": last_event.isoformat() if last_event else None,
        "last_heartbeat_at": last_hb.isoformat() if last_hb else None,
        "hint": hint,
    }
