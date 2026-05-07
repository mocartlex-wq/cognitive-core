"""Тесты dashboard эндпоинтов (read-only browsers)."""
import pytest


@pytest.mark.asyncio
async def test_recent_events_default(client, headers):
    r = await client.get("/dashboard/recent-events", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "count" in data and "items" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_recent_events_with_limit(client, headers):
    r = await client.get("/dashboard/recent-events?limit=5", headers=headers)
    assert r.status_code == 200
    assert len(r.json()["items"]) <= 5


@pytest.mark.asyncio
async def test_recent_events_filter_by_domain(client, headers):
    r = await client.get("/dashboard/recent-events?domain=test_domain&limit=10", headers=headers)
    assert r.status_code == 200
    for ev in r.json()["items"]:
        assert ev["domain"] == "test_domain"


@pytest.mark.asyncio
async def test_audit_tail(client, headers):
    r = await client.get("/dashboard/audit-tail?limit=20", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "count" in data and "items" in data
    for item in data["items"]:
        assert "action" in item
        assert "success" in item


@pytest.mark.asyncio
async def test_audit_tail_only_failures(client, headers):
    r = await client.get("/dashboard/audit-tail?limit=10&only_failures=true", headers=headers)
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["success"] is False


@pytest.mark.asyncio
async def test_domains_endpoint(client, headers):
    r = await client.get("/dashboard/domains", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "count" in data and "items" in data
    for d in data["items"]:
        assert "domain" in d
        assert "l1" in d
        assert "l2" in d
        assert "l3_active" in d
        assert "tools_active" in d


@pytest.mark.asyncio
async def test_timeline_endpoint(client, headers):
    r = await client.get("/dashboard/timeline?days=3", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "days" in data and data["days"] == 3
    assert "l1_per_day" in data
    assert "l2_per_day" in data
    assert "audit_per_day" in data


@pytest.mark.asyncio
async def test_knowledge_endpoint(client, headers):
    r = await client.get("/dashboard/knowledge?limit=10", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "count" in data and "items" in data
    for k in data["items"]:
        assert "id" in k and "domain" in k and "type" in k
