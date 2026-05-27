"""Billing API — checkout flow + webhook handlers.

POST /api/billing/checkout/{tier}?provider=stripe|yookassa
  Создать checkout session, вернуть URL для редиректа.
  Auth: session-cookie (tenant initiated upgrade из /ui/pricing).

POST /api/billing/webhook/stripe
POST /api/billing/webhook/yookassa
  Webhook endpoints для notifications. Auth через signature verify
  (Stripe HMAC, ЮKassa source IP через nginx).

GET /api/billing/subscriptions/me
  Список моих active subscriptions (для UI dashboard).

Owner mandate 2026-05-26: «у меня есть MasterCard и VPN» → Stripe доступен.
ЮKassa — для РФ-tenants через банк-карты МИР/Сбер.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request

from app.db.postgres import get_pool
from app.security.middleware import require_user
from app.services.billing import (
    PROVIDER_LABELS,
    TIER_PRICING_RUB,
    TIER_PRICING_USD,
    get_provider,
    is_valid_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.post("/checkout/{tier}")
async def create_checkout(
    request: Request,
    tier: str = Path(..., pattern="^(pro|enterprise)$"),
    provider: str = Query("yookassa", description="stripe|yookassa"),
) -> dict:
    """Initiate checkout — возвращает URL для редиректа."""
    user = await require_user(request)
    if not is_valid_provider(provider):
        raise HTTPException(status_code=400, detail=f"Неизвестный provider: {provider}")
    if tier == "enterprise":
        raise HTTPException(
            status_code=400,
            detail="Enterprise — индивидуальная цена, свяжитесь sales@me-ai.ru",
        )

    if provider == "yookassa":
        pricing = TIER_PRICING_RUB.get(tier, {})
        amount_kopecks = pricing.get("price_kopecks") or 0
        currency = "RUB"
    else:  # stripe
        pricing = TIER_PRICING_USD.get(tier, {})
        amount_kopecks = (pricing.get("price_cents") or 0)  # cents
        currency = "USD"

    if amount_kopecks <= 0:
        raise HTTPException(status_code=400, detail=f"Tier {tier} не имеет цены")

    provider_mod = get_provider(provider)
    if not provider_mod or not provider_mod.is_configured():
        raise HTTPException(
            status_code=503,
            detail=f"{PROVIDER_LABELS[provider]} ещё не настроен платформой (env vars отсутствуют)",
        )

    base_url = str(request.base_url).rstrip("/")
    success_url = f"{base_url}/ui/profile?upgraded={tier}"
    cancel_url = f"{base_url}/ui/pricing?cancelled=1"

    result = await provider_mod.create_checkout(
        amount_kopecks=amount_kopecks,
        currency=currency,
        owner_user_id=str(user.user_id),
        target_tier=tier,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=f"{provider}: {result['error']}")
    return result


@router.post("/webhook/stripe")
async def webhook_stripe(
    request: Request,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
) -> dict:
    """Stripe webhook — verify HMAC + handle event."""
    body = await request.body()
    provider_mod = get_provider("stripe")
    if not provider_mod or not provider_mod.is_configured():
        raise HTTPException(status_code=503, detail="Stripe не настроен")

    event = provider_mod.verify_webhook(body, stripe_signature)
    if event is None:
        raise HTTPException(status_code=401, detail="Invalid signature")

    pool = await get_pool()
    result = await provider_mod.handle_event(event, pool)
    return result


@router.post("/webhook/yookassa")
async def webhook_yookassa(request: Request) -> dict:
    """ЮKassa webhook — verified by nginx source-IP whitelist."""
    body = await request.body()
    provider_mod = get_provider("yookassa")
    if not provider_mod or not provider_mod.is_configured():
        raise HTTPException(status_code=503, detail="ЮKassa не настроена")

    event = provider_mod.verify_webhook(body, signature="")
    if event is None:
        raise HTTPException(status_code=400, detail="Невалидный body")

    pool = await get_pool()
    result = await provider_mod.handle_event(event, pool)
    return result


@router.get("/subscriptions/me")
async def my_subscriptions(request: Request) -> dict:
    """Список моих active subscriptions для UI."""
    user = await require_user(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, provider, tier, status,
                      current_period_start, current_period_end, created_at
                 FROM subscriptions
                WHERE owner_user_id = $1::uuid AND status = 'active'
                ORDER BY created_at DESC""",
            user.user_id,
        )
    return {
        "subscriptions": [dict(r) for r in rows],
        "active_tier": rows[0]["tier"] if rows else "free",
    }
