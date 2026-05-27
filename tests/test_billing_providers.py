"""Tests для billing providers scaffold (Stripe + ЮKassa).

Покрывают:
  - registry: PROVIDER_REGISTRY, TIER_PRICING_RUB/USD, TIER_LIMITS
  - stripe: is_configured (env-based), verify_webhook (HMAC SHA-256),
    checkout error-paths
  - yookassa: is_configured, _auth_header (Basic base64), verify_webhook
    (passthrough JSON parse)
  - HTTP mocking — без живых вызовов к Stripe/ЮKassa

Real integration tests (требуют sandbox creds) — отдельный файл, off by default.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import patch

import pytest

from app.services.billing import (
    PROVIDER_REGISTRY,
    TIER_LIMITS,
    TIER_PRICING_RUB,
    TIER_PRICING_USD,
    is_valid_provider,
    stripe_provider,
    yookassa_provider,
)


# ───────── Registry ─────────
def test_registry_has_both_providers():
    assert "stripe" in PROVIDER_REGISTRY
    assert "yookassa" in PROVIDER_REGISTRY
    assert is_valid_provider("stripe") is True
    assert is_valid_provider("yookassa") is True
    assert is_valid_provider("paypal") is False


def test_tier_pricing_canonical():
    assert TIER_PRICING_RUB["free"]["price_kopecks"] == 0
    assert TIER_PRICING_RUB["pro"]["price_kopecks"] == 49000  # 490₽
    assert TIER_PRICING_USD["pro"]["price_cents"] == 500  # $5
    assert TIER_PRICING_RUB["enterprise"]["price_kopecks"] is None  # custom


def test_tier_limits_increasing():
    assert TIER_LIMITS["free"]["max_events_per_day"] < TIER_LIMITS["pro"]["max_events_per_day"]
    assert TIER_LIMITS["pro"]["max_events_per_day"] < TIER_LIMITS["enterprise"]["max_events_per_day"]


# ───────── Stripe: is_configured ─────────
def test_stripe_is_configured_no_env():
    with patch.object(stripe_provider, "STRIPE_SECRET_KEY", ""):
        assert stripe_provider.is_configured() is False


def test_stripe_is_configured_with_env():
    with patch.object(stripe_provider, "STRIPE_SECRET_KEY", "sk_test_abc"):
        assert stripe_provider.is_configured() is True


# ───────── Stripe: webhook signature ─────────
def test_stripe_verify_webhook_valid_sig():
    secret = "whsec_test123"
    body = b'{"id":"evt_1","type":"checkout.session.completed"}'
    timestamp = str(int(time.time()))
    signed = f"{timestamp}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={timestamp},v1={sig}"

    result = stripe_provider.verify_webhook(body, header, webhook_secret=secret)
    assert result is not None
    assert result["id"] == "evt_1"
    assert result["type"] == "checkout.session.completed"


def test_stripe_verify_webhook_invalid_sig():
    body = b'{"id":"evt_1"}'
    timestamp = str(int(time.time()))
    header = f"t={timestamp},v1=DEADBEEF"
    result = stripe_provider.verify_webhook(body, header, webhook_secret="wrong-secret")
    assert result is None


def test_stripe_verify_webhook_old_timestamp_rejected():
    secret = "whsec_test"
    body = b'{}'
    old_ts = str(int(time.time()) - 600)  # 10 минут назад → >300s threshold
    signed = f"{old_ts}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={old_ts},v1={sig}"
    result = stripe_provider.verify_webhook(body, header, webhook_secret=secret)
    assert result is None  # rejected anti-replay


def test_stripe_verify_webhook_no_secret_configured():
    """Если webhook_secret пустой — verify обязан вернуть None (не silent-accept)."""
    result = stripe_provider.verify_webhook(b'{}', "t=0,v1=x", webhook_secret="")
    assert result is None


# ───────── Stripe: checkout error paths ─────────
@pytest.mark.asyncio
async def test_stripe_checkout_no_secret_key():
    with patch.object(stripe_provider, "STRIPE_SECRET_KEY", ""):
        result = await stripe_provider.create_checkout(
            amount_kopecks=500, currency="USD", owner_user_id="abc",
            target_tier="pro", success_url="http://s", cancel_url="http://c",
        )
    assert "error" in result
    assert "STRIPE_SECRET_KEY" in result["error"]


@pytest.mark.asyncio
async def test_stripe_checkout_no_price_for_tier():
    with patch.object(stripe_provider, "STRIPE_SECRET_KEY", "sk_test"), \
         patch.object(stripe_provider, "STRIPE_PRICES", {"pro": ""}):
        result = await stripe_provider.create_checkout(
            amount_kopecks=500, currency="USD", owner_user_id="abc",
            target_tier="pro", success_url="http://s", cancel_url="http://c",
        )
    assert "error" in result
    assert "price_id" in result["error"]


# ───────── ЮKassa: is_configured ─────────
def test_yookassa_is_configured():
    with patch.object(yookassa_provider, "YOOKASSA_SHOP_ID", ""), \
         patch.object(yookassa_provider, "YOOKASSA_SECRET_KEY", ""):
        assert yookassa_provider.is_configured() is False

    with patch.object(yookassa_provider, "YOOKASSA_SHOP_ID", "12345"), \
         patch.object(yookassa_provider, "YOOKASSA_SECRET_KEY", "live_xyz"):
        assert yookassa_provider.is_configured() is True


def test_yookassa_auth_header_basic_base64():
    """Verify Basic Auth header format (base64 of shopId:secret)."""
    import base64
    with patch.object(yookassa_provider, "YOOKASSA_SHOP_ID", "shop123"), \
         patch.object(yookassa_provider, "YOOKASSA_SECRET_KEY", "secret-xyz"):
        header = yookassa_provider._auth_header()
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "shop123:secret-xyz"


# ───────── ЮKassa: webhook (post-review hardening) ─────────
def test_yookassa_verify_webhook_valid_json():
    """Body с required fields + IP не передан → проходит layer 2 validation."""
    body = b'{"event":"payment.succeeded","object":{"id":"pay_1"}}'
    result = yookassa_provider.verify_webhook(body, signature="")
    assert result["event"] == "payment.succeeded"


def test_yookassa_verify_webhook_invalid_json():
    result = yookassa_provider.verify_webhook(b"not-json", signature="")
    assert result is None


def test_yookassa_verify_webhook_missing_required_fields():
    """Body без object.id → reject (защита от malformed webhook)."""
    body = b'{"event":"payment.succeeded"}'
    assert yookassa_provider.verify_webhook(body, signature="") is None
    body2 = b'{"event":"payment.succeeded","object":{}}'
    assert yookassa_provider.verify_webhook(body2, signature="") is None


def test_yookassa_verify_webhook_rejects_non_whitelisted_ip():
    """IP не в YOOKASSA_ALLOWED_IP_RANGES → reject (defense-in-depth)."""
    body = b'{"event":"payment.succeeded","object":{"id":"pay_1"}}'
    # Random IP (Google DNS) — точно не ЮKassa
    result = yookassa_provider.verify_webhook(body, signature="", client_ip="8.8.8.8")
    assert result is None


def test_yookassa_verify_webhook_accepts_whitelisted_ip():
    """IP из whitelist (185.71.76.5 в 185.71.76.0/27) → pass."""
    body = b'{"event":"payment.succeeded","object":{"id":"pay_1"}}'
    result = yookassa_provider.verify_webhook(body, signature="", client_ip="185.71.76.5")
    assert result is not None
    assert result["event"] == "payment.succeeded"


def test_yookassa_is_yookassa_ip_ranges():
    """Unit test для _is_yookassa_ip helper."""
    assert yookassa_provider._is_yookassa_ip("185.71.76.0") is True
    assert yookassa_provider._is_yookassa_ip("185.71.76.31") is True   # /27 boundary
    assert yookassa_provider._is_yookassa_ip("185.71.76.32") is False  # outside /27
    assert yookassa_provider._is_yookassa_ip("77.75.156.11") is True
    assert yookassa_provider._is_yookassa_ip("77.75.156.12") is False  # /32 not 11
    assert yookassa_provider._is_yookassa_ip("8.8.8.8") is False
    assert yookassa_provider._is_yookassa_ip("not-an-ip") is False     # malformed


# ───────── ЮKassa: checkout errors ─────────
@pytest.mark.asyncio
async def test_yookassa_checkout_not_configured():
    with patch.object(yookassa_provider, "YOOKASSA_SHOP_ID", ""):
        result = await yookassa_provider.create_checkout(
            amount_kopecks=49000, currency="RUB", owner_user_id="abc",
            target_tier="pro", success_url="http://s", cancel_url="http://c",
        )
    assert "error" in result
    assert "не настроена" in result["error"] or "YOOKASSA" in result["error"]


@pytest.mark.asyncio
async def test_yookassa_checkout_rejects_non_rub():
    with patch.object(yookassa_provider, "YOOKASSA_SHOP_ID", "12345"), \
         patch.object(yookassa_provider, "YOOKASSA_SECRET_KEY", "sec"):
        result = await yookassa_provider.create_checkout(
            amount_kopecks=500, currency="USD", owner_user_id="abc",
            target_tier="pro", success_url="http://s", cancel_url="http://c",
        )
    assert "error" in result
    assert "RUB" in result["error"]
