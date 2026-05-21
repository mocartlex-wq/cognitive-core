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
from app.security.middleware import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/user/connect", tags=["connect"])

# Production base URL — пишется в артефакты (mcpServers.url, OpenAPI server и т.д.)
BASE_URL = "https://mcp.xn----8sbwawqx4fza.xn--p1ai"
MCP_SSE_URL = f"{BASE_URL}/mcp/sse"
MCP_MESSAGES_URL = f"{BASE_URL}/mcp/messages"

# Short-lived in-memory QR token store (10min TTL). Для production-scale
# это переносится в Redis, но для wizard'а одного юзера за раз — ок.
_QR_TOKENS: dict[str, dict] = {}
QR_TTL_SECONDS = 600


# ─────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────
class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._\-]+$")
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
    """Конфиг для Claude Code / Claude Desktop / Cursor mcpServers."""
    config = {
        "mcpServers": {
            "cognitive-core": {
                "command": "npx",
                "args": ["-y", "mcp-remote", MCP_SSE_URL, "--header", f"X-API-Key: {api_key}"],
            },
        },
    }
    return {
        "kind": "mcp_json",
        "filename": "claude_mcp_config.json",
        "mime": "application/json",
        "content": json.dumps(config, indent=2, ensure_ascii=False),
        "instructions": (
            f"1. Откройте файл `~/.claude.json` (Claude Code) или `claude_desktop_config.json` "
            f"(Claude Desktop) или `.cursor/mcp.json` (Cursor).\n"
            f"2. Добавьте секцию `mcpServers.cognitive-core` из этого JSON в существующий config.\n"
            f"3. Перезапустите приложение.\n"
            f"4. У помощника появятся 24 MCP-инструмента: cognitive_remember, cognitive_recall, "
            f"cognitive_send, room_post и т.д.\n"
            f"5. agent_id вашего помощника: `{agent_id}` — это его «имя» в системе."
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
