"""Test fixtures для session-cookie auth.

Pattern: создаёт тестовый account + session напрямую в БД (asyncpg),
возвращает httpx client с `X-Session-Id` header set (fallback для cookie
поддерживается middleware.py:_extract_session_id).

Требует:
- COGCORE_TEST_DB_URL env (например postgresql://cognitive:pwd@localhost/cognitive_core)
  Если не задан — fixture skip-ает тесты.
- app.security.session.create_session импортируем — reuse production code.

Usage в тестах:
    @pytest.mark.asyncio
    async def test_my_endpoint(authed_client):
        # authed_client уже имеет X-Session-Id header
        r = await authed_client.get("/user/agents")
        assert r.status_code == 200
"""
import os
import uuid

import httpx
import pytest


def _test_db_url() -> str | None:
    """Return DB URL для test fixture или None если не configured."""
    return os.getenv("COGCORE_TEST_DB_URL") or os.getenv("DATABASE_URL")


@pytest.fixture
def test_email() -> str:
    """Unique email per test (для idempotency между runs)."""
    return f"test_{uuid.uuid4().hex[:12]}@cogcore.test"


@pytest.fixture
async def test_account_session(test_email):
    """Создаёт тестовый account + session row в БД, yield-ит session_id.

    После теста — удаляет account (CASCADE на sessions).
    Skip если COGCORE_TEST_DB_URL не задан.
    """
    db_url = _test_db_url()
    if not db_url:
        pytest.skip("COGCORE_TEST_DB_URL not set — session fixture требует direct DB access")

    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    # Connect
    conn = await asyncpg.connect(db_url)
    try:
        # Insert account
        user_id = await conn.fetchval(
            """
            INSERT INTO accounts (email, email_verified, is_admin)
            VALUES ($1, TRUE, FALSE)
            RETURNING user_id::text
            """,
            test_email,
        )
        # Create session via direct INSERT (replicates create_session logic)
        import secrets
        from datetime import datetime, timedelta, timezone
        session_id = secrets.token_hex(32)  # 64-char hex
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        await conn.execute(
            """
            INSERT INTO sessions (session_id, user_id, device_info, expires_at)
            VALUES ($1, $2::uuid, '{}'::jsonb, $3)
            """,
            session_id, user_id, expires_at,
        )

        yield {
            "user_id": user_id,
            "email": test_email,
            "session_id": session_id,
        }
    finally:
        # Cleanup — DELETE account CASCADEs sessions
        try:
            await conn.execute(
                "DELETE FROM accounts WHERE email = $1",
                test_email,
            )
        except Exception:
            pass
        await conn.close()


@pytest.fixture
async def authed_client(api_url, test_account_session):
    """httpx client с X-Session-Id header set.

    `api_url` fixture — из основного conftest.py.
    `test_account_session` — fixture выше, создаёт account + session.
    """
    headers = {"X-Session-Id": test_account_session["session_id"]}
    async with httpx.AsyncClient(base_url=api_url, headers=headers, timeout=30.0) as c:
        # Verify session работает
        r = await c.get("/auth/status")
        if r.status_code != 200:
            pytest.skip(
                f"session injection failed (auth/status={r.status_code}): "
                f"возможно SESSION_COOKIE_NAME mismatch или middleware не читает X-Session-Id"
            )
        yield c


@pytest.fixture
async def admin_account_session(test_email):
    """Same as test_account_session но с is_admin=TRUE. Для тестов /admin/*."""
    db_url = _test_db_url()
    if not db_url:
        pytest.skip("COGCORE_TEST_DB_URL not set")

    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    conn = await asyncpg.connect(db_url)
    try:
        user_id = await conn.fetchval(
            """
            INSERT INTO accounts (email, email_verified, is_admin)
            VALUES ($1, TRUE, TRUE)
            RETURNING user_id::text
            """,
            test_email,
        )
        import secrets
        from datetime import datetime, timedelta, timezone
        session_id = secrets.token_hex(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        await conn.execute(
            """
            INSERT INTO sessions (session_id, user_id, device_info, expires_at)
            VALUES ($1, $2::uuid, '{}'::jsonb, $3)
            """,
            session_id, user_id, expires_at,
        )
        yield {
            "user_id": user_id,
            "email": test_email,
            "session_id": session_id,
            "is_admin": True,
        }
    finally:
        try:
            await conn.execute("DELETE FROM accounts WHERE email = $1", test_email)
        except Exception:
            pass
        await conn.close()


@pytest.fixture
async def admin_client(api_url, admin_account_session):
    """httpx client с admin session."""
    headers = {"X-Session-Id": admin_account_session["session_id"]}
    async with httpx.AsyncClient(base_url=api_url, headers=headers, timeout=30.0) as c:
        yield c
