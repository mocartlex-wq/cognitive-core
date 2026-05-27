"""Video generation providers — text→video and image→video.

Each provider exposes async functions:

    async def submit(
        api_key: str,
        prompt: str,
        *,
        image_url: str | None = None,        # for image2video mode
        duration_sec: int = 5,
        aspect_ratio: str = "16:9",
        model_name: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        # Returns {"task_id": "...", "provider_status": "submitted"}
        # OR {"error": "...", "fallback_recommended": bool}

    async def poll(
        api_key: str,
        task_id: str,
        *,
        timeout: float = 10.0,
    ) -> dict:
        # Returns {"status": "queued|generating|completed|failed",
        #          "video_url": str | None, "progress_pct": int}
        # OR {"error": "..."}

Whitelist:
  - kling   (Kuaishou Kling.ai — best Chinese model, $0.10-0.50/sec)
  - sora    (OpenAI Sora — preview, API access limited as of 2026-05)

Все providers — per-tenant external API key через `user_external_keys` table
с provider='kling_video' или 'sora_video' (отличает от vision-providers).

Owner mandate (post-launch feature 2026-05-26): «нам нужен видео ИИ, для
создания видео роликов и возможности обработки видео, для блогинга».
Стартуем с Kling — у них public API + работает из РФ через openrouter-style
proxy. Sora wait-list.
"""
from __future__ import annotations

from . import kling, sora

PROVIDER_LABELS: dict[str, str] = {
    "kling_video": "Kling.ai (Kuaishou)",
    "sora_video":  "Sora (OpenAI, preview)",
}

PROVIDER_REGISTRY = {
    "kling_video": kling,
    "sora_video":  sora,
}


def get_provider(provider: str):
    """Return module (with submit/poll funcs) for provider, or None."""
    return PROVIDER_REGISTRY.get(provider)


def is_valid_provider(provider: str) -> bool:
    return provider in PROVIDER_REGISTRY
