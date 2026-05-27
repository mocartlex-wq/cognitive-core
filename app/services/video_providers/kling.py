"""Kling.ai (Kuaishou) video generation provider.

API: https://api.kling.ai/v1
Docs: https://docs.kling.ai (公开 docs)
Auth: JWT Bearer signed HS256 с парой (access_key, secret_key)

Tenant хранит ключ как: `access_key|secret_key` (одной строкой через '|')
аналогично YandexGPT (folder_id|api_key). Это позволяет переиспользовать
user_external_keys table без миграции.

Pricing 2026-05: Kling-V1 ~ $0.10/sec, Kling-V1-Pro ~ $0.35/sec.
5-секундное видео = $0.50-1.75. Для блогинга — приемлемо.

ВАЖНО: Kling JWT генерируется client-side. Алгоритм:
    header  = {"alg":"HS256","typ":"JWT"}
    payload = {"iss": access_key, "exp": now+1800, "nbf": now-5}
    token   = HMAC-SHA256(secret_key, base64url(header) + "." + base64url(payload))

Submit endpoint асинхронный — возвращает task_id, видео генерируется 30-180s.
Caller poll'ит cognitive_video_status(task_id) пока status != 'completed'.

Scaffold 2026-05-26: framework готов, нужны real keys для verification end-to-end.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.kling.ai"
DEFAULT_MODEL = "kling-v1"  # cheap default; pro = "kling-v1-pro"


def _parse_key(api_key: str) -> tuple[str, str]:
    """Парсим 'access_key|secret_key'. Возвращает ('', '') если invalid."""
    if "|" not in api_key:
        return ("", "")
    parts = api_key.split("|", 1)
    return (parts[0].strip(), parts[1].strip())


def _b64url(data: bytes) -> str:
    """Base64-URL encoding без padding (per RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_jwt(access_key: str, secret_key: str, ttl_sec: int = 1800) -> str:
    """Generate JWT для Kling API auth. HS256 + custom payload."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": access_key, "exp": int(time.time()) + ttl_sec, "nbf": int(time.time()) - 5}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(secret_key.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url(sig)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


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
    """Submit text2video или image2video task. Возвращает task_id."""
    if not api_key:
        return {"error": "missing api_key", "fallback_recommended": True}
    if not prompt or not prompt.strip():
        return {"error": "prompt required", "fallback_recommended": False}

    access_key, secret_key = _parse_key(api_key)
    if not access_key or not secret_key:
        return {
            "error": "kling: формат ключа должен быть 'access_key|secret_key' (через '|')",
            "fallback_recommended": True,
        }

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    model = model_name or DEFAULT_MODEL
    token = _generate_jwt(access_key, secret_key)

    if image_url:
        endpoint = f"{base}/v1/videos/image2video"
        payload = {
            "model_name": model,
            "image": image_url,
            "prompt": prompt,
            "duration": str(duration_sec),
            "aspect_ratio": aspect_ratio,
        }
    else:
        endpoint = f"{base}/v1/videos/text2video"
        payload = {
            "model_name": model,
            "prompt": prompt,
            "duration": str(duration_sec),
            "aspect_ratio": aspect_ratio,
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(endpoint, json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code not in (200, 201, 202):
        body_preview = r.text[:200]
        recommend = r.status_code in (401, 403, 429)
        logger.warning("kling submit non-2xx status=%d body=%s", r.status_code, body_preview)
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend,
        }
    try:
        data = r.json()
        # Kling response: {"code": 0, "data": {"task_id": "..."}, "message": "ok"}
        if data.get("code") not in (0, None):
            return {
                "error": f"kling error code={data.get('code')}: {data.get('message','?')}",
                "fallback_recommended": False,
            }
        task_data = data.get("data", {})
        task_id = task_data.get("task_id") or task_data.get("id")
        if not task_id:
            return {"error": f"no task_id in response: {data}", "fallback_recommended": False}
        return {
            "task_id": task_id,
            "provider_status": "submitted",
            "model": model,
            "mode": "image2video" if image_url else "text2video",
            "estimated_duration_sec": duration_sec * 12,  # ~12x realtime для V1
        }
    except (KeyError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}", "fallback_recommended": False}


async def poll(
    api_key: str,
    task_id: str,
    *,
    timeout: float = 10.0,
    base_url: Optional[str] = None,
) -> dict:
    """Poll task status. Возвращает status + video_url когда completed."""
    if not api_key or not task_id:
        return {"error": "api_key + task_id required"}

    access_key, secret_key = _parse_key(api_key)
    if not access_key or not secret_key:
        return {"error": "kling: invalid key format (expected 'access_key|secret_key')"}

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    token = _generate_jwt(access_key, secret_key)

    # Kling: GET /v1/videos/text2video/{id} (общий для обоих режимов)
    endpoint = f"{base}/v1/videos/text2video/{task_id}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(endpoint, headers=headers)
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}"}

    if r.status_code != 200:
        return {
            "error": f"http_{r.status_code}: {r.text[:200]}",
            "status_code": r.status_code,
        }
    try:
        data = r.json()
        if data.get("code") not in (0, None):
            return {"error": f"kling error: {data.get('message','?')}"}
        task = data.get("data", {})
        task_status = task.get("task_status", "?").lower()
        # Map Kling statuses → unified
        status_map = {
            "submitted": "queued",
            "processing": "generating",
            "succeed": "completed",
            "failed": "failed",
        }
        unified_status = status_map.get(task_status, task_status)
        result: dict = {
            "status": unified_status,
            "provider_status": task_status,
            "progress_pct": int(task.get("progress", 0)),
        }
        if unified_status == "completed":
            videos = task.get("task_result", {}).get("videos", [])
            if videos:
                result["video_url"] = videos[0].get("url")
                result["duration_sec"] = videos[0].get("duration")
        elif unified_status == "failed":
            result["error_detail"] = task.get("task_status_msg", "unknown failure")
        return result
    except (KeyError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}"}


async def test_connection(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """Cheap test: GET account info с сгенерированным JWT.

    Kling не имеет dedicated /ping — используем GET /v1/videos/text2video
    (list endpoint, должен вернуть 200 с пустым массивом если ключ валиден).
    """
    if not api_key:
        return {"ok": False, "message": "empty api_key"}
    access_key, secret_key = _parse_key(api_key)
    if not access_key or not secret_key:
        return {"ok": False, "message": "invalid key format (expected 'access_key|secret_key')"}

    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    token = _generate_jwt(access_key, secret_key)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base}/v1/videos/text2video", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        return {"ok": True, "message": "ok"}
    return {"ok": False, "message": f"http_{r.status_code}: {r.text[:120]}"}
