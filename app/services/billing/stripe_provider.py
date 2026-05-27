"""Stripe billing provider (scaffold 2026-05-26).

Owner mandate: «у меня есть MasterCard и VPN» → можем регистрировать
Stripe для foreign tenants (USD/EUR pricing, recurring subscriptions).

Дёргаем Stripe API напрямую через httpx (не используем pip stripe lib —
лишняя зависимость). Полный protocol:
  POST /v1/checkout/sessions    — создать checkout link
  POST /v1/webhook_endpoints    — register webhook (owner делает в Dashboard)
  Webhook events: checkout.session.completed, customer.subscription.{created,deleted,updated}

ENV vars (через .env):
  STRIPE_SECRET_KEY=sk_test_... or sk_live_...
  STRIPE_WEBHOOK_SECRET=whsec_... (from Dashboard → Webhooks)
  STRIPE_PRICE_PRO=price_xxx (создаётся в Stripe Dashboard)
  STRIPE_PRICE_ENTERPRISE=price_yyy

Scaffold готов к live activation когда owner получит Stripe account.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

STRIPE_API_BASE = "https://api.stripe.com/v1"
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICES = {
    "pro":        os.environ.get("STRIPE_PRICE_PRO", ""),
    "enterprise": os.environ.get("STRIPE_PRICE_ENTERPRISE", ""),
}


def is_configured() -> bool:
    """True если STRIPE_SECRET_KEY задан в env."""
    return bool(STRIPE_SECRET_KEY)


async def create_checkout(
    amount_kopecks: int,
    currency: str,
    owner_user_id: str,
    target_tier: str,
    success_url: str,
    cancel_url: str,
) -> dict:
    """Создать Stripe Checkout Session. Returns checkout_url."""
    if not is_configured():
        return {"error": "Stripe не настроен (STRIPE_SECRET_KEY отсутствует в env)"}

    price_id = STRIPE_PRICES.get(target_tier)
    if not price_id:
        return {"error": f"Stripe price_id для tier={target_tier} не настроен (env STRIPE_PRICE_{target_tier.upper()})"}

    # Stripe Checkout Session — form-encoded (не JSON)
    form_data = {
        "mode": "subscription",  # recurring
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": owner_user_id,  # для webhook matching
        "metadata[target_tier]": target_tier,
        "metadata[owner_user_id]": owner_user_id,
    }
    headers = {
        "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{STRIPE_API_BASE}/checkout/sessions",
                data=urlencode(form_data), headers=headers,
            )
    except httpx.HTTPError as e:
        return {"error": f"stripe network: {type(e).__name__}"}
    if r.status_code not in (200, 201):
        body_preview = r.text[:300]
        logger.warning("stripe checkout non-2xx status=%d body=%s", r.status_code, body_preview)
        return {"error": f"stripe http_{r.status_code}", "details": body_preview}
    try:
        data = r.json()
        return {
            "checkout_url": data.get("url"),
            "session_id": data.get("id"),
            "expires_at": data.get("expires_at"),
        }
    except (KeyError, ValueError) as e:
        return {"error": f"parse_error: {type(e).__name__}"}


def verify_webhook(body: bytes, signature: str, webhook_secret: Optional[str] = None) -> dict | None:
    """Verify Stripe webhook signature (HMAC-SHA256).

    Header format: `t=<timestamp>,v1=<signature>`.
    Stripe sig spec: https://stripe.com/docs/webhooks/signatures
    """
    secret = webhook_secret or STRIPE_WEBHOOK_SECRET
    if not secret:
        logger.warning("stripe webhook: secret not configured")
        return None
    try:
        # Parse t=...,v1=... format
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in signature.split(",") if "=" in p}
        timestamp = parts.get("t", "")
        provided_sig = parts.get("v1", "")
        if not timestamp or not provided_sig:
            return None
        # Verify timestamp within 5 min (anti-replay)
        if abs(int(time.time()) - int(timestamp)) > 300:
            logger.warning("stripe webhook: timestamp drift >300s")
            return None
        # Compute expected sig
        signed_payload = f"{timestamp}.".encode() + body
        expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, provided_sig):
            logger.warning("stripe webhook: signature mismatch")
            return None
        # Valid → parse body
        import json as _json
        return _json.loads(body.decode("utf-8"))
    except Exception as e:
        logger.warning("stripe webhook verify failed: %s", type(e).__name__)
        return None


async def handle_event(event: dict, db_pool) -> dict:
    """Process verified Stripe event. Returns action taken.

    SECURITY 2026-05-26 (post-review fix #1): idempotency через atomic
    INSERT...ON CONFLICT DO NOTHING вместо SELECT-then-INSERT (race condition).
    Если RETURNING вернул event_id — это первая обработка (мы держим lock через
    PRIMARY KEY); если NULL — кто-то параллельно обработал → skip.
    """
    event_type = event.get("type", "")
    event_id = event.get("id", "")
    if not event_id:
        return {"action": "skipped_no_event_id"}

    # Atomic idempotency claim: INSERT первым ходом, если PK конфликт — skip.
    async with db_pool.acquire() as conn:
        claimed = await conn.fetchval(
            """INSERT INTO billing_processed_events (event_id, provider, processed_at, event_type)
                 VALUES ($1, 'stripe', NOW(), $2)
                 ON CONFLICT (event_id) DO NOTHING
                 RETURNING event_id""",
            event_id, event_type,
        )
        if not claimed:
            return {"action": "skipped_duplicate", "event_id": event_id}

    if event_type == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        owner = session.get("client_reference_id") or session.get("metadata", {}).get("owner_user_id")
        target_tier = session.get("metadata", {}).get("target_tier", "pro")
        if not owner:
            return {"action": "skipped_no_owner", "event_id": event_id}

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
                    "UPDATE billing_processed_events SET owner_user_id = $1::uuid WHERE event_id = $2",
                    owner, event_id,
                )
                # NOTE: INSERT в billing_processed_events уже произошёл вверху (atomic claim).
                # Здесь только обогащаем owner_user_id для audit.
        logger.info("stripe upgraded owner=%s to tier=%s", owner[:8], target_tier)
        return {"action": "tier_upgraded", "owner": owner, "new_tier": target_tier}

    if event_type == "customer.subscription.deleted":
        # TODO: implement downgrade-to-free
        return {"action": "subscription_canceled_TODO", "event_id": event_id}

    return {"action": "ignored", "event_type": event_type}


async def test_connection() -> dict:
    """Verify Stripe credentials by hitting GET /v1/customers (list, limit=1)."""
    if not is_configured():
        return {"ok": False, "message": "STRIPE_SECRET_KEY не задан в env"}
    headers = {"Authorization": f"Bearer {STRIPE_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{STRIPE_API_BASE}/customers?limit=1", headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"network: {type(e).__name__}"}
    if r.status_code == 200:
        return {"ok": True, "message": "Stripe API доступен"}
    return {"ok": False, "message": f"http_{r.status_code}: {r.text[:200]}"}
