"""Tests для /admin/slo endpoints (M2 PR в v1.0 roadmap).

Destination: tests/test_admin_slo.py

Skip-graceful если COGCORE_TEST_DB_URL / API не доступен (см. conftest pattern
из test_tenant_isolation.py).
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def api_url() -> str:
    return os.getenv("COGCORE_TEST_URL", "http://localhost:8000")


@pytest.fixture
async def client(api_url: str):
    """Skip если API не доступен."""
    if not os.getenv("COGCORE_TEST_DB_URL"):
        pytest.skip("COGCORE_TEST_DB_URL not set — SLO tests требуют live API + DB")
    async with httpx.AsyncClient(base_url=api_url, timeout=15.0) as c:
        try:
            r = await c.get("/health")
            if r.status_code != 200:
                pytest.skip(f"API not healthy at {api_url}")
        except Exception:
            pytest.skip(f"API unreachable at {api_url}")
        yield c


@pytest.fixture
async def admin_client(client: httpx.AsyncClient):
    """admin_client fixture (PR #115). Fallback к X-Session-Id из env."""
    sid = os.getenv("COGCORE_ADMIN_SESSION_ID")
    if not sid:
        pytest.skip(
            "COGCORE_ADMIN_SESSION_ID not set — admin endpoints "
            "требуют admin session (см. tests/fixtures/session.py)"
        )
    client.headers["X-Session-Id"] = sid
    return client


# ─────────────────────────────────────────────────────────────────────────
# TestSLOAuth — non-admin should get 403/401
# ─────────────────────────────────────────────────────────────────────────
class TestSLOAuth:
    async def test_slo_status_requires_auth(self, client: httpx.AsyncClient):
        """GET /admin/slo/ без сессии → 401."""
        r = await client.get("/admin/slo/")
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]}"

    async def test_slo_budget_requires_auth(self, client: httpx.AsyncClient):
        """GET /admin/slo/budget без сессии → 401."""
        r = await client.get("/admin/slo/budget")
        assert r.status_code == 401

    async def test_slo_targets_requires_auth(self, client: httpx.AsyncClient):
        """GET /admin/slo/targets без сессии → 401."""
        r = await client.get("/admin/slo/targets")
        assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────────
# TestSLOStatus — happy path для status endpoint
# ─────────────────────────────────────────────────────────────────────────
class TestSLOStatus:
    async def test_status_shape(self, admin_client: httpx.AsyncClient):
        """Возвращает window + indicators + violations + alerts."""
        r = await admin_client.get("/admin/slo/")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["window"] == "28d_rolling"
        assert "computed_at" in j
        assert "indicators" in j
        assert "violations_24h" in j
        assert "alerts" in j

    async def test_status_indicators_present(self, admin_client: httpx.AsyncClient):
        """Все 4 SLO indicators должны быть в ответе."""
        r = await admin_client.get("/admin/slo/")
        assert r.status_code == 200
        indicators = r.json()["indicators"]
        expected = {"availability", "latency_p95_memory", "error_rate", "postgres_p95"}
        assert set(indicators.keys()) == expected

    async def test_status_each_indicator_has_compliant_flag(
        self, admin_client: httpx.AsyncClient
    ):
        """Каждый indicator имеет bool 'compliant' — для UI light/dark."""
        r = await admin_client.get("/admin/slo/")
        for name, ind in r.json()["indicators"].items():
            assert "compliant" in ind, f"{name} missing 'compliant'"
            assert isinstance(ind["compliant"], bool)


# ─────────────────────────────────────────────────────────────────────────
# TestSLOBudget — error budget endpoint
# ─────────────────────────────────────────────────────────────────────────
class TestSLOBudget:
    async def test_budget_shape(self, admin_client: httpx.AsyncClient):
        """Budget endpoint возвращает availability + latency budgets."""
        r = await admin_client.get("/admin/slo/budget")
        assert r.status_code == 200, r.text
        j = r.json()
        assert "availability_budget" in j
        assert "latency_budget" in j

    async def test_availability_budget_math(self, admin_client: httpx.AsyncClient):
        """allowed_downtime_min_per_28d ≈ 201 для target=99.5% за 28d.

        28d * 24h * 60min * (1-0.995) = 40320 * 0.005 = 201.6 min.
        """
        r = await admin_client.get("/admin/slo/budget")
        avail = r.json()["availability_budget"]
        assert avail["target"] == 0.995
        # допускаем небольшую погрешность от округления
        assert 200 <= avail["allowed_downtime_min_per_28d"] <= 202
        assert avail["consumed_min"] >= 0
        assert avail["remaining_min"] >= 0

    async def test_latency_budget_present(self, admin_client: httpx.AsyncClient):
        """Latency budget tracking violations count."""
        r = await admin_client.get("/admin/slo/budget")
        lat = r.json()["latency_budget"]
        assert lat["target_p95_ms"] == 300
        assert "violations_count" in lat
        assert "violations_allowed_per_28d" in lat
