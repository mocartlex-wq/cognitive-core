"""E2E test for billing flow with mock providers.
Tests the FULL чекаут → webhook → subscription pipeline without real Stripe/ЮKassa keys.

Goal: убедиться что при появлении реальных ключей всё работает без багов.
Pre-launch sanity check per DS recommendation.

Тестирует:
- POST /api/billing/checkout/{tier} → returns checkout_url + provider session_id
- POST /api/billing/webhook/stripe → verifies signature + updates subscription
- POST /api/billing/webhook/yookassa → verifies IP + updates subscription
- GET /api/billing/subscriptions/me → returns active subscription
- Idempotency: replay same webhook → not double-applied
- Rate limit: 6 checkouts in 60s → 429 on 6th
"""
import os
import time
import hmac
import hashlib
import json
import uuid
import pytest
from unittest.mock import patch, AsyncMock


# Fixture: mock providers settings так чтобы тесты не пытались реально звонить наружу
@pytest.fixture(autouse=True)
def mock_billing_env(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_mock_key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_mock_secret")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "mock-shop-id")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "mock-secret-key")


@pytest.fixture
def authed_client():
    """Создаёт TestClient + аутентифицирует через session-cookie."""
    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    # TODO: implement test session creation helper в app/security/session.py
    # Сейчас тесты сидят в pending пока helper не написан — это marker для backlog.
    pytest.skip("test_session_create helper not yet implemented in app/security/session.py")
    return client


# ════════════════════════════════════════════════════════════════════════════
# STRIPE TESTS
# ════════════════════════════════════════════════════════════════════════════

class TestStripeFlow:
    """Stripe checkout + webhook flow."""

    @pytest.mark.asyncio
    async def test_create_checkout_returns_url(self, authed_client):
        """POST /api/billing/checkout/pro?provider=stripe → checkout_url."""
        with patch("app.services.billing.stripe_provider.create_checkout_session",
                   AsyncMock(return_value={
                       "session_id": "cs_test_123",
                       "checkout_url": "https://checkout.stripe.com/c/cs_test_123",
                   })):
            r = authed_client.post("/api/billing/checkout/pro?provider=stripe")
            assert r.status_code == 200
            body = r.json()
            assert "checkout_url" in body
            assert body["checkout_url"].startswith("https://checkout.stripe.com/")

    @pytest.mark.asyncio
    async def test_webhook_signature_invalid_rejected(self, authed_client):
        """Stripe webhook с битой подписью → 400."""
        bad_payload = json.dumps({"type": "checkout.session.completed"})
        r = authed_client.post(
            "/api/billing/webhook/stripe",
            content=bad_payload,
            headers={"Stripe-Signature": "t=1234,v1=invalid"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_webhook_valid_signature_applies(self, authed_client):
        """Stripe webhook с валидной подписью → subscription создан."""
        secret = "whsec_mock_secret"
        timestamp = str(int(time.time()))
        payload = {
            "id": "evt_test_001",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "test-user-uuid",
                    "subscription": "sub_test_001",
                }
            }
        }
        body = json.dumps(payload, separators=(",", ":"))
        signed = f"{timestamp}.{body}"
        sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        signature_header = f"t={timestamp},v1={sig}"

        r = authed_client.post(
            "/api/billing/webhook/stripe",
            content=body,
            headers={"Stripe-Signature": signature_header},
        )
        assert r.status_code == 200
        assert r.json()["action"] in ("subscription_activated", "processed")

    @pytest.mark.asyncio
    async def test_webhook_idempotency_no_double_apply(self, authed_client):
        """Replay same Stripe webhook event_id → 200 но action=skipped_duplicate."""
        secret = "whsec_mock_secret"
        timestamp = str(int(time.time()))
        payload = {"id": "evt_dup_test", "type": "checkout.session.completed", "data": {}}
        body = json.dumps(payload, separators=(",", ":"))
        signed = f"{timestamp}.{body}"
        sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()

        # First call — apply
        r1 = authed_client.post(
            "/api/billing/webhook/stripe",
            content=body,
            headers={"Stripe-Signature": f"t={timestamp},v1={sig}"},
        )
        assert r1.status_code == 200

        # Second call — should skip
        r2 = authed_client.post(
            "/api/billing/webhook/stripe",
            content=body,
            headers={"Stripe-Signature": f"t={timestamp},v1={sig}"},
        )
        assert r2.status_code == 200
        assert r2.json().get("action") == "skipped_duplicate"


# ════════════════════════════════════════════════════════════════════════════
# YOOKASSA TESTS
# ════════════════════════════════════════════════════════════════════════════

class TestYooKassaFlow:
    """ЮKassa specific tests — IP whitelist + similar idempotency."""

    @pytest.mark.asyncio
    async def test_webhook_from_unknown_ip_rejected(self, authed_client):
        """ЮKassa webhook from unknown IP → 403."""
        payload = {
            "event": "payment.succeeded",
            "object": {"id": "test-payment-uuid", "metadata": {}},
        }
        r = authed_client.post(
            "/api/billing/webhook/yookassa",
            json=payload,
            headers={"X-Real-IP": "8.8.8.8"},  # not in ЮKassa range
        )
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_webhook_from_yookassa_ip_accepted(self, authed_client):
        """ЮKassa webhook from valid IP (185.71.76.x) → 200."""
        payload = {
            "event": "payment.succeeded",
            "object": {"id": "test-yk-payment-001", "metadata": {"user_id": "test-uuid"}},
        }
        r = authed_client.post(
            "/api/billing/webhook/yookassa",
            json=payload,
            headers={"X-Real-IP": "185.71.76.10"},
        )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_webhook_missing_required_fields_rejected(self, authed_client):
        """ЮKassa webhook без event или object.id → 400."""
        for bad in [{}, {"event": "x"}, {"object": {}}, {"event": "x", "object": {}}]:
            r = authed_client.post(
                "/api/billing/webhook/yookassa",
                json=bad,
                headers={"X-Real-IP": "185.71.76.10"},
            )
            assert r.status_code == 400, f"failed for payload {bad}"


# ════════════════════════════════════════════════════════════════════════════
# RATE LIMIT TEST
# ════════════════════════════════════════════════════════════════════════════

class TestRateLimit:
    @pytest.mark.asyncio
    async def test_checkout_rate_limit_5_per_60s(self, authed_client):
        """6 checkout requests за 60s → 6й возвращает 429."""
        with patch("app.services.billing.stripe_provider.create_checkout_session",
                   AsyncMock(return_value={"session_id": "cs_x", "checkout_url": "https://x"})):
            for i in range(5):
                r = authed_client.post("/api/billing/checkout/pro?provider=stripe")
                assert r.status_code == 200, f"request {i+1}/5 failed unexpectedly: {r.status_code}"
            # 6th should be rate-limited
            r = authed_client.post("/api/billing/checkout/pro?provider=stripe")
            assert r.status_code == 429, f"expected 429, got {r.status_code}"


# ════════════════════════════════════════════════════════════════════════════
# GET SUBSCRIPTION TEST
# ════════════════════════════════════════════════════════════════════════════

class TestSubscriptionStatus:
    @pytest.mark.asyncio
    async def test_get_my_subscription_no_sub_returns_free(self, authed_client):
        """GET /subscriptions/me для user без подписки → tier=free."""
        r = authed_client.get("/api/billing/subscriptions/me")
        assert r.status_code == 200
        body = r.json()
        assert body.get("tier") == "free"
        assert body.get("status") in (None, "free", "no_subscription")


# ════════════════════════════════════════════════════════════════════════════
# NOTE для maintainer: чтобы запустить эти тесты, нужно сделать в conftest.py:
#   1. test_session_create() helper — INSERT в accounts + sessions с тест-cookie
#   2. cleanup fixture — очищать billing_processed_events / subscriptions tables после каждого теста
#   3. Postgres test DB — отдельная (или Docker compose в CI) чтобы не трогать production
#
# Текущий статус: маркер для CI — все тесты с pytest.skip пока helper не написан.
# Это «scaffolding» — когда придут реальные Stripe/ЮKassa ключи, тесты можно
# сразу запускать (вместе с написанным helper) для verification.
# ════════════════════════════════════════════════════════════════════════════
