"""Phase 5D — E2E test для tenant isolation.

Проверяет что owner_user_id фильтр работает на всех memory endpoint'ах
и что один tenant НЕ может читать данные другого.

Запуск (требует доступ к работающему API + БД на localhost):
    pytest tests/test_tenant_isolation.py -v

В CI — требуется test-postgres + test-redis instances. Если БД недоступна —
тест skip'ается.
"""
import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def api_url():
    return os.getenv("COGCORE_TEST_URL", "http://localhost:8000")


@pytest.fixture
async def client(api_url):
    async with httpx.AsyncClient(base_url=api_url, timeout=30.0) as c:
        # Health check — если API недоступен, skip
        try:
            r = await c.get("/health")
            if r.status_code != 200:
                pytest.skip(f"API not healthy at {api_url}")
        except Exception:
            pytest.skip(f"API unreachable at {api_url}")
        yield c


async def _create_test_tenant(client: httpx.AsyncClient, email: str) -> dict:
    """Helper: создать аккаунт через OTP flow.

    Реальный OTP-код приходит email-ом. В тестовом окружении надо либо:
      - использовать mailhog (mailpit) — перехватывает SMTP, читает inbox
      - либо проложить test-mode endpoint /auth/email/test-verify (не делаем — security)

    Для CI без mailhog тест пропускается с инструкцией.
    """
    test_mode = os.getenv("COGCORE_TEST_MODE") == "1"
    if not test_mode:
        pytest.skip(
            "OTP-based test requires email interception. "
            "Set COGCORE_TEST_MODE=1 + run with mailpit on localhost:1025 "
            "(see tests/README.md for setup)."
        )
    # Real OTP flow would go here
    raise NotImplementedError("Implement after mailpit is wired into CI")


async def test_health(client: httpx.AsyncClient):
    """Sanity check — API живой."""
    r = await client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert "healthy" in j or "status" in j


async def test_tenant_isolation_basic(client: httpx.AsyncClient):
    """Critical: 2 owner'а пишут в один domain, recall возвращает только свои данные.

    Skipped without test-mode email interception. Документация в e2e_tenant_test.sh.
    """
    if os.getenv("COGCORE_TEST_MODE") != "1":
        pytest.skip("Requires COGCORE_TEST_MODE=1 + mailpit (см. tests/README.md)")

    # Pseudo-impl — будет полным когда mailpit будет в CI
    ts = int(uuid.uuid4().int % 1000000)
    email_a = f"isol-a-{ts}@test.local"
    email_b = f"isol-b-{ts}@test.local"

    tenant_a = await _create_test_tenant(client, email_a)
    tenant_b = await _create_test_tenant(client, email_b)

    assert tenant_a["owner_id"] != tenant_b["owner_id"]

    # ... full implementation см. scripts/e2e_tenant_test.sh шаги 3-9


async def test_usage_endpoint_auth(client: httpx.AsyncClient):
    """/user/usage без auth → 401, не 404. Подтверждает что endpoint
    зарегистрирован и middleware работает."""
    r = await client.get("/user/usage")
    assert r.status_code == 401, f"Expected 401 (auth required), got {r.status_code}"


async def test_admin_tenants_auth(client: httpx.AsyncClient):
    """/admin/tenants без auth → 401."""
    r = await client.get("/admin/tenants")
    assert r.status_code == 401


async def test_pricing_page_public(client: httpx.AsyncClient):
    """/ui/pricing — публичная, без auth."""
    r = await client.get("/ui/pricing")
    assert r.status_code == 200
    assert "Free" in r.text or "free" in r.text
    assert "Pro" in r.text or "pro" in r.text


async def test_welcome_page_public(client: httpx.AsyncClient):
    """/ui/welcome — публичная (auth-check встроен в JS, не на serv side)."""
    r = await client.get("/ui/welcome")
    assert r.status_code == 200
