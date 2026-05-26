"""Sber GigaChat Pro vision provider.

GigaChat использует OAuth2: tenant даёт нам client credentials (или scope-key),
мы exchange его на короткоживущий access_token (~30 мин), потом делаем
chat/completions.

Endpoints:
  Auth:  https://ngw.devices.sberbank.ru:9443/api/v2/oauth
  API:   https://gigachat.devices.sberbank.ru/api/v1
Model: GigaChat-Pro (vision-capable) или GigaChat-Max

Для simplicity: tenant хранит base64-encoded auth-key (как client_id:client_secret),
а мы используем его для запроса access_token. Кэшируем токен 25 мин.

Где взять key: https://developers.sber.ru/portal/products/gigachat-api
Stack: ~10₽ per video на GigaChat-Pro (РФ-резидентный, OK для compliance).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"
DEFAULT_MODEL = "GigaChat-Pro"

# In-memory token cache: {auth_key_hash: (access_token, expires_at_epoch)}
# Per-process — для production-scale можно перенести в Redis.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _cache_key(auth_key: str) -> str:
    """Хэш auth_key для key в cache — не хранить plaintext."""
    import hashlib
    return hashlib.sha256(auth_key.encode()).hexdigest()[:16]


async def _get_access_token(auth_key: str, base_auth_url: str, timeout: float) -> str | None:
    """Exchange auth_key (client_id:client_secret base64) на access_token. Cache 25 мин."""
    cache_key = _cache_key(auth_key)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    headers = {
        "Authorization": f"Basic {auth_key}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"scope": "GIGACHAT_API_PERS"}
    try:
        # GigaChat OAuth требует verify=False иногда (self-signed CA Sber).
        # В продакшене tenant должен подтвердить, что CA приемлемо.
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.post(base_auth_url, data=data, headers=headers)
    except httpx.HTTPError as e:
        logger.warning("gigachat oauth network: %s", type(e).__name__)
        return None
    if r.status_code != 200:
        logger.warning("gigachat oauth failed status=%d", r.status_code)
        return None
    try:
        d = r.json()
        token = d.get("access_token")
        expires_ms = d.get("expires_at")  # epoch ms
        if not token:
            return None
        # Cache до expiration
        expires_at = (expires_ms / 1000) if expires_ms else (time.time() + 25 * 60)
        _TOKEN_CACHE[cache_key] = (token, expires_at)
        return token
    except (KeyError, ValueError):
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

    # GigaChat использует OAuth — exchange ключ → access_token
    access_token = await _get_access_token(api_key, DEFAULT_AUTH_URL, timeout)
    if not access_token:
        return {
            "error": "oauth_failed: не удалось получить access_token",
            "fallback_recommended": True,
        }

    # GigaChat поддерживает images через attachments (file_id) или прямые URL.
    # Используем content array OpenAI-style; для prod нужно отдельно загружать
    # картинки через POST /files и затем ссылаться по file_id. Здесь — best-effort:
    # передаём URL в content tex+image_url, формат может потребовать adjustment
    # под конкретную доку (которая периодически меняется).
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
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
    except httpx.TimeoutException:
        return {"error": f"timeout >{timeout}s", "fallback_recommended": False}
    except httpx.HTTPError as e:
        return {"error": f"http_error: {type(e).__name__}", "fallback_recommended": False}

    if r.status_code != 200:
        body_preview = r.text[:200]
        recommend = r.status_code in (401, 403, 429)
        logger.warning("gigachat non-200 status=%d body=%s", r.status_code, body_preview)
        return {
            "error": f"http_{r.status_code}: {body_preview}",
            "status_code": r.status_code,
            "fallback_recommended": recommend,
        }
    try:
        data = r.json()
        msg = data["choices"][0]["message"]
        mechanics = str(msg.get("content", "")).strip()
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
    """Test = OAuth exchange. Если получили access_token → ключ валиден."""
    if not api_key:
        return {"ok": False, "message": "empty api_key"}
    token = await _get_access_token(api_key, DEFAULT_AUTH_URL, timeout)
    if token:
        return {"ok": True, "message": "ok"}
    return {"ok": False, "message": "oauth_failed (ключ невалиден или Sber API недоступен)"}
