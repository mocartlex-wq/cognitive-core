"""Vision analyzer — Qwen-VL обёртка для извлечения «механики» из video frames.

Owner-mandate (2026-05-24): «дать возможность механики, а не картинок».
Текущий media-pipeline даёт 12 статичных frame URL'ов + Whisper-транскрипт.
Этот модуль добавляет ещё один stage: vision-LLM смотрит на frames + читает
transcript → возвращает 2-3 предложения «что происходит в видео» (mechanics).

Provider: Alibaba Cloud Model Studio (DashScope International)
  - Endpoint: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
  - Модель: qwen-vl-max-latest (multimodal)
  - API формат: OpenAI-compatible chat/completions
  - Регион: Frankfurt (eu-central-1) — лучшая latency из РФ

Config из env (загружается через docker-compose env_file):
  QWEN_API_KEY    — Alibaba API ключ (sk-ws-... формат)
  QWEN_BASE_URL   — endpoint (default = dashscope-intl)
  QWEN_MODEL      — model name (default = qwen-vl-max-latest)

Если QWEN_API_KEY не установлен — vision stage пропускается (graceful degradation):
базовый pipeline продолжит работать с frame URLs + transcript как раньше.

Cost guard:
  - Max 12 frames per call (= типичный output из media_analyzer)
  - Max 800 output tokens
  - timeout=60s (Qwen может думать долго на 12 frames)
  - ~ $0.02-0.03 per video на qwen-vl-max-latest

Returns: dict with keys:
  - mechanics_summary (str) — 2-3 sentence summary что происходит
  - error (str) если что-то сломалось — pipeline продолжит без vision stage
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "").strip()
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1").rstrip("/")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen-vl-max-latest").strip()
QWEN_MAX_FRAMES = int(os.environ.get("QWEN_MAX_FRAMES", "12"))
QWEN_MAX_OUTPUT_TOKENS = int(os.environ.get("QWEN_MAX_OUTPUT_TOKENS", "800"))
QWEN_TIMEOUT_SECONDS = float(os.environ.get("QWEN_TIMEOUT_SECONDS", "60"))


def is_enabled() -> bool:
    """True если QWEN_API_KEY есть в env — vision stage активен."""
    return bool(QWEN_API_KEY)


_SYSTEM_PROMPT = (
    "Ты анализируешь короткие видео (3-30 секунд). Тебе дают 8-12 кадров "
    "извлечённых равномерно по длительности + транскрипт аудио (если есть). "
    "Твоя задача — описать МЕХАНИКУ происходящего: что делает человек, "
    "какой процесс/действие изображено, как меняется состояние между кадрами. "
    "НЕ описывай каждый кадр отдельно — собирай в одну последовательность.\n\n"
    "Ответ — 2-4 предложения на русском, по делу, без воды. Если в кадрах "
    "интерфейс — назови приложение/сайт и действия. Если человек — что он "
    "делает, как меняется поза/локация. Если процесс — что начинается/заканчивается."
)


async def analyze_mechanics(
    frame_urls: list[str],
    transcript: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> dict:
    """Главная функция: подаёт кадры + transcript Qwen-VL, возвращает mechanics.

    Args:
        frame_urls: список абсолютных HTTPS URL'ов на JPG frames
                   (например, https://mcp.me-ai.ru/api/media/frame/video/abc/frame_0001.jpg)
        transcript: Whisper-транскрипт аудиодорожки (опц.)
        duration_seconds: длительность видео (опц., для context'а)

    Returns:
        {
          "mechanics_summary": "<2-4 предложения>" если success,
          "error": "<msg>" если ошибка,
          "model": "qwen-vl-max-latest",
          "tokens_in": int,
          "tokens_out": int,
        }
    """
    if not is_enabled():
        return {"error": "QWEN_API_KEY not configured", "skipped": True}

    if not frame_urls:
        return {"error": "no frames provided", "skipped": True}

    # Cost guard: cap frames
    capped_frames = frame_urls[:QWEN_MAX_FRAMES]

    # Compose user message
    user_text_parts = [
        f"Видео длительностью {duration_seconds:.1f} сек." if duration_seconds else "Видео.",
        f"Кадров: {len(capped_frames)}.",
    ]
    if transcript and transcript.strip():
        # Cap transcript для cost — иначе много tokens
        t_short = transcript[:2000]
        user_text_parts.append(f"Транскрипт аудио: «{t_short}»")
    else:
        user_text_parts.append("Аудиодорожки нет или она пустая.")
    user_text_parts.append("Опиши механику происходящего в 2-4 предложениях.")
    user_text = " ".join(user_text_parts)

    # Build OpenAI-compatible multimodal content array
    content = [{"type": "text", "text": user_text}]
    for url in capped_frames:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "max_tokens": QWEN_MAX_OUTPUT_TOKENS,
        "temperature": 0.2,  # детерминированно — описание факта, не творчество
    }

    headers = {
        "Authorization": f"Bearer {QWEN_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=QWEN_TIMEOUT_SECONDS) as client:
            r = await client.post(
                f"{QWEN_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException:
        logger.warning("Qwen vision timeout after %ds", QWEN_TIMEOUT_SECONDS)
        return {"error": f"timeout >{QWEN_TIMEOUT_SECONDS}s"}
    except httpx.HTTPError as e:
        logger.warning("Qwen vision HTTP error: %s", e)
        return {"error": f"http_error: {e}"}

    if r.status_code != 200:
        body_preview = r.text[:400]
        logger.warning("Qwen vision non-200: %d body=%s", r.status_code, body_preview)
        return {"error": f"http_{r.status_code}: {body_preview}"}

    try:
        data = r.json()
        msg = data["choices"][0]["message"]
        # qwen-vl message.content может быть строкой ИЛИ массивом [{type:text,text:...}]
        raw_content = msg.get("content", "")
        if isinstance(raw_content, list):
            mechanics = " ".join(
                p.get("text", "") for p in raw_content if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        else:
            mechanics = str(raw_content).strip()

        usage = data.get("usage", {})
        return {
            "mechanics_summary": mechanics,
            "model": QWEN_MODEL,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "frames_analyzed": len(capped_frames),
        }
    except (KeyError, IndexError, ValueError) as e:
        logger.exception("Qwen vision response parse failed: %s", e)
        return {"error": f"parse_error: {e}", "raw_status": r.status_code}
