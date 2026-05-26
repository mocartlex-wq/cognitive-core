"""Per-tenant external AI provider keys endpoints.

Endpoints (все требуют session-cookie через require_user):
  GET    /user/settings/external-keys              — список providers + masked
  POST   /user/settings/external-key               — UPSERT нового ключа
  POST   /user/settings/external-key/{p}/test      — validate connection
  DELETE /user/settings/external-key/{p}           — удалить

Security:
  - Plaintext key никогда не возвращается в response (только masked для UI).
  - Encrypt при save через Fernet (app/security/secrets_vault.py).
  - Whitelist provider name (qwen/minimax/gigachat/claude/openai/gemini).
  - Никогда не логируем содержимое keys (даже в exception traces).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import require_user
from app.security.secrets_vault import SecretsVaultError, decrypt, encrypt, mask
from app.services.vision_providers import (
    PROVIDER_LABELS,
    PROVIDER_ORDER,
    is_valid_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/user/settings", tags=["user-settings"])


# ─────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────
class SaveExternalKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(..., min_length=1, max_length=32)
    api_key: str = Field(..., min_length=4, max_length=2048)
    base_url: Optional[str] = Field(None, max_length=500)
    model_name: Optional[str] = Field(None, max_length=200)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def _validate_provider(provider: str) -> None:
    if not is_valid_provider(provider):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Неизвестный provider «{provider}». "
                f"Допустимые: {', '.join(PROVIDER_ORDER)}"
            ),
        )


def _validate_base_url(base_url: Optional[str]) -> Optional[str]:
    if not base_url:
        return None
    bu = base_url.strip()
    if not bu:
        return None
    if not (bu.startswith("https://") or bu.startswith("http://")):
        raise HTTPException(
            status_code=400,
            detail="base_url должен начинаться с https:// или http://",
        )
    return bu


def _fmt_dt(v) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


# ─────────────────────────────────────────────────────────────────────────
# GET /user/settings/external-keys
# ─────────────────────────────────────────────────────────────────────────
@router.get("/external-keys")
async def list_external_keys(request: Request):
    """Список providers с masked ключами.

    Возвращает только metadata + masked ключ — plaintext никогда не светится.
    Чтобы tenant видел какие у него подключены providers + статус.
    """
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT provider, api_key_encrypted, base_url, model_name,
                       last_used_at, last_test_status, last_test_at,
                       created_at, updated_at
                  FROM user_external_keys
                 WHERE owner_user_id = $1::uuid
                """,
                user.user_id,
            )
        except Exception as e:
            # Migration 0010 ещё не применена — return empty
            logger.info("external-keys table not available: %s", type(e).__name__)
            return {"count": 0, "items": [], "available_providers": _available_providers()}

    items = []
    for row in rows:
        provider = row["provider"]
        # Mask: decrypt чтобы показать last 4, если decrypt failed — все звёзды
        masked: str
        try:
            plain = decrypt(row["api_key_encrypted"])
            masked = mask(plain)
        except SecretsVaultError:
            masked = "*** (ключ неисправен)"
            # Перезаписать last_test_status чтобы UI знал
            # FIX 2026-05-26: раньше silent except → ошибки vault и DB update
            # тонули. Теперь логируем — admin может найти broken keys в /var/log.
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE user_external_keys
                              SET last_test_status = 'decrypt_failed', updated_at = NOW()
                            WHERE owner_user_id = $1::uuid AND provider = $2""",
                        user.user_id, provider,
                    )
            except Exception as e:
                logger.warning("user_settings: failed to mark %s key as decrypt_failed for user=%s: %s",
                               provider, str(user.user_id)[:8], type(e).__name__)

        items.append({
            "provider": provider,
            "label": PROVIDER_LABELS.get(provider, provider),
            "masked_key": masked,
            "base_url": row.get("base_url"),
            "model_name": row.get("model_name"),
            "last_used_at": _fmt_dt(row.get("last_used_at")),
            "last_test_status": row.get("last_test_status"),
            "last_test_at": _fmt_dt(row.get("last_test_at")),
            "created_at": _fmt_dt(row.get("created_at")),
            "updated_at": _fmt_dt(row.get("updated_at")),
        })

    # Сортируем по PROVIDER_ORDER
    order_idx = {p: i for i, p in enumerate(PROVIDER_ORDER)}
    items.sort(key=lambda x: order_idx.get(x["provider"], 999))

    return {
        "count": len(items),
        "items": items,
        "available_providers": _available_providers(),
    }


def _available_providers() -> list[dict]:
    """Metadata для UI — какие providers в нашем whitelist."""
    return [
        {"id": p, "label": PROVIDER_LABELS.get(p, p)}
        for p in PROVIDER_ORDER
    ]


# ─────────────────────────────────────────────────────────────────────────
# POST /user/settings/external-key  (UPSERT)
# ─────────────────────────────────────────────────────────────────────────
@router.post("/external-key")
async def save_external_key(body: SaveExternalKeyBody, request: Request):
    """UPSERT key — encrypt и сохранить.

    НЕ запускает test (для скорости). UI потом дёрнет /test отдельно.
    """
    user = await require_user(request)
    _validate_provider(body.provider)
    base_url = _validate_base_url(body.base_url)
    plain_key = body.api_key.strip()
    if not plain_key:
        raise HTTPException(status_code=400, detail="api_key пустой")

    try:
        encrypted = encrypt(plain_key)
    except SecretsVaultError as e:
        logger.error("encrypt failed (likely vault not configured): %s", type(e).__name__)
        raise HTTPException(
            status_code=500,
            detail="Vault не настроен. Установите COGCORE_SECRETS_MASTER_KEY в .env",
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_external_keys
                (owner_user_id, provider, api_key_encrypted, base_url, model_name)
            VALUES ($1::uuid, $2, $3, $4, $5)
            ON CONFLICT (owner_user_id, provider) DO UPDATE
                SET api_key_encrypted = EXCLUDED.api_key_encrypted,
                    base_url          = EXCLUDED.base_url,
                    model_name        = EXCLUDED.model_name,
                    last_test_status  = NULL,
                    last_test_at      = NULL,
                    updated_at        = NOW()
            """,
            user.user_id, body.provider, encrypted, base_url, body.model_name,
        )

    # Никогда не логируем сам ключ — только что factум saved
    logger.info("external_key_saved user=%s provider=%s", user.user_id, body.provider)
    return {
        "ok": True,
        "provider": body.provider,
        "label": PROVIDER_LABELS.get(body.provider, body.provider),
        "masked_key": mask(plain_key),
    }


