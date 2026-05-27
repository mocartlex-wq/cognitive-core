"""Unit tests для app/services/quota_enforcer.py (PR #119a M1 Test Foundation).

Pure-logic tests with mocked Request + DB pool. No FastAPI test-client,
no postgres — runs без COGCORE_TEST_DB_URL.

Module under test: per-owner quota enforcement (events/day, agents, storage_mb).
Все enforce_* функции read row из owner_quotas и поднимают HTTPException
если over-limit (429) или suspended (403).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.quota_enforcer import (
    _check_suspended,
    _get_quota,
    enforce_agent_quota,
    enforce_event_quota,
    enforce_storage_quota,
    get_owner_usage_summary,
)

OWNER_UUID = "00000000-0000-0000-0000-000000000001"


def _make_request(user_id: str | None = OWNER_UUID) -> MagicMock:
    """Mock Request с request.state SimpleNamespace (для атрибутов).

    request.state must allow setattr (_owner_quota cache) — SimpleNamespace ok.
    """
    req = MagicMock()
    req.state = SimpleNamespace(user_id=user_id)
    return req


def _make_quota(
    *,
    events_today: int = 0,
    max_events_per_day: int = 10000,
    storage_mb_now: float = 0.0,
    max_storage_mb: int = 1024,
    agents_count: int = 0,
    max_agents: int = 10,
    tier: str = "free",
    suspended: bool = False,
) -> dict:
    return {
        "events_today": events_today,
        "max_events_per_day": max_events_per_day,
        "storage_mb_now": storage_mb_now,
        "max_storage_mb": max_storage_mb,
        "agents_count": agents_count,
        "max_agents": max_agents,
        "max_recall_per_min": 30,
        "tier": tier,
        "suspended": suspended,
    }


def _patch_pool(quota_row: dict | None):
    """Patch app.services.quota_enforcer.get_pool → fake pool returning quota_row.

    Возвращает context-manager-patch, который надо использовать
    как `with _patch_pool(row):`.
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=quota_row)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return patch(
        "app.services.quota_enforcer.get_pool",
        new=AsyncMock(return_value=pool),
    )


def _patch_owner(owner: str | None):
    return patch(
        "app.services.quota_enforcer.resolve_owner_user_id",
        new=AsyncMock(return_value=owner),
    )


@pytest.mark.asyncio
class TestGetQuota:
    async def test_returns_dict_for_existing_owner(self):
        req = _make_request()
        row = _make_quota(events_today=42)
        with _patch_pool(row):
            res = await _get_quota(req, OWNER_UUID)
        assert res is not None
        assert res["events_today"] == 42
        assert res["_owner"] == OWNER_UUID

    async def test_returns_none_for_missing_owner(self):
        req = _make_request()
        with _patch_pool(None):
            res = await _get_quota(req, OWNER_UUID)
        assert res is None

    async def test_uses_request_state_cache_on_second_call(self):
        req = _make_request()
        row = _make_quota(events_today=7)
        with _patch_pool(row) as pool_patch:
            await _get_quota(req, OWNER_UUID)
            # 2nd call — must hit cache, NOT fetch снова
            cached = await _get_quota(req, OWNER_UUID)
        assert cached["events_today"] == 7
        # get_pool вызван 1 раз — второй из кеша
        assert pool_patch.call_count == 1


@pytest.mark.asyncio
class TestCheckSuspended:
    async def test_none_quota_allows(self):
        # никаких exceptions для None
        await _check_suspended(None)

    async def test_non_suspended_allows(self):
        await _check_suspended(_make_quota(suspended=False))

    async def test_suspended_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            await _check_suspended(_make_quota(suspended=True))
        assert exc.value.status_code == 403
        assert "suspended" in exc.value.detail.lower()


