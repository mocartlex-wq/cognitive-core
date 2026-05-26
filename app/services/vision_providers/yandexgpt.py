"""YandexGPT (Yandex Cloud Foundation Models) vision provider.

YandexGPT использует static API key + folder_id. Auth простой:
  Authorization: Api-Key AQVN...

Endpoints:
  API:   https://llm.api.cloud.yandex.net/foundationModels/v1/completion
  Vision: yandex-vision (preview model)

Tenant хранит свой ключ в формате:
  "{folder_id}|{api_key}"  — '|' как разделитель, чтобы tenant передал оба
  значения через один UI-input (User External Keys имеет одно поле).

Если ключ без '|' — считаем что только API-key, folder_id берём из ENV.

Где взять key:
  1. https://console.cloud.yandex.ru/ → IAM → Service Accounts → создать
     сервисный аккаунт с ролью ai.languageModels.user
  2. Создать API-key для этого аккаунта
  3. Folder ID — в Cloud Overview правой панели

Stoимость: ~0.4₽ per 1000 tokens (YandexGPT Pro). Vision-quota меньше, чем text.

ВАЖНО про vision: YandexGPT Vision (preview) на 2026-05 поддерживает только
ОДИН frame за вызов. Если frame_urls > 1 — берём средний (index N/2).
Это даёт worst quality но best compatibility — для лучшего vision используйте
Qwen или GigaChat-Pro.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1"
DEFAULT_VISION_MODEL = "yandex-vision/latest"
DEFAULT_TEXT_MODEL_FALLBACK = "gpt://b1g.../yandexgpt/latest"  # template — folder_id substituted
ENV_FOLDER_ID = os.environ.get("YANDEX_CLOUD_FOLDER_ID", "").strip()


def _parse_key(api_key: str) -> tuple[str, str]:
    """Парсим 'folder_id|api_key' или fallback: api_key + ENV folder_id.

    Returns: (folder_id, raw_api_key)
    """
    if "|" in api_key:
        folder_id, raw_key = api_key.split("|", 1)
        return folder_id.strip(), raw_key.strip()
    return ENV_FOLDER_ID, api_key.strip()


async def _fetch_image_bytes(url: str, timeout: float = 30.0) -> Optional[bytes]:
    """Download image from MinIO presigned URL → bytes (для base64 payload)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code != 200:
            logger.warning("yandexgpt: image fetch failed status=%d url=%s", r.status_code, url[:80])
            return None
        return r.content
    except httpx.HTTPError as e:
        logger.warning("yandexgpt: image fetch network: %s", type(e).__name__)
        return None


async def analyze(
    api_key: str,
    frame_urls: list[str],
    transcript: Optional[str],
    duration_seconds: Optional[float],
    *,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: float = 60.0,
    max_output_tokens: int = 800,
    system_prompt: str = "",
    user_prompt: str = "",
) -> dict:
    if not api_key:
        return {"error": "missing api_key", "fallback_recommended": True}
    if not frame_urls:
        return {"error": "no frames provided", "fallback_recommended": False}

    folder_id, raw_key = _parse_key(api_key)
    if not folder_id:
        return {
            "error": "yandex: missing folder_id (укажите 'folder_id|api_key' или ENV YANDEX_CLOUD_FOLDER_ID)",
            "fallback_recommended": True,
        }

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_VISION_MODEL

    # YandexGPT Vision preview принимает один frame за call. Берём средний.
    middle_idx = len(frame_urls) // 2
    chosen_url = frame_urls[middle_idx]
    image_bytes = await _fetch_image_bytes(chosen_url, timeout=15.0)
    if not image_bytes:
        return {
            "error": "yandex: не смогли загрузить frame для vision",
            "fallback_recommended": False,
        }
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    # Yandex SDK content format — стандарт «role/text» + images в content array
    payload = {
        "modelUri": f"gpt://{folder_id}/{model}",
        "completionOptions": {
            "stream": False,
            "temperature": 0.2,
            "maxTokens": str(max_output_tokens),
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {
                "role": "user",
                "text": user_prompt,
                # YandexGPT Vision preview spec — image как inline base64
                "images": [{"data": image_b64, "mimeType": "image/jpeg"}],
            },
        ],
    }
    headers = {
        "Authorization": f"Api-Key {raw_key}",
        "Content-Type": "application/json",
        "x-folder-id": folder_id,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/completion", json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code != 200:
        body_preview = r.text[:200]
        recommend = r.status_code in (401, 403, 429)
        logger.warning("yandexgpt non-200 status=%d body=%s", r.status_code, body_preview)
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend,
        }
    try:
        data = r.json()
        # Yandex response shape: {"result": {"alternatives": [{"message": {"text": ...}}], "usage": {...}}}
        result = data.get("result", {})
        alternatives = result.get("alternatives", [])
        if not alternatives:
            return {"error": "parse_error: no alternatives", "fallback_recommended": False}
        text = alternatives[0].get("message", {}).get("text", "").strip()
        usage = result.get("usage", {})
        return {
            "mechanics_summary": text,
            "model": model,
            "tokens_in": int(usage.get("inputTextTokens", 0)),
            "tokens_out": int(usage.get("completionTokens", 0)),
        }
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}", "fallback_recommended": False}


async def test_connection(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """Cheap test: short text-only completion → проверяет folder_id + api_key.

    Если ответил 200 — оба значения валидны.
    """
    if not api_key:
        return {"ok": False, "message": "empty api_key"}

    folder_id, raw_key = _parse_key(api_key)
    if not folder_id:
        return {"ok": False, "message": "missing folder_id (format: 'folder_id|api_key' или ENV YANDEX_CLOUD_FOLDER_ID)"}

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    # Text-only ping
    payload = {
        "modelUri": f"gpt://{folder_id}/yandexgpt-lite/latest",
        "completionOptions": {"stream": False, "temperature": 0.0, "maxTokens": "5"},
        "messages": [{"role": "user", "text": "ping"}],
    }
    headers = {
        "Authorization": f"Api-Key {raw_key}",
        "Content-Type": "application/json",
        "x-folder-id": folder_id,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/completion", json=payload, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        return {"ok": True, "message": "ok"}
    return {"ok": False, "message": f"http_{r.status_code}: {r.text[:120]}"}
