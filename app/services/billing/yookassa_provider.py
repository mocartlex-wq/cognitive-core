"""ЮKassa billing provider (scaffold 2026-05-26).

ЮKassa (бывшая Яндекс.Касса) — основной payment processor для РФ.
Принимает МИР, Visa-РФ, MasterCard-РФ, Сбер ID, СБП, ЮMoney.

Docs: https://yookassa.ru/developers
Auth: Basic с shopId + secret (HTTP Basic Auth, не Bearer)

ENV vars:
  YOOKASSA_SHOP_ID=1234567
  YOOKASSA_SECRET_KEY=live_xxx (или test_xxx для sandbox)

Scaffold готов — owner получает account на yookassa.ru, добавляет creds
в .env, и billing работает.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY", "")


def is_configured() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def _auth_header() -> str:
    """ЮKassa Basic Auth = base64(shopId:secret)."""
    creds = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}"
    return "Basic " + base64.b64encode(creds.encode()).decode()


async def create_checkout(
    amount_kopecks: int,
    currency: str,
    owner_user_id: str,
    target_tier: str,
    success_url: str,
    cancel_url: str,
) -> dict:
    """Создать ЮKassa Payment с redirect → возвращает confirmation_url."""
    if not is_configured():
        return {"error": "ЮKassa не настроена (YOOKASSA_SHOP_ID + YOOKASSA_SECRET_KEY отсутствуют)"}
    if currency != "RUB":
        return {"error": "ЮKassa поддерживает только RUB. Используйте Stripe для USD/EUR."}

    # Idempotence key — обязателен в ЮKassa API (защита от double-charge на retry)
    idempotence_key = str(uuid.uuid4())

    body = {
        "amount": {
            "value": f"{amount_kopecks / 100:.2f}",  # ЮKassa требует строку с 2 знаками
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": success_url,
        },
        "description": f"Cognitive Core {target_tier} subscription",
        "metadata": {
            "owner_user_id": owner_user_id,
            "target_tier": target_tier,
        },
        "save_payment_method": True,  # для recurring (auto-renew)
    }
    headers = {
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
        "Idempotence-Key": idempotence_key,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{YOOKASSA_API_BASE}/payments", json=body, headers=headers)
    except httpx.HTTPError as e:
        return {"error": f"yookassa network: {type(e).__name__}"}
    if r.status_code not in (200, 201):
        body_preview = r.text[:300]
        logger.warning("yookassa checkout non-2xx status=%d body=%s", r.status_code, body_preview)
        return {"error": f"yookassa http_{r.status_code}", "details": body_preview}
    try:
        data = r.json()
        return {
            "checkout_url": data.get("confirmation", {}).get("confirmation_url"),
            "session_id": data.get("id"),
            "status": data.get("status"),
        }
    except (KeyError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}"}


def verify_webhook(body: bytes, signature: str, webhook_secret: Optional[str] = None) -> dict | None:
    """ЮKassa webhook не имеет signature по умолчанию — verify через source IP.

    Docs: https://yookassa.ru/developers/using-api/webhooks
    ЮKassa отправляет уведомления с IP из whitelist:
      185.71.76.0/27, 185.71.77.0/27, 77.75.153.0/25, 77.75.154.128/25,
      77.75.156.11, 77.75.156.35

    В нашем случае verify делает nginx через `allow` directive в location.
    Если nginx разрешил — request от ЮKassa. Здесь просто parse body.

    Для production-grade: добавить mTLS или secret-token в URL path.
    """
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:
        logger.warning("yookassa webhook parse failed: %s", type(e).__name__)
        return None


async def handle_event(event: dict, db_pool) -> dict:
    """Process verified ЮKassa notification."""
    event_type = event.get("event", "")
    payment = event.get("object", {})
    payment_id = payment.get("id", "")

    # Idempotency
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM billing_processed_events WHERE event_id = $1 AND provider = 'yookassa'",
            payment_id,
        )
        if exists:
            return {"action": "skipped_duplicate", "event_id": payment_id}

    if event_type == "payment.succeeded":
        metadata = payment.get("metadata", {})
        owner = metadata.get("owner_user_id")
        target_tier = metadata.get("target_tier", "pro")
        if not owner:
            return {"action": "skipped_no_owner", "event_id": payment_id}

        from app.services.billing import TIER_LIMITS
        limits = TIER_LIMITS.get(target_tier, TIER_LIMITS["pro"])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO owner_quotas
                         (owner_user_id, tier, max_events_per_day, max_storage_mb,
                          max_agents, max_recall_per_min)
                       VALUES ($1::uuid, $2, $3, $4, $5, $6)
                       ON CONFLICT (owner_user_id) DO UPDATE
                         SET tier = EXCLUDED.tier,
                             max_events_per_day = EXCLUDED.max_events_per_day,
                             max_storage_mb = EXCLUDED.max_storage_mb,
                             max_agents = EXCLUDED.max_agents,
                             max_recall_per_min = EXCLUDED.max_recall_per_min""",
                    owner, target_tier, limits["max_events_per_day"], limits["max_storage_mb"],
                    limits["max_agents"], limits["max_recall_per_min"],
                )
                await conn.execute(
                    "INSERT INTO billing_processed_events (event_id, provider, processed_at) "
                    "VALUES ($1, 'yookassa', NOW())",
                    payment_id,
                )
        logger.info("yookassa upgraded owner=%s to tier=%s", owner[:8], target_tier)
        return {"action": "tier_upgraded", "owner": owner, "new_tier": target_tier}

    return {"action": "ignored", "event_type": event_type}


async def test_connection() -> dict:
    """Verify by hitting GET /v3/payments (list, limit=1)."""
    if not is_configured():
        return {"ok": False, "message": "YOOKASSA_SHOP_ID / SECRET_KEY не заданы в env"}
    headers = {"Authorization": _auth_header()}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{YOOKASSA_API_BASE}/payments?limit=1", headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        return {"ok": True, "message": "ЮKassa API доступен"}
    return {"ok": False, "message": f"http_{r.status_code}: {r.text[:200]}"}
