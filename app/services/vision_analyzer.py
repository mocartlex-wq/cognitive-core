"""Vision analyzer — multi-provider mechanics extraction.

Owner-mandate (2026-05-24): «дать возможность механики, а не картинок».
Media-pipeline даёт 12 статичных frame URL'ов + Whisper-транскрипт.
Этот модуль — vision-LLM stage: возвращает 2-4 предложения «что происходит».

Provider resolution order (per call):

  1. Если passed owner_user_id → читаем user_external_keys для tenant'а
     и пробуем в порядке PROVIDER_ORDER (qwen → minimax → gigachat → claude
     → openai → gemini). Первый успешный wins.

  2. Если у tenant'а нет ключей ИЛИ они все failed → fallback на shared
     platform-key (env QWEN_API_KEY). Это сохраняет существующее поведение
     для tenants без opt-in.

  3. Если и shared нет / тоже failed → DeepSeek text-only fallback из
     transcript (env DEEPSEEK_API_KEY).

  4. Если ничего нет → graceful skip, базовый pipeline (frames + transcript)
     продолжает работать без vision stage.

Audit-trail:
  Каждое успешное использование per-tenant ключа пишется в L1
  (`domain=external_key_usage`) — для billing transparency.

Security:
  - Per-tenant keys никогда не логируются (даже в exception).
  - При 401/403/429 ключа делаем fallback на следующего provider'а,
    last_test_status обновляется на 'auth_failed'/'rate_limit'.

Backward-compat:
  - analyze_mechanics(frame_urls, transcript, duration_seconds) — старая сигнатура
    работает (owner_user_id опц.).
  - is_enabled() / has_vision() / has_text_fallback() — оставлены.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

from app.security.secrets_vault import SecretsVaultError, decrypt
from app.services.vision_providers import (
    PROVIDER_LABELS,
    PROVIDER_ORDER,
    get_analyzer,
)

logger = logging.getLogger(__name__)

# ─── Shared platform-key (legacy default — env-based) ─────────────────────
# NB: os.environ.get(NAME, DEFAULT) применяет DEFAULT только если ключа НЕТ.
# docker-compose.prod.yml использует ${VAR:-} → переменная всегда есть, но
# пустая. Поэтому везде паттерн `os.environ.get(...) or DEFAULT` — пустая
# строка falsy, default срабатывает.
def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default

QWEN_API_KEY = _env("QWEN_API_KEY")
QWEN_BASE_URL = _env(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
QWEN_MODEL = _env("QWEN_MODEL", "qwen-vl-max-latest")
QWEN_MAX_FRAMES = int(_env("QWEN_MAX_FRAMES", "12"))
QWEN_MAX_OUTPUT_TOKENS = int(_env("QWEN_MAX_OUTPUT_TOKENS", "800"))
QWEN_TIMEOUT_SECONDS = float(_env("QWEN_TIMEOUT_SECONDS", "60"))

DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
).rstrip("/")
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")


# ─── Public flags (backward-compat) ───────────────────────────────────────
def is_enabled() -> bool:
    """True если хотя бы один из shared providers (Qwen или DeepSeek) доступен.

    Per-tenant ключи проверяются отдельно — это shared-only флаг.
    """
    return bool(QWEN_API_KEY) or bool(DEEPSEEK_API_KEY)


def has_vision() -> bool:
    """True если shared Qwen vision доступен."""
    return bool(QWEN_API_KEY)


def has_text_fallback() -> bool:
    """True если shared DeepSeek text fallback доступен."""
    return bool(DEEPSEEK_API_KEY)


# ─── Prompts ──────────────────────────────────────────────────────────────
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


def _build_user_prompt(transcript: Optional[str], duration_seconds: Optional[float],
                       frame_count: int) -> str:
    parts = [
        f"Видео длительностью {duration_seconds:.1f} сек." if duration_seconds else "Видео.",
        f"Кадров: {frame_count}.",
    ]
    if transcript and transcript.strip():
        t_short = transcript[:2000]
        parts.append(f"Транскрипт аудио: «{t_short}»")
    else:
        parts.append("Аудиодорожки нет или она пустая.")
    parts.append("Опиши механику происходящего в 2-4 предложениях.")
    return " ".join(parts)


# ─── DeepSeek text-only fallback (existing logic) ────────────────────────
async def _analyze_text_only_deepseek(
    transcript: Optional[str],
    duration_seconds: Optional[float],
    frame_count: int,
) -> dict:
    """Fallback: text-only mechanics через DeepSeek."""
    if not DEEPSEEK_API_KEY:
        return {"error": "deepseek_unavailable", "skipped": True}

    has_audio = bool(transcript and transcript.strip())
    if not has_audio:
        return {
            "mechanics_summary": (
                f"Видео {duration_seconds:.1f}с без аудиодорожки, {frame_count} кадров. "
                f"Для описания визуального содержимого нужен vision-провайдер."
            ) if duration_seconds else f"Silent video, {frame_count} frames.",
            "model": "n/a (silent, no LLM call)",
            "tokens_in": 0,
            "tokens_out": 0,
            "fallback": "text_only_silent",
        }

    system_prompt = (
        "Ты анализируешь короткие видео по их транскрипту аудио. "
        "Опиши МЕХАНИКУ происходящего (что делается, какой процесс) "
        "в 2-4 предложениях на русском. По делу, без воды."
    )
    user_msg = (
        f"Видео длительностью {duration_seconds:.1f} сек ({frame_count} кадров). "
        f"Транскрипт аудио:\n«{transcript[:3000]}»\n\n"
        f"Опиши механику в 2-4 предложениях."
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 500,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions", json=payload, headers=headers
            )
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        return {"error": f"deepseek_error: {type(e).__name__}"}
    if r.status_code != 200:
        return {"error": f"deepseek_http_{r.status_code}"}
    try:
        data = r.json()
        msg = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        return {
            "mechanics_summary": msg,
            "model": DEEPSEEK_MODEL,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "fallback": "text_only_deepseek",
        }
    except (KeyError, IndexError, ValueError) as e:
        return {"error": f"deepseek_parse: {type(e).__name__}"}


# ─── Per-tenant key fetch + audit ─────────────────────────────────────────
async def _load_tenant_keys(owner_user_id: str) -> list[dict]:
    """Загрузить и расшифровать все per-tenant ключи владельца.

    Возвращает список dict-ов в порядке PROVIDER_ORDER (preferred first):
        [{"provider": "qwen", "api_key": "<plain>", "base_url": ..., "model_name": ...}, ...]

    Невалидные/нерасшифровывающиеся записи пропускаются с warning'ом.
    """
    from app.db.postgres import get_pool

    if not owner_user_id:
        return []
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT provider, api_key_encrypted, base_url, model_name
                  FROM user_external_keys
                 WHERE owner_user_id = $1::uuid
                """,
                owner_user_id,
            )
    except Exception as e:
        # Таблица может ещё не быть создана (миграция 0010 не применена)
        logger.info("tenant keys table not available: %s", type(e).__name__)
        return []

    keys_by_provider: dict[str, dict] = {}
    for row in rows:
        provider = row["provider"]
        try:
            plain = decrypt(row["api_key_encrypted"])
        except SecretsVaultError:
            logger.warning(
                "tenant key decrypt failed owner=%s provider=%s — skipped",
                owner_user_id, provider,
            )
            continue
        keys_by_provider[provider] = {
            "provider": provider,
            "api_key": plain,
            "base_url": row.get("base_url"),
            "model_name": row.get("model_name"),
        }

    # Сортируем по PROVIDER_ORDER preference, остальные провайдеры — в конец
    ordered: list[dict] = []
    for p in PROVIDER_ORDER:
        if p in keys_by_provider:
            ordered.append(keys_by_provider[p])
    for p, k in keys_by_provider.items():
        if p not in PROVIDER_ORDER:
            ordered.append(k)
    return ordered