@pytest.mark.asyncio
class TestEnforceEventQuota:
    async def test_under_quota_passes(self):
        req = _make_request()
        row = _make_quota(events_today=100, max_events_per_day=10000)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            await enforce_event_quota(req)  # no raise

    async def test_at_quota_raises_429_with_retry_after(self):
        req = _make_request()
        row = _make_quota(events_today=10000, max_events_per_day=10000)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            with pytest.raises(HTTPException) as exc:
                await enforce_event_quota(req)
        assert exc.value.status_code == 429
        assert "events quota exceeded" in exc.value.detail
        # Retry-After header — это quota-enforcer-specific UX
        assert exc.value.headers == {"Retry-After": "3600"}

    async def test_no_owner_bypasses(self):
        """admin/legacy env-key flow: resolve_owner_user_id → None → skip check."""
        req = _make_request(user_id=None)
        with _patch_owner(None):
            # Even если DB вернул over-quota row — bypass happens до _get_quota
            await enforce_event_quota(req)

    async def test_missing_row_does_not_block(self):
        """row отсутствует в owner_quotas — НЕ блокируем (странно, но safe)."""
        req = _make_request()
        with _patch_owner(OWNER_UUID), _patch_pool(None):
            await enforce_event_quota(req)

    async def test_suspended_owner_raises_403(self):
        req = _make_request()
        row = _make_quota(suspended=True, events_today=0)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            with pytest.raises(HTTPException) as exc:
                await enforce_event_quota(req)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
class TestEnforceAgentQuota:
    async def test_under_quota_passes(self):
        req = _make_request()
        row = _make_quota(agents_count=3, max_agents=10)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            await enforce_agent_quota(req)

    async def test_at_quota_raises_429(self):
        req = _make_request()
        row = _make_quota(agents_count=10, max_agents=10)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            with pytest.raises(HTTPException) as exc:
                await enforce_agent_quota(req)
        assert exc.value.status_code == 429
        assert "agents quota exceeded" in exc.value.detail

    async def test_no_owner_bypasses(self):
        req = _make_request(user_id=None)
        with _patch_owner(None):
            await enforce_agent_quota(req)


@pytest.mark.asyncio
class TestEnforceStorageQuota:
    async def test_under_quota_passes(self):
        req = _make_request()
        row = _make_quota(storage_mb_now=500.0, max_storage_mb=1024)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            await enforce_storage_quota(req, extra_mb=10.0)

    async def test_projected_over_raises_429(self):
        """extra_mb makes current + extra > max → 429."""
        req = _make_request()
        row = _make_quota(storage_mb_now=1020.0, max_storage_mb=1024)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            with pytest.raises(HTTPException) as exc:
                await enforce_storage_quota(req, extra_mb=10.0)
        # NB: реализация поднимает 429 (НЕ 413), хоть smell-of-413
        assert exc.value.status_code == 429
        assert "storage quota exceeded" in exc.value.detail

    async def test_zero_extra_at_limit_passes(self):
        """boundary: storage_mb_now == max, extra_mb=0 → projected == max → ok."""
        req = _make_request()
        row = _make_quota(storage_mb_now=1024.0, max_storage_mb=1024)
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            await enforce_storage_quota(req, extra_mb=0.0)

    async def test_no_owner_bypasses(self):
        req = _make_request(user_id=None)
        with _patch_owner(None):
            await enforce_storage_quota(req, extra_mb=999.0)


@pytest.mark.asyncio
class TestGetOwnerUsageSummary:
    async def test_returns_dict_with_pct(self):
        req = _make_request()
        row = _make_quota(
            events_today=2500,
            max_events_per_day=10000,
            storage_mb_now=256.0,
            max_storage_mb=1024,
            agents_count=2,
            max_agents=10,
            tier="pro",
        )
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            summary = await get_owner_usage_summary(req)
        assert summary is not None
        assert summary["tier"] == "pro"
        assert summary["events"] == {"used": 2500, "max": 10000, "pct": 25.0}
        assert summary["storage_mb"] == {"used": 256.0, "max": 1024, "pct": 25.0}
        assert summary["agents"] == {"used": 2, "max": 10, "pct": 20.0}
        assert summary["suspended"] is False

    async def test_no_owner_returns_none(self):
        req = _make_request(user_id=None)
        with _patch_owner(None):
            assert await get_owner_usage_summary(req) is None

    async def test_missing_quota_returns_none(self):
        req = _make_request()
        with _patch_owner(OWNER_UUID), _patch_pool(None):
            assert await get_owner_usage_summary(req) is None

    async def test_pct_handles_zero_max_safely(self):
        """max=0 → division by max(1, 0) = 1 — не падает ZeroDivisionError."""
        req = _make_request()
        row = _make_quota(
            events_today=0,
            max_events_per_day=0,
            storage_mb_now=0.0,
            max_storage_mb=0,
            agents_count=0,
            max_agents=0,
        )
        with _patch_owner(OWNER_UUID), _patch_pool(row):
            summary = await get_owner_usage_summary(req)
        assert summary["events"]["pct"] == 0.0
        assert summary["storage_mb"]["pct"] == 0.0
        assert summary["agents"]["pct"] == 0.0
