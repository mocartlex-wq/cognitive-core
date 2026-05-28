"""Per-tenant outbound webhook management endpoints.

Endpoints (все требуют session-cookie через require_user):
  GET    /user/webhooks            — список webhooks owner-а (secret masked)
  POST   /user/webhooks            — добавить {url, events[], secret?}
  DELETE /user/webhooks/{id}       — удалить (ownership check)
  POST   /user/webhooks/{id}/test  — отправить test-event на endpoint

Security (КРИТИЧНО — webhook URL = user input):
  - Anti-SSRF: только https://, отклоняем private/loopback/link-local/reserved
    IP-литералы и hostname localhost. Используем ipaddress (как в
    billing/yookassa_provider.py). Это мешает tenant-у направить платформу
    POST-ить на internal services (169.254.169.254 metadata, 10.x, 127.x...).
  - Secret шифруется при сохранении (Fernet, app/security/secrets_vault.py),
    plaintext НИКОГДА не возвращается (только masked в GET).
  - Ownership: DELETE/test проверяют owner_user_id перед действием.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from app.db.postgres import get_pool
from app.security.middleware import require_user
from app.security.secrets_vault import encrypt, mask
from app.services.webhooks import WEBHOOK_EVENTS, send_webhook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/user/webhooks", tags=["webhooks"])

_MAX_WEBHOOKS_PER_OWNER = 10


# ─────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────
class CreateWebhookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., min_length=8, max_length=2048)
    events: list[str] = Field(..., min_length=1, max_length=20)
    secret: Optional[str] = Field(None, min_length=8, max_length=256)


# ─────────────────────────────────────────────────────────────────────────
# Anti-SSRF URL validation
# ─────────────────────────────────────────────────────────────────────────
def _reject_ssrf(url: str) -> str:
    """Валидация webhook URL. Возвращает нормализованный URL или 400.

    Блокируем:
      - схему != https
      - hostname отсутствует / localhost-подобные имена
      - IP-литералы из private / loopback / link-local / reserved / multicast
        диапазонов (10.x, 172.16-31.x, 192.168.x, 127.x, 169.254.x, ::1, fc00::/7 ...)
    DNS-имена НЕ резолвим здесь (resolve-time SSRF митигируется на sender:
    follow_redirects=False + платформенный egress). Блок hostname-литералов
    localhost/*.local — дешёвый дополнительный слой.
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный URL")

    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Webhook URL должен быть https://")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise HTTPException(status_code=400, detail="URL без хоста")

    # Явно запрещённые hostname-литералы.
    if host in {"localhost", "ip6-localhost", "ip6-loopback"} or host.endswith(".local") \
            or host.endswith(".internal") or host == "metadata.google.internal":
        raise HTTPException(status_code=400, detail="Запрещённый хост (internal/localhost)")

    # Если host — IP-литерал, проверяем диапазон.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        raise HTTPException(
            status_code=400,
            detail="Запрещённый IP (private/loopback/link-local). Anti-SSRF.",
        )

    return url.strip()


def _validate_events(events: list[str]) -> list[str]:
    """Оставляем только известные event_type. 400 если ни одного валидного."""
    valid = [e for e in events if e in WEBHOOK_EVENTS]
    if not valid:
        raise HTTPException(
            status_code=400,
            detail=f"Нет валидных событий. Допустимые: {', '.join(WEBHOOK_EVENTS)}",
        )
    return sorted(set(valid))


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────
@router.get("")
async def list_webhooks(request: Request) -> dict:
    """Список webhooks owner-а. Secret НИКОГДА не отдаём — только флаг наличия."""
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, url, events, enabled,
                      (secret_encrypted IS NOT NULL) AS has_secret,
                      created_at, last_triggered_at, last_status
                 FROM user_webhooks
                WHERE owner_user_id = $1::uuid
                ORDER BY created_at DESC""",
            user.user_id,
        )
    return {
        "webhooks": [
            {
                "id": r["id"],
                "url": r["url"],
                "events": r["events"],
                "enabled": r["enabled"],
                "secret": mask("x" * 12) if r["has_secret"] else None,
                "created_at": r["created_at"],
                "last_triggered_at": r["last_triggered_at"],
                "last_status": r["last_status"],
            }
            for r in rows
        ],
        "available_events": WEBHOOK_EVENTS,
    }


@router.post("")
async def create_webhook(request: Request, body: CreateWebhookBody) -> dict:
    """Добавить webhook. Anti-SSRF validate + encrypt secret."""
    user = await require_user(request)
    url = _reject_ssrf(body.url)
    events = _validate_events(body.events)
    secret_enc = encrypt(body.secret) if body.secret else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_webhooks WHERE owner_user_id = $1::uuid",
            user.user_id,
        )
        if count >= _MAX_WEBHOOKS_PER_OWNER:
            raise HTTPException(
                status_code=400,
                detail=f"Лимит webhooks: {_MAX_WEBHOOKS_PER_OWNER}",
            )
        new_id = await conn.fetchval(
            """INSERT INTO user_webhooks
                   (owner_user_id, url, events, secret_encrypted, enabled)
               VALUES ($1::uuid, $2, $3, $4, TRUE)
               RETURNING id::text""",
            user.user_id, url, events, secret_enc,
        )
    logger.info("webhook: created id=%s owner=%s events=%s", new_id, user.user_id, events)
    return {"id": new_id, "url": url, "events": events, "enabled": True}


@router.delete("/{webhook_id}")
async def delete_webhook(
    request: Request,
    webhook_id: str = Path(..., min_length=1, max_length=64),
) -> dict:
    """Удалить webhook. Ownership enforced через WHERE owner_user_id."""
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            """DELETE FROM user_webhooks
                WHERE id = $1::uuid AND owner_user_id = $2::uuid
                RETURNING id::text""",
            webhook_id, user.user_id,
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook не найден")
    return {"deleted": deleted}


@router.post("/{webhook_id}/test")
async def test_webhook(
    request: Request,
    webhook_id: str = Path(..., min_length=1, max_length=64),
) -> dict:
    """Отправить test-event на endpoint. Ownership check + decrypt secret."""
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT url, secret_encrypted FROM user_webhooks
                WHERE id = $1::uuid AND owner_user_id = $2::uuid""",
            webhook_id, user.user_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Webhook не найден")

    secret = None
    if row["secret_encrypted"]:
        from app.security.secrets_vault import SecretsVaultError, decrypt
        try:
            secret = decrypt(row["secret_encrypted"])
        except SecretsVaultError:
            secret = None  # шлём unsigned, не блокируем тест

    result = await send_webhook(
        row["url"],
        "agent.claimed",
        {"agent_id": "test-agent", "note": "Это тестовое событие Cognitive Core"},
        secret=secret,
    )
    return {"delivered": result.get("ok", False), "status": result.get("status")}
