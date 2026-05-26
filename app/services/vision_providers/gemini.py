"""Google Gemini vision provider.

Endpoint: https://generativelanguage.googleapis.com/v1beta
Model:    gemini-2.0-flash (fast + cheap)
Format:   generateContent с inline_data (base64) ИЛИ file_data (URI).

Для нашего use-case кадры — публичные URL'ы. Gemini поддерживает
fileData with fileUri через File API, но проще fetch'нуть base64
inline_data. Для 12 frames ~600KB total — приемлемо.

Где взять key: https://ai.google.dev/  (Google AI Studio)
Stack: ~$0.001-0.005 per 12-frame video на gemini-2.0-flash.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.0-flash"


async def _fetch_image_b64(url: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Скачать картинку и вернуть (base64, mime). None если 4xx/5xx."""
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return base64.b64encode(r.content).decode("ascii"), mime
    except httpx.HTTPError:
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

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_MODEL

    # Fetch frames + build content parts
    parts = [{"text": user_prompt}]
    async with httpx.AsyncClient(timeout=timeout) as client:
        for url in frame_urls:
            res = await _fetch_image_b64(url, client)
            if res is None:
                continue
            b64, mime = res
            parts.append({
                "inline_data": {"mime_type": mime, "data": b64}
            })

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]} if system_prompt else None,
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_output_tokens,
            },
        }
        # Strip None
        if payload["system_instruction"] is None:
            payload.pop("system_instruction")

        # Gemini принимает api_key как query-param
        url_endpoint = f"{base}/models/{model}:generateContent?key={api_key}"
        try:
            r = await client.post(url_endpoint, json=payload,
                                  headers={"Content-Type": "application/json"})
        except httpx.TimeoutException:
            return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
        except httpx.HTTPError as e:
            return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code != 200:
        body_preview = r.text[:200]
        recommend = r.status_code in (401, 403, 429)
        logger.warning("gemini non-200 status=%d body=%s", r.status_code, body_preview)
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend,
        }
    try:
        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return {"error": "no candidates in response", "fallback_recommended": False}
        cand_parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p.get("text", "") for p in cand_parts if "text" in p]
        mechanics = " ".join(text_parts).strip()
        usage = data.get("usageMetadata", {})
        return {
            "mechanics_summary": mechanics,
            "model": model,
            "tokens_in": usage.get("promptTokenCount", 0),
            "tokens_out": usage.get("candidatesTokenCount", 0),
        }
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}", "fallback_recommended": False}


async def test_connection(api_key: str, *, base_url: Optional[str] = None,
                          model_name: Optional[str] = None, timeout: float = 10.0) -> dict:
    """Text-only ping для validate api_key (без image fetch)."""
    if not api_key:
        return {"ok": False, "message": "empty api_key"}
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_MODEL
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 5, "temperature": 0.1},
    }
    url_endpoint = f"{base}/models/{model}:generateContent?key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url_endpoint, json=payload,
                                  headers={"Content-Type": "application/json"})
    except httpx.TimeoutException:
        return {"ok": False, "message": f"timeout >{timeout}s"}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        return {"ok": True, "message": "ok"}
    if r.status_code in (400, 401, 403):
        # Gemini может вернуть 400 для invalid key вместо 401
        return {"ok": False, "message": f"auth_failed (HTTP {r.status_code})"}
    if r.status_code == 429:
        return {"ok": False, "message": "rate_limit"}
    return {"ok": False, "message": f"HTTP {r.status_code}"}
