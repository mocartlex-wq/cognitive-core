"""Anthropic Claude vision provider — Haiku/Sonnet.

Endpoint: https://api.anthropic.com/v1
Model:    claude-haiku-4-5 (cheap default) или claude-sonnet-4-5
Format:   Anthropic Messages API с `image` content type (url source).

Где взять key: https://console.anthropic.com/settings/keys
Stack: ~$0.005-0.015 per 12-frame video на haiku.

Note: Anthropic API требует header `anthropic-version: 2023-06-01`.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-haiku-4-5"
API_VERSION = "2023-06-01"


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

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_MODEL

    # Anthropic content array:
    # [{"type":"image","source":{"type":"url","url":"..."}}, ..., {"type":"text","text":"..."}]
    content = []
    for url in frame_urls:
        content.append({
            "type": "image",
            "source": {"type": "url", "url": url},
        })
    content.append({"type": "text", "text": user_prompt})

    payload = {
        "model": model,
        "max_tokens": max_output_tokens,
        "temperature": 0.2,
        "system": system_prompt,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/messages", json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code != 200:
        body_preview = r.text[:200]
        recommend = r.status_code in (401, 403, 429)
        logger.warning("claude non-200 status=%d body=%s", r.status_code, body_preview)
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend,
        }
    try:
        data = r.json()
        # Anthropic: content = [{"type":"text","text":"..."}]
        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        mechanics = " ".join(text_parts).strip()
        usage = data.get("usage", {})
        return {
            "mechanics_summary": mechanics,
            "model": model,
            "tokens_in": usage.get("input_tokens", 0),
            "tokens_out": usage.get("output_tokens", 0),
        }
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}", "fallback_recommended": False}


async def test_connection(api_key: str, *, base_url: Optional[str] = None,
                          model_name: Optional[str] = None, timeout: float = 10.0) -> dict:
    """Text-only ping для validate api_key."""
    if not api_key:
        return {"ok": False, "message": "empty api_key"}
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_MODEL
    payload = {
        "model": model,
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "ping"}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/messages", json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "message": f"timeout >{timeout}s"}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        return {"ok": True, "message": "ok"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": f"auth_failed (HTTP {r.status_code})"}
    if r.status_code == 429:
        return {"ok": False, "message": "rate_limit"}
    return {"ok": False, "message": f"HTTP {r.status_code}"}
