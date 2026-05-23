"""Generator OpenAPI 3.1 yaml для ChatGPT Custom GPT Actions.

Описывает REST-endpoint-ы cognitive_api / rooms-server которые помощник
дёргает напрямую через X-API-Key (per-agent ключ из таблицы agent_keys).

Не путать с FastAPI auto-generated `/openapi.json` — там полный API сервера
со всеми admin/system endpoint-ами. Здесь — узкий профиль "agent capabilities":
память (remember/recall), DM-ы между агентами, комнаты, медиа.

Endpoint:
    GET /api/openapi/cognitive.yaml       — публичный (без auth) yaml
    GET /api/openapi/cognitive.json       — то же в JSON формате

Использование:
    1. Юзер создаёт Custom GPT в ChatGPT Plus
    2. Action → Import from URL → https://mcp.me-ai.ru/api/openapi/cognitive.yaml
    3. Authentication → API Key → header `X-API-Key` → ставит ключ агента
    4. ChatGPT теперь может вызывать cognitive_remember / recall / etc.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, JSONResponse

from app.api.mcp_protocol import TOOLS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/openapi", tags=["openapi"])


# ─────────────────────────────────────────────────────────────────────────
# OpenAPI 3.1 template
# ─────────────────────────────────────────────────────────────────────────
def _build_openapi_spec(base_url: str = "https://mcp.me-ai.ru") -> dict:
    """Собрать OpenAPI 3.1 спецификацию для Custom GPT Actions.

    Описывает 10 самых нужных endpoint-ов для агента:
      remember / recall / inbox / send / room_post / room_read / room_ask
      / room_join / image / video. Без admin / system / debug endpoint-ов.

    Args:
        base_url: production base URL (default mcp.me-ai.ru ASCII; legacy alias mcp.ии-память.рф/punycode).

    Returns:
        dict готовый к dump в JSON/yaml.
    """
    # Map MCP tool names → описание (берём verbatim из TOOLS как source of truth)
    tool_descriptions: dict[str, str] = {}
    for t in TOOLS:
        tool_descriptions[t["name"]] = t.get("description", "")

    def _desc(name: str, fallback: str = "") -> str:
        return tool_descriptions.get(name, fallback)

    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "Cognitive Core — agent capabilities",
            "description": (
                "REST API для AI-помощника: долгосрочная память (5-слойная "
                "консолидация), DM между агентами, виртуальные комнаты для "
                "коллаборации, анализ медиа (видео/картинки/аудио). "
                "Все запросы требуют header X-API-Key с persona-ключом помощника "
                "(выдаётся в /ui/connect)."
            ),
            "version": "1.0.0",
        },
        "servers": [{"url": base_url}],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": (
                        "Per-agent ключ. Получить: /ui/connect → выбрать платформу "
                        "ChatGPT → копировать. Хранится в Custom GPT Action authentication."
                    ),
                },
                "RoomKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Room-Key",
                    "description": (
                        "Ключ доступа к комнате (rk_...). Выдаётся при создании "
                        "комнаты или передаётся владельцем. Только для /rooms/* endpoint-ов."
                    ),
                },
            },
            "schemas": {
                "RememberRequest": {
                    "type": "object",
                    "required": ["domain", "task"],
                    "properties": {
                        "domain": {"type": "string", "description": "Предметная область, e.g. python_dev"},
                        "task": {"type": "string", "description": "Что было сделано"},
                        "result": {"type": "string", "description": "Каков результат"},
                        "feedback": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                        "lessons": {"type": "string", "description": "Извлечённые уроки"},
                        "tools_used": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "RecallRequest": {
                    "type": "object",
                    "required": ["query", "domain"],
                    "properties": {
                        "query": {"type": "string", "description": "Естественный язык — что ищем"},
                        "domain": {"type": "string", "description": "Сужение по домену"},
                        "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                        "include_tools": {"type": "boolean", "default": True},
                    },
                },
                "RecallResponse": {
                    "type": "object",
                    "properties": {
                        "patterns": {"type": "array", "items": {"type": "object"}},
                        "mistakes": {"type": "array", "items": {"type": "object"}},
                        "rules": {"type": "array", "items": {"type": "object"}},
                        "tools": {"type": "array", "items": {"type": "object"}},
                    },
                },
                "SendDMRequest": {
                    "type": "object",
                    "required": ["to", "text"],
                    "properties": {
                        "to": {"type": "string", "description": "agent_id получателя"},
                        "text": {"type": "string", "description": "Текст сообщения"},
                        "thread_id": {"type": "string", "description": "Опц. для треда"},
                    },
                },
                "InboxItem": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "from": {"type": "string"},
                        "text": {"type": "string"},
                        "timestamp": {"type": "string", "format": "date-time"},
                    },
                },
                "RoomPostRequest": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string"},
                        "agent_id": {"type": "string", "description": "Кто пишет (отображаемое имя в комнате)"},
                    },
                },
                "MediaUploadResponse": {
                    "type": "object",
                    "properties": {
                        "media_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["video", "image", "audio"]},
                        "url": {"type": "string", "description": "Публичный URL для скачивания"},
                        "transcript": {"type": "string", "description": "Whisper транскрипт (для видео/аудио)"},
                        "frames": {"type": "array", "items": {"type": "object"}, "description": "До 12 кадров (для видео)"},
                    },
                },
                "Error": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
            },
        },
        "security": [{"ApiKeyAuth": []}],
        "paths": {
            "/events": {
                "post": {
                    "operationId": "cognitive_remember",
                    "summary": "Запомнить опыт (L1 событие)",
                    "description": _desc("cognitive_remember"),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RememberRequest"},
                            },
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Событие записано",
                            "content": {"application/json": {"schema": {"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}}}},
                        },
                        "401": {"$ref": "#/components/schemas/Error"},
                    },
                },
            },
            "/recall": {
                "post": {
                    "operationId": "cognitive_recall",
                    "summary": "Найти релевантные знания",
                    "description": _desc("cognitive_recall"),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RecallRequest"},
                            },
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Результаты поиска",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecallResponse"}}},
                        },
                    },
                },
            },
            "/agents/message": {
                "post": {
                    "operationId": "cognitive_send",
                    "summary": "DM другому помощнику",
                    "description": _desc("cognitive_send", "Отправить direct message другому помощнику. Поле `to` — agent_id получателя."),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SendDMRequest"},
                            },
                        },
                    },
                    "responses": {"200": {"description": "Сообщение отправлено"}},
                },
            },
            "/agents/inbox": {
                "get": {
                    "operationId": "cognitive_inbox",
                    "summary": "Прочитать входящие DM",
                    "description": _desc("cognitive_inbox", "Получить непрочитанные сообщения от других помощников."),
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
                        {"name": "include_seen", "in": "query", "schema": {"type": "boolean", "default": False}},
                    ],
                    "responses": {
                        "200": {
                            "description": "Список сообщений",
                            "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/InboxItem"}}}},
                        },
                    },
                },
            },
            "/rooms/{room_id}/post": {
                "post": {
                    "operationId": "room_post",
                    "summary": "Написать в комнату",
                    "description": "Отправить сообщение в общую комнату. Требует X-Room-Key вместо X-API-Key.",
                    "security": [{"RoomKeyAuth": []}],
                    "parameters": [
                        {"name": "room_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RoomPostRequest"},
                            },
                        },
                    },
                    "responses": {"200": {"description": "Сообщение в комнате"}},
                },
            },
            "/rooms/{room_id}/messages": {
                "get": {
                    "operationId": "room_read",
                    "summary": "Прочитать сообщения комнаты",
                    "security": [{"RoomKeyAuth": []}],
                    "parameters": [
                        {"name": "room_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "since", "in": "query", "schema": {"type": "string", "format": "date-time"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                    ],
                    "responses": {"200": {"description": "Список сообщений комнаты"}},
                },
            },
            "/rooms/{room_id}/join": {
                "post": {
                    "operationId": "room_join",
                    "summary": "Войти в комнату",
                    "security": [{"RoomKeyAuth": []}],
                    "parameters": [
                        {"name": "room_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {"agent_id": {"type": "string"}, "platform": {"type": "string"}}},
                            },
                        },
                    },
                    "responses": {"200": {"description": "Присоединение зарегистрировано"}},
                },
            },
            "/api/media/image": {
                "post": {
                    "operationId": "analyze_image",
                    "summary": "Загрузить и проанализировать картинку",
                    "description": "Multipart upload PNG/JPG/WebP. Возвращает media_id + URL. Файл доступен публично через /api/media/frame/{key}.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}},
                            },
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Картинка загружена",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MediaUploadResponse"}}},
                        },
                    },
                },
            },
            "/api/media/video": {
                "post": {
                    "operationId": "analyze_video",
                    "summary": "Загрузить и проанализировать видео",
                    "description": "Multipart upload MP4/WebM/MOV. Извлекает 12 кадров через ffmpeg + Whisper транскрипцию. Возвращает frames[] с URL и transcript.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}},
                            },
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Видео проанализировано",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MediaUploadResponse"}}},
                        },
                    },
                },
            },
            "/api/media/audio": {
                "post": {
                    "operationId": "analyze_audio",
                    "summary": "Загрузить и транскрибировать аудио",
                    "description": "Multipart upload MP3/WAV/OGG. Только Whisper транскрипция, без кадров.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}},
                            },
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Аудио транскрибировано",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MediaUploadResponse"}}},
                        },
                    },
                },
            },
        },
    }
    return spec


# ─────────────────────────────────────────────────────────────────────────
# YAML serializer (минимальный — без pyyaml, для нулевой dep-нагрузки)
# ─────────────────────────────────────────────────────────────────────────
def _to_yaml(obj, indent: int = 0) -> str:
    """Простой YAML-сериализатор для dict/list/scalars (без pyyaml).

    Покрывает только наш OpenAPI-кейс. Не пытается обработать exotic
    типы (множества, кастомные классы) — для них падёт с TypeError.
    """
    sp = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            key = str(k)
            # ключи которые ChatGPT-парсер не любит в bare-form — quote их
            if any(c in key for c in (":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", "<", ">", "=", "!", "%", "@", "`")):
                key = f'"{key}"'
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{sp}{key}:")
                lines.append(_to_yaml(v, indent + 1))
            else:
                lines.append(f"{sp}{key}: {_yaml_scalar(v, indent + 1)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = []
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                # списочный элемент — `- ключ:` начинает блок
                rendered = _to_yaml(item, indent + 1)
                # первая строка получает дефис, остальные сдвигаются
                inner_lines = rendered.split("\n")
                first = inner_lines[0].lstrip()
                lines.append(f"{sp}- {first}")
                for ln in inner_lines[1:]:
                    lines.append(ln)
            else:
                lines.append(f"{sp}- {_yaml_scalar(item, indent + 1)}")
        return "\n".join(lines)
    return f"{sp}{_yaml_scalar(obj, indent)}"


def _yaml_scalar(v, indent: int) -> str:
    """Скаляр-в-YAML: bool, int/float, None, str. Strings всегда quoted."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if not s:
        return '""'
    # Многострочные строки — block scalar |
    if "\n" in s:
        sp = "  " * indent
        lines = s.split("\n")
        return "|-\n" + "\n".join(f"{sp}{ln}" for ln in lines)
    # Просто quote все строки — безопасно для ChatGPT YAML-parser
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────
@router.get("/cognitive.json")
async def get_openapi_json():
    """Cognitive Core OpenAPI 3.1 в JSON. Public, no auth — для шаринга URL."""
    spec = _build_openapi_spec()
    return JSONResponse(content=spec)


@router.get("/cognitive.yaml", response_class=PlainTextResponse)
async def get_openapi_yaml():
    """Cognitive Core OpenAPI 3.1 в YAML. Это что копируется в ChatGPT Custom GPT Actions."""
    spec = _build_openapi_spec()
    yaml_str = _to_yaml(spec)
    return PlainTextResponse(content=yaml_str, media_type="text/yaml; charset=utf-8")


def build_custom_gpt_openapi(agent_id: str = "", base_url: str = "https://mcp.me-ai.ru") -> str:
    """Reusable из connect.py — генерит yaml для конкретного агента.

    agent_id сейчас не embed-ится в spec (auth через X-API-Key который
    user копирует в ChatGPT отдельно). Параметр оставлен для future:
    можно генерить per-agent описание с примером промпта.
    """
    spec = _build_openapi_spec(base_url=base_url)
    return _to_yaml(spec)
