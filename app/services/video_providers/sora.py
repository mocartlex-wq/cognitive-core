"""OpenAI Sora video generation provider (preview, API на wait-list).

Scaffold 2026-05-26: ready для активации когда OpenAI даст public API.
По состоянию на сейчас (2026-05) Sora доступна только через ChatGPT Plus/Pro
UI; API endpoint /v1/videos/generations объявлен но не открыт всем.

Структура зеркалит kling.py для consistency. Когда OpenAI откроет:
  1. Заменить TODO-stubs ниже на реальные HTTP вызовы
  2. Verify response shape против real Sora docs
  3. Запустить tests/test_video_providers.py — структура уже готова
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "sora-1"


async def submit(
    api_key: str,
    prompt: str,
    *,
    image_url: Optional[str] = None,
    duration_sec: int = 5,
    aspect_ratio: str = "16:9",
    model_name: Optional[str] = None,
    timeout: float = 30.0,
    base_url: Optional[str] = None,
) -> dict:
    """Submit Sora generation task. Currently STUB — wait for OpenAI API GA."""
    if not api_key:
        return {"error": "missing api_key", "fallback_recommended": True}

    # TODO 2026-05-26: when OpenAI Sora API GA — uncomment:
    # endpoint = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/videos/generations"
    # payload = {"model": model_name or DEFAULT_MODEL, "prompt": prompt,
    #            "duration_seconds": duration_sec, "aspect_ratio": aspect_ratio}
    # if image_url: payload["reference_image"] = image_url
    # async with httpx.AsyncClient(timeout=timeout) as client:
    #     r = await client.post(endpoint, json=payload, headers={"Authorization": f"Bearer {api_key}"})
    # ... (parse response, return task_id)

    return {
        "error": "Sora API ещё не открыт публично (wait-list). Используйте Kling пока.",
        "fallback_recommended": True,
        "fallback_provider": "kling_video",
    }


async def poll(
    api_key: str,
    task_id: str,
    *,
    timeout: float = 10.0,
    base_url: Optional[str] = None,
) -> dict:
    """Poll Sora task. STUB."""
    return {
        "error": "Sora API не открыт. Это stub.",
        "status": "failed",
    }


async def test_connection(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """Test — пока всегда «not yet available»."""
    return {
        "ok": False,
        "message": "Sora API ещё не публичный. Подайте wait-list заявку: https://openai.com/sora",
    }
