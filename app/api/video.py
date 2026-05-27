"""Video generation API — per-tenant external provider (Kling, Sora).

POST /api/video/generate  — submit task (returns task_id)
GET  /api/video/status/{task_id}  — poll status

Auth: X-API-Key (agent_id auth) → resolves owner_user_id → load provider
key from user_external_keys table (Fernet-encrypted, decrypted on demand).

Owner mandate (2026-05-26): «нам нужен видео ИИ, для создания видео роликов
и возможности обработки видео, для блогинга». Phase: post-launch (после
получения owner-ом Kling API key).

Scaffold structure готов — нужны:
  1. Kling access_key + secret_key от owner → добавить через /ui/profile
     External AI providers → provider="kling_video", key="access_key|secret_key"
  2. Endpoint live сразу — не требует deploy
  3. E2E test: scripts/e2e_test_video.sh (TODO Phase следующей сессии)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.media import _check_admin_or_owner
from app.db.postgres import get_pool
from app.security.secrets_vault import SecretsVaultError, decrypt
from app.services.video_providers import (
    PROVIDER_LABELS,
    get_provider,
    is_valid_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/video", tags=["video"])


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(..., min_length=3, max_length=2000)
    provider: str = Field("kling_video", description="kling_video|sora_video")
    image_url: str | None = Field(None, description="Опц. — для image2video режима")
    duration_sec: int = Field(5, ge=3, le=10, description="Длительность 3-10s")
    aspect_ratio: str = Field("16:9", pattern=r"^(16:9|9:16|1:1)$")
    model_name: str | None = Field(None, description="Опц. override модели (kling-v1 / kling-v1-pro)")


async def _load_provider_key(owner_user_id: str, provider: str) -> str:
    """Загрузить decrypted API key из user_external_keys для tenant'а."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT api_key_encrypted FROM user_external_keys "
            "WHERE owner_user_id = $1::uuid AND provider = $2",
            owner_user_id, provider,
        )
    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"Ключ {PROVIDER_LABELS.get(provider, provider)} не настроен. "
                   "Добавьте через /ui/profile → «Внешние AI-провайдеры».",
        )
    try:
        return decrypt(row["api_key_encrypted"])
    except SecretsVaultError as e:
        logger.warning("video: decrypt failed for owner=%s provider=%s: %s",
                       str(owner_user_id)[:8], provider, type(e).__name__)
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать ключ — пересохраните его в /ui/profile.",
        )


@router.post("/generate")
async def generate(request: Request, body: GenerateBody) -> dict:
    """Submit video generation task. Async — возвращает task_id для polling."""
    user = await _check_admin_or_owner(request)
    if not is_valid_provider(body.provider):
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный provider: {body.provider}. Доступны: {list(PROVIDER_LABELS.keys())}",
        )

    api_key = await _load_provider_key(user.user_id, body.provider)
    provider_mod = get_provider(body.provider)
    if provider_mod is None:
        raise HTTPException(status_code=500, detail=f"Provider {body.provider} module not loaded")

    result = await provider_mod.submit(
        api_key=api_key,
        prompt=body.prompt,
        image_url=body.image_url,
        duration_sec=body.duration_sec,
        aspect_ratio=body.aspect_ratio,
        model_name=body.model_name,
        timeout=30.0,
    )
    if "error" in result:
        # Provider returned error — pass through with 502 (bad upstream)
        raise HTTPException(
            status_code=502,
            detail=f"Provider {body.provider} error: {result['error']}",
        )
    return result


@router.get("/status/{task_id}")
async def status(request: Request, task_id: str, provider: str = Query("kling_video")) -> dict:
    """Poll task status. Возвращает status + video_url когда completed."""
    user = await _check_admin_or_owner(request)
    if not is_valid_provider(provider):
        raise HTTPException(status_code=400, detail=f"Неизвестный provider: {provider}")

    api_key = await _load_provider_key(user.user_id, provider)
    provider_mod = get_provider(provider)
    if provider_mod is None:
        raise HTTPException(status_code=500, detail=f"Provider {provider} module not loaded")

    result = await provider_mod.poll(api_key=api_key, task_id=task_id, timeout=10.0)
    if "error" in result and "status" not in result:
        raise HTTPException(status_code=502, detail=f"Provider {provider} error: {result['error']}")
    return result
