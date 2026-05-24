"""MiniMax (Hailuo) vision provider — chatcompletion_v2.

Endpoint: https://api.minimax.chat/v1
Model:    abab6.5s-chat (mm capable) — официальный multimodal endpoint
          для chatcompletion_v2 поддерживает images через content parts.

Где взять key: https://www.minimax.io/platform/keys (international)
              или https://api.minimax.chat/ (CN)
Stack: ~$0.04-0.08 per 12-frame video (один из самых дорогих).
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# International endpoint default — для CN заменить на api.minimax.chat
DEFAULT_BASE_URL = "https://api.minimax.io/v1"
DEFAULT_MODEL = "abab6.5s-chat"


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

    # MiniMax chatcompletion_v2 — OpenAI-compatible format с image_url
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
            # MiniMax публичный endpoint — /text/chatcompletion_v2
            r = await client.post(f"{base}/text/chatcompletion_v2",
                                  json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code != 200:
        body_preview = r.text[:200]
        recommend = r.status_code in (401, 403, 429)
        logger.warning("minimax non-200 status=%d body=%s", r.status_code, body_preview)
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend,
        }
    try:
        data = r.json()
        # MiniMax v2 формат: { "base_resp": {"status_code":0}, "choices":[{"message":{"content":"..."}}], "usage":{...} }
        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code") != 0:
            return {
                "error": f"minimax_err: {base_resp.get('status_msg', 'unknown')}",
                "fallback_recommended": False,
            }
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
            "tokens_in": usage.get("prompt_tokens", usage.get("total_tokens", 0)),
            "tokens_out": usage.get("completion_tokens", 0),
        }
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}", "fallback_recommended": False}


async def test_connection(api_key: str, *, base_url: Optional[str] = None,
                          model_name: Optional[str] = None, timeout: float = 10.0) -> dict:
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
            r = await client.post(f"{base}/text/chatcompletion_v2",
                                  json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "message": f"timeout >{timeout}s"}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        try:
            d = r.json()
            br = d.get("base_resp", {})
            if br.get("status_code") == 0:
                return {"ok": True, "message": "ok"}
            return {"ok": False, "message": f"minimax: {br.get('status_msg', 'unknown')}"}
        except Exception:
            return {"ok": True, "message": "ok (no body)"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": f"auth_failed (HTTP {r.status_code})"}
    if r.status_code == 429:
        return {"ok": False, "message": "rate_limit"}
    return {"ok": False, "message": f"HTTP {r.status_code}"}
