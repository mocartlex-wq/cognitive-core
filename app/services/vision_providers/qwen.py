"""Qwen-VL provider — Alibaba DashScope (OpenAI-compatible).

Endpoint: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
Model:    qwen-vl-max-latest (multimodal)
Format:   OpenAI chat/completions с image_url content parts.

Где взять key: https://dashscope.console.aliyun.com/apiKey (sk-ws-...)
Stack ~$0.02-0.03 per 12-frame video.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-vl-max-latest"


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

    content = [{"type": "text", "text": user_prompt}]
    for url in frame_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_output_tokens,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code != 200:
        body_preview = r.text[:200]
        recommend_fallback = r.status_code in (401, 403, 429)
        # Безопасно логируем — без plaintext ключа
        logger.warning(
            "qwen non-200 status=%d body=%s recommend_fallback=%s",
            r.status_code, body_preview, recommend_fallback,
        )
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend_fallback,
        }

    try:
        data = r.json()
        msg = data["choices"][0]["message"]
        raw_content = msg.get("content", "")
        if isinstance(raw_content, list):
            mechanics = " ".join(
                p.get("text", "") for p in raw_content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        else:
            mechanics = str(raw_content).strip()
        usage = data.get("usage", {})
        return {
            "mechanics_summary": mechanics,
            "model": model,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
        }
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}", "fallback_recommended": False}


async def test_connection(api_key: str, *, base_url: Optional[str] = None,
                          model_name: Optional[str] = None, timeout: float = 10.0) -> dict:
    """Minimal-cost API call для validate api_key.

    Делаем text-only ping (НЕ vision) — дешевле и быстрее.
    """
    if not api_key:
        return {"ok": False, "message": "empty api_key"}
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
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