# ─────────────────────────────────────────────────────────────────────────
# POST /user/settings/external-key/{provider}/test
# ─────────────────────────────────────────────────────────────────────────
@router.post("/external-key/{provider}/test")
async def test_external_key(provider: str, request: Request):
    """Validate connection через minimal API call к provider.

    Decrypt ключ → дёрнуть provider.test_connection(...) → обновить
    last_test_status + last_test_at. Возвращает {ok, message, latency_ms}.
    """
    user = await require_user(request)
    _validate_provider(provider)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT api_key_encrypted, base_url, model_name
              FROM user_external_keys
             WHERE owner_user_id = $1::uuid AND provider = $2
            """,
            user.user_id, provider,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Ключ не настроен")

    try:
        plain_key = decrypt(row["api_key_encrypted"])
    except SecretsVaultError as e_vault:
        # Mark status decrypt_failed чтобы UI показал
        # FIX 2026-05-26: silent except → log warning. Без этого vault errors
        # тонули, admin не видел сколько ключей broken.
        logger.warning("user_settings.test_external_key: vault decrypt FAILED for provider=%s user=%s: %s",
                       provider, str(user.user_id)[:8], type(e_vault).__name__)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE user_external_keys
                          SET last_test_status='decrypt_failed', last_test_at=NOW(),
                              updated_at=NOW()
                        WHERE owner_user_id=$1::uuid AND provider=$2""",
                    user.user_id, provider,
                )
        except Exception as e_db:
            logger.warning("user_settings.test_external_key: also failed to update DB status: %s",
                           type(e_db).__name__)
        raise HTTPException(
            status_code=500,
            detail="Не удалось расшифровать ключ — пересохраните его.",
        )

    # Dynamic import test_connection
    import importlib
    import time
    try:
        mod = importlib.import_module(f"app.services.vision_providers.{provider}")
        test_fn = getattr(mod, "test_connection", None)
    except ImportError:
        test_fn = None
    if test_fn is None:
        raise HTTPException(
            status_code=500,
            detail=f"test_connection не реализован для {provider}",
        )

    t0 = time.monotonic()
    try:
        result = await test_fn(
            plain_key,
            base_url=row.get("base_url"),
            model_name=row.get("model_name"),
        )
    except Exception as e:
        logger.warning("test_connection raised: %s", type(e).__name__)
        result = {"ok": False, "message": f"exception: {type(e).__name__}"}
    latency_ms = round((time.monotonic() - t0) * 1000, 1)

    # Map result → status
    if result.get("ok"):
        status = "ok"
    else:
        msg = (result.get("message") or "").lower()
        if "auth" in msg or "401" in msg or "403" in msg:
            status = "auth_failed"
        elif "rate_limit" in msg or "429" in msg:
            status = "rate_limit"
        elif "timeout" in msg:
            status = "timeout"
        else:
            status = "error"

    # Update status в БД
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE user_external_keys
                   SET last_test_status = $1, last_test_at = NOW(), updated_at = NOW()
                 WHERE owner_user_id = $2::uuid AND provider = $3
                """,
                status, user.user_id, provider,
            )
    except Exception as e:
        logger.warning("update test_status failed: %s", type(e).__name__)

    return {
        "ok": result.get("ok", False),
        "message": result.get("message", ""),
        "latency_ms": latency_ms,
        "status": status,
        "provider": provider,
    }


# ─────────────────────────────────────────────────────────────────────────
# DELETE /user/settings/external-key/{provider}
# ─────────────────────────────────────────────────────────────────────────
@router.delete("/external-key/{provider}")
async def delete_external_key(provider: str, request: Request):
    """Удалить tenant key. Возвращает 404 если не было."""
    user = await require_user(request)
    _validate_provider(provider)
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM user_external_keys
             WHERE owner_user_id = $1::uuid AND provider = $2
            """,
            user.user_id, provider,
        )
    # asyncpg возвращает строку вида "DELETE 0" / "DELETE 1"
    deleted = result.endswith(" 1") if isinstance(result, str) else False
    if not deleted:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    logger.info("external_key_deleted user=%s provider=%s", user.user_id, provider)
    return {"ok": True, "provider": provider}