async def _update_test_status(owner_user_id: str, provider: str, status: str) -> None:
    """Обновить last_test_status + last_used_at + last_test_at для аудита.

    Используется после real call (НЕ test endpoint) — чтобы UI показывал
    реальный последний статус ключа.
    """
    from app.db.postgres import get_pool
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE user_external_keys
                   SET last_used_at = NOW(),
                       last_test_status = $1,
                       last_test_at = NOW(),
                       updated_at = NOW()
                 WHERE owner_user_id = $2::uuid AND provider = $3
                """,
                status, owner_user_id, provider,
            )
    except Exception as e:
        logger.warning("update_test_status failed: %s", type(e).__name__)


async def _audit_external_key_usage(
    owner_user_id: str, provider: str, model: str,
    tokens_in: int, tokens_out: int, success: bool, error: Optional[str] = None,
) -> None:
    """Запись в L1 для billing transparency.

    Tenant сможет увидеть когда и сколько потратил через cognitive_recall
    domain=external_key_usage.
    """
    from app.db.postgres import get_pool
    payload = {
        "owner_user_id": owner_user_id,
        "provider": provider,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "success": success,
    }
    if error:
        # НЕ кладём raw error если он может содержать чувствительные данные
        payload["error_class"] = error[:80]
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO l1_raw_events (source_agent, domain, raw_payload)
                VALUES ($1, $2, $3::jsonb)
                """,
                "vision_analyzer",
                "external_key_usage",
                json.dumps(payload, ensure_ascii=False),
            )
    except Exception as e:
        logger.warning("audit external_key_usage failed: %s", type(e).__name__)


