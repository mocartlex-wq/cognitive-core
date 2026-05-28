"""Тесты для admin audit-log READ endpoints (app/api/admin_audit.py, M4).

Паттерн повторяет tests/test_tenant_isolation.py: бьёмся в живой API,
health-check → skip если недоступен. Admin-сессию подделать без перехвата
email нельзя, поэтому проверяемое здесь:

  • auth-gating: без сессии → 401, с обычным (не-admin) ключом → 401/403
  • endpoint'ы зарегистрированы (не 404)
  • _mask_email / _mask_owner — чистые unit-функции, тестируются напрямую

Полные 200-проверки формы ответа требуют admin-сессии (cookie через OTP).
Они помечены skip с инструкцией — включатся когда в CI появится email-перехват
(mailpit), как описано в tests/README.md. Это держит файл честным: то что
нельзя проверить без admin-сессии — явно skip, а не ложно-зелёное.

Запуск:
    pytest tests/test_admin_audit.py -v
Требует работающий API на COGCORE_TEST_URL (default http://localhost:8000).
Без БД/API — graceful skip.
"""
import os

import httpx
import pytest

from app.api.admin_audit import _mask_email, _mask_owner

pytestmark = pytest.mark.anyio


@pytest.fixture
def api_url():
    return os.getenv("COGCORE_TEST_URL", "http://localhost:8000")


@pytest.fixture
async def client(api_url):
    async with httpx.AsyncClient(base_url=api_url, timeout=30.0) as c:
        try:
            r = await c.get("/health")
            if r.status_code != 200:
                pytest.skip(f"API not healthy at {api_url}")
        except Exception:
            pytest.skip(f"API unreachable at {api_url}")
        yield c


_NEEDS_ADMIN = "Requires admin session (OTP/mailpit email interception); см. tests/README.md"


# ─────────────────────────────────────────────────────────────────────────
# Unit: masking helpers (не требуют ни API, ни БД)
# ─────────────────────────────────────────────────────────────────────────
class TestMaskingHelpers:
    def test_mask_email_basic(self):
        assert _mask_email("john.doe@example.com") == "jo***@example.com"

    def test_mask_email_short_local(self):
        # локальная часть короче 2 символов — не падаем
        assert _mask_email("a@b.com") == "a***@b.com"

    def test_mask_email_preserves_domain(self):
        assert _mask_email("user@tenant.ru").endswith("@tenant.ru")

    def test_mask_email_none(self):
        assert _mask_email(None) == "***"

    def test_mask_email_garbage(self):
        assert _mask_email("not-an-email") == "***"

    def test_mask_owner_truncates(self):
        masked = _mask_owner("a1b2c3d4-5678-90ab-cdef-1234567890ab")
        assert masked == "a1b2c3d4…"

    def test_mask_owner_none(self):
        assert _mask_owner(None) is None


# ─────────────────────────────────────────────────────────────────────────
# Auth-gating: без сессии → 401 (НЕ 404 — значит endpoint зарегистрирован)
# ─────────────────────────────────────────────────────────────────────────
class TestAuditAuth:
    async def test_logins_requires_auth(self, client: httpx.AsyncClient):
        r = await client.get("/admin/audit/logins")
        assert r.status_code == 401, f"expected 401, got {r.status_code}"

    async def test_billing_requires_auth(self, client: httpx.AsyncClient):
        r = await client.get("/admin/audit/billing")
        assert r.status_code == 401

    async def test_agents_requires_auth(self, client: httpx.AsyncClient):
        r = await client.get("/admin/audit/agents")
        assert r.status_code == 401

    async def test_summary_requires_auth(self, client: httpx.AsyncClient):
        r = await client.get("/admin/audit/summary")
        assert r.status_code == 401

    async def test_non_admin_api_key_rejected(self, client: httpx.AsyncClient):
        """Обычный X-API-Key (агентский ключ) ≠ admin-сессия → 401/403, не 200.

        require_admin требует session-cookie с is_admin; per-agent ключ
        не даёт доступа к audit-endpoint'ам.
        """
        r = await client.get(
            "/admin/audit/summary",
            headers={"X-API-Key": "key-design-001"},
        )
        assert r.status_code in (401, 403), f"non-admin got {r.status_code}"


# ─────────────────────────────────────────────────────────────────────────
# Shape-проверки под admin-сессией (skip пока нет email-перехвата в CI)
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(
    os.getenv("COGCORE_TEST_MODE") != "1",
    reason=_NEEDS_ADMIN,
)
class TestAuditLoginsShape:
    async def test_logins_ok(self, admin_client: httpx.AsyncClient):
        r = await admin_client.get("/admin/audit/logins?limit=5&days=7")
        assert r.status_code == 200
        body = r.json()
        assert "count" in body and "items" in body
        assert isinstance(body["items"], list)
        for item in body["items"]:
            assert "@" in item["email"] and "***" in item["email"]  # masked


@pytest.mark.skipif(
    os.getenv("COGCORE_TEST_MODE") != "1",
    reason=_NEEDS_ADMIN,
)
class TestAuditBillingShape:
    async def test_billing_ok_or_note(self, admin_client: httpx.AsyncClient):
        r = await admin_client.get("/admin/audit/billing")
        assert r.status_code == 200
        body = r.json()
        assert "count" in body and "items" in body
        # billing_processed_events может отсутствовать → ожидаем note
        if not body["items"]:
            assert "note" in body


@pytest.mark.skipif(
    os.getenv("COGCORE_TEST_MODE") != "1",
    reason=_NEEDS_ADMIN,
)
class TestAuditAgentsShape:
    async def test_agents_ok(self, admin_client: httpx.AsyncClient):
        r = await admin_client.get("/admin/audit/agents?limit=5")
        assert r.status_code == 200
        body = r.json()
        assert "count" in body and "items" in body
        for item in body["items"]:
            assert item["status"] in ("online", "idle", "stale")


@pytest.mark.skipif(
    os.getenv("COGCORE_TEST_MODE") != "1",
    reason=_NEEDS_ADMIN,
)
class TestAuditSummaryShape:
    async def test_summary_ok(self, admin_client: httpx.AsyncClient):
        r = await admin_client.get("/admin/audit/summary")
        assert r.status_code == 200
        body = r.json()
        for key in ("total_accounts", "active_agents",
                    "billing_events_30d", "rooms_count"):
            assert key in body
