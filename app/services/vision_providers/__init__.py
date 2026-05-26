"""Vision providers — multi-model vision analyzers за единым контрактом.

Каждый provider exposed как async-функция `analyze(...)`:

    async def analyze(
        api_key: str,
        frame_urls: list[str],
        transcript: str | None,
        duration_seconds: float | None,
        *,
        base_url: str | None = None,
        model_name: str | None = None,
        timeout: float = 60.0,
        max_output_tokens: int = 800,
        system_prompt: str = "...",
        user_prompt: str = "...",
    ) -> dict

Возвращает один из двух dict-форматов:

  Success:
    {
      "mechanics_summary": str,
      "model": str,
      "tokens_in": int,
      "tokens_out": int,
    }

  Failure (caller может fallback на следующего provider'а):
    {
      "error": str,
      "status_code": int | None,
      "fallback_recommended": bool,   # True если 401/403/429 — auth/quota
    }

Whitelist providers — см. PROVIDER_REGISTRY ниже.
"""
from __future__ import annotations

from . import claude, gemini, gigachat, minimax, openai, qwen

# Каноничный whitelist + порядок по preference (cheap-and-good сначала).
# UI fall-through пробует ключи в этом порядке: первый working — wins.
PROVIDER_ORDER: list[str] = [
    "qwen",       # Alibaba — лучшее ratio для русского + дёшево
    "minimax",    # Hailuo — отличный visual reasoning
    "gigachat",   # Sber — РФ-резидентный
    "claude",     # Anthropic Haiku — быстрый и качественный
    "openai",     # GPT-4o-mini — стабильно
    "gemini",     # Google 2.0 Flash — fastest
]

PROVIDER_LABELS: dict[str, str] = {
    "qwen":     "Qwen-VL (Alibaba)",
    "minimax":  "MiniMax (Hailuo)",
    "gigachat": "GigaChat (Sber)",
    "claude":   "Claude (Anthropic)",
    "openai":   "GPT-4o (OpenAI)",
    "gemini":   "Gemini (Google)",
}

PROVIDER_REGISTRY = {
    "qwen":     qwen.analyze,
    "minimax":  minimax.analyze,
    "gigachat": gigachat.analyze,
    "claude":   claude.analyze,
    "openai":   openai.analyze,
    "gemini":   gemini.analyze,
}


def get_analyzer(provider: str):
    """Вернуть async-функцию analyze для provider'а, или None если unknown."""
    return PROVIDER_REGISTRY.get(provider)


def is_valid_provider(provider: str) -> bool:
    """Provider в whitelist?"""
    return provider in PROVIDER_REGISTRY