# ─── Main entry point ─────────────────────────────────────────────────────
async def analyze_mechanics(
    frame_urls: list[str],
    transcript: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    owner_user_id: Optional[str] = None,
) -> dict:
    """Provider-aware mechanics extraction с fallback chain.

    Args:
        frame_urls: список абсолютных HTTPS URL'ов на JPG frames.
        transcript: Whisper-транскрипт аудиодорожки (опц.).
        duration_seconds: длительность видео (опц.).
        owner_user_id: UUID-string владельца — для per-tenant ключей. Если
                       None → сразу shared platform-key.

    Returns:
        {
          "mechanics_summary": "<2-4 предложения>",
          "model": "<provider:model>",
          "tokens_in": int,
          "tokens_out": int,
          "provider_source": "tenant:qwen" | "shared:qwen" | "fallback:deepseek",
          "frames_analyzed": int,
        }
        или
        {"error": "...", "skipped": True} если совсем nothing available.
    """
    if not frame_urls:
        return {"error": "no frames provided", "skipped": True}

    # Cap frames для cost
    capped = frame_urls[:QWEN_MAX_FRAMES]
    frame_count = len(capped)
    user_prompt = _build_user_prompt(transcript, duration_seconds, frame_count)

    tried: list[str] = []
    last_error: Optional[str] = None

    # ─── Stage 1: per-tenant keys ─────────────────────────────────────────
    if owner_user_id:
        tenant_keys = await _load_tenant_keys(owner_user_id)
        for entry in tenant_keys:
            provider = entry["provider"]
            analyzer = get_analyzer(provider)
            if not analyzer:
                continue
            tried.append(f"tenant:{provider}")
            try:
                result = await analyzer(
                    api_key=entry["api_key"],
                    frame_urls=capped,
                    transcript=transcript,
                    duration_seconds=duration_seconds,
                    base_url=entry.get("base_url"),
                    model_name=entry.get("model_name"),
                    timeout=QWEN_TIMEOUT_SECONDS,
                    max_output_tokens=QWEN_MAX_OUTPUT_TOKENS,
                    system_prompt=_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                )
            except Exception as e:
                logger.warning(
                    "tenant provider %s raised: %s", provider, type(e).__name__,
                )
                result = {"error": f"exception: {type(e).__name__}",
                          "fallback_recommended": True}

            if result.get("mechanics_summary"):
                # SUCCESS — обновить status + audit + return
                await _update_test_status(owner_user_id, provider, "ok")
                await _audit_external_key_usage(
                    owner_user_id, provider,
                    result.get("model", provider),
                    result.get("tokens_in", 0),
                    result.get("tokens_out", 0),
                    success=True,
                )
                return {
                    **result,
                    "provider_source": f"tenant:{provider}",
                    "frames_analyzed": frame_count,
                }

            # FAILURE — update status и попробуем следующий
            last_error = result.get("error")
            status_code = result.get("status_code")
            if status_code in (401, 403):
                await _update_test_status(owner_user_id, provider, "auth_failed")
            elif status_code == 429:
                await _update_test_status(owner_user_id, provider, "rate_limit")
            else:
                await _update_test_status(owner_user_id, provider, "error")

            # Audit failure тоже — для billing visibility
            await _audit_external_key_usage(
                owner_user_id, provider, provider, 0, 0,
                success=False, error=last_error or "unknown",
            )

            # Если fallback_recommended=False — это hard error (parse/timeout
            # к нашей платформе), а не auth-issue. Тогда тоже fallthrough.

    # ─── Stage 2: shared platform Qwen ────────────────────────────────────
    if QWEN_API_KEY:
        tried.append("shared:qwen")
        analyzer = get_analyzer("qwen")
        try:
            result = await analyzer(
                api_key=QWEN_API_KEY,
                frame_urls=capped,
                transcript=transcript,
                duration_seconds=duration_seconds,
                base_url=QWEN_BASE_URL,
                model_name=QWEN_MODEL,
                timeout=QWEN_TIMEOUT_SECONDS,
                max_output_tokens=QWEN_MAX_OUTPUT_TOKENS,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as e:
            result = {"error": f"exception: {type(e).__name__}",
                      "fallback_recommended": True}

        if result.get("mechanics_summary"):
            return {
                **result,
                "provider_source": "shared:qwen",
                "frames_analyzed": frame_count,
            }
        last_error = result.get("error") or last_error

    # ─── Stage 3: DeepSeek text-only fallback ─────────────────────────────
    if DEEPSEEK_API_KEY:
        tried.append("fallback:deepseek")
        result = await _analyze_text_only_deepseek(transcript, duration_seconds, frame_count)
        if result.get("mechanics_summary"):
            return {
                **result,
                "provider_source": "fallback:deepseek",
                "frames_analyzed": frame_count,
                "qwen_error": last_error,
            }
        last_error = result.get("error") or last_error

    # ─── Stage 4: nothing worked ──────────────────────────────────────────
    return {
        "error": f"no provider available; tried={tried}; last_error={last_error}",
        "skipped": True,
    }
