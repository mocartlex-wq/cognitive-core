"""Billing providers — subscription + payment processing.

Каждый provider exposes:

    async def create_checkout(
        amount_kopecks: int,        # 1₽ = 100 копеек (canonical RU unit)
        currency: str,              # "RUB" | "USD"
        owner_user_id: str,
        target_tier: str,           # "pro" | "enterprise"
        success_url: str,
        cancel_url: str,
    ) -> dict:
        # Returns {"checkout_url": "...", "session_id": "..."}
        # OR {"error": "...", "details": "..."}

    def verify_webhook(
        body: bytes,
        signature: str,             # из request header
        webhook_secret: str,
    ) -> dict | None:
        # Returns parsed event (dict) если подпись валидна
        # OR None если invalid (caller выдаёт 401)

    async def handle_event(
        event: dict,
        db_pool: Pool,
    ) -> dict:
        # Process payment.succeeded / subscription.created etc.
        # Updates owner_quotas.tier accordingly.
        # Returns {"action": "tier_upgraded", "owner": "...", "new_tier": "..."}

## Поддерживаемые providers (post-launch 2026-05-26)

| Provider | Регион | Карты | Когда использовать |
|---|---|---|---|
| `stripe` | США/EU | MasterCard / Visa / Apple Pay | Foreign tenants, $ / € прайс |
| `yookassa` | РФ | МИР / Visa РФ / Сбер ID | RU tenants, ₽ прайс |

Owner mandate 2026-05-26: «у меня есть MasterCard и VPN» — оба
provider'а доступны. Routing по геолокации tenant'а (или ручной выбор).

## Tier model

Существующая таблица `owner_quotas` (migration 0007) уже имеет
`tier TEXT DEFAULT 'free'`. Billing handlers просто UPDATE этой
колонки + bump max_events_per_day / max_storage_mb по плану tier.

| Tier | Цена | events/day | storage | agents | recall/min |
|---|---|---|---|---|---|
| free | 0₽ | 10 000 | 1 GB | 10 | 30 |
| pro | 490₽/мес ($5) | 100 000 | 10 GB | 50 | 100 |
| enterprise | от 50 000₽/мес | unlimited | 1 TB | 500 | 500 |

## SECURITY notes

- Webhook signature ОБЯЗАТЕЛЬНО verify (HMAC-SHA256 для Stripe,
  Notification от ЮKassa с RSA-проверкой). Без verify любой могут
  поднять tier через fake webhook.
- Idempotent processing: каждое event имеет `id`, сохраняем в таблицу
  `billing_processed_events` чтобы не upgrade'нуть дважды при retry.
- Subscriptions persist в новой таблице `subscriptions` (создаётся
  migration 0014 когда owner начнёт реально интегрировать).
"""
from __future__ import annotations

from . import stripe_provider, yookassa_provider

PROVIDER_LABELS: dict[str, str] = {
    "stripe":   "Stripe (foreign cards)",
    "yookassa": "ЮKassa (РФ карты)",
}

PROVIDER_REGISTRY = {
    "stripe":   stripe_provider,
    "yookassa": yookassa_provider,
}


def get_provider(provider: str):
    return PROVIDER_REGISTRY.get(provider)


def is_valid_provider(provider: str) -> bool:
    return provider in PROVIDER_REGISTRY


# Tier pricing — single source of truth для UI + billing
TIER_PRICING_RUB = {
    "free":       {"price_kopecks": 0,     "label": "Free"},
    "pro":        {"price_kopecks": 49000, "label": "Pro (490₽/мес)"},
    "enterprise": {"price_kopecks": None,  "label": "Enterprise (по запросу)"},
}

TIER_PRICING_USD = {
    "free":       {"price_cents": 0,    "label": "Free"},
    "pro":        {"price_cents": 500,  "label": "Pro ($5/mo)"},
    "enterprise": {"price_cents": None, "label": "Enterprise (custom)"},
}

# Tier limits — applied to owner_quotas при upgrade
TIER_LIMITS = {
    "free":       {"max_events_per_day": 10000,   "max_storage_mb": 1024,    "max_agents": 10,  "max_recall_per_min": 30},
    "pro":        {"max_events_per_day": 100000,  "max_storage_mb": 10240,   "max_agents": 50,  "max_recall_per_min": 100},
    "enterprise": {"max_events_per_day": 1000000, "max_storage_mb": 1048576, "max_agents": 500, "max_recall_per_min": 500},
}
