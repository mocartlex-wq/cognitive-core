"""Тесты глобального tools-registry эндпоинта."""
import pytest


@pytest.mark.asyncio
async def test_tools_registry_basic(client, headers):
    r = await client.get("/dashboard/tools-registry", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "totals" in data and "by_type" in data and "items" in data and "count" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_tools_registry_groups_by_name(client, headers):
    """Одинаковые tool_name в разных доменах объединяются в одну строку."""
    r = await client.get("/dashboard/tools-registry?limit=500", headers=headers)
    data = r.json()
    names = [t["tool_name"] for t in data["items"]]
    assert len(names) == len(set(names)), "tool_name должны быть уникальны (group by name)"


@pytest.mark.asyncio
async def test_tools_registry_sort_options(client, headers):
    """Все 4 варианта сортировки работают."""
    for sort in ["instances", "domains", "recent", "name"]:
        r = await client.get(f"/dashboard/tools-registry?sort={sort}&limit=10", headers=headers)
        assert r.status_code == 200, f"sort={sort} failed"


@pytest.mark.asyncio
async def test_tools_registry_type_filter(client, headers):
    """Type-фильтр возвращает только указанный тип."""
    r = await client.get("/dashboard/tools-registry?type_filter=api&limit=50", headers=headers)
    assert r.status_code == 200
    for t in r.json()["items"]:
        assert t["tool_type"] == "api"


@pytest.mark.asyncio
async def test_tools_registry_invalid_sort(client, headers):
    """Невалидное значение sort → 422."""
    r = await client.get("/dashboard/tools-registry?sort=invalid", headers=headers)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_tools_registry_aggregations_consistent(client, headers):
    """totals.unique_tools должно совпадать с количеством items при отсутствии лимита."""
    r = await client.get("/dashboard/tools-registry?limit=1000", headers=headers)
    data = r.json()
    if data["totals"]["unique_tools"] <= 1000:
        assert data["totals"]["unique_tools"] == len(data["items"])


@pytest.mark.asyncio
async def test_tools_registry_includes_breadth(client, headers):
    """Каждый item имеет domains_breadth и список domains."""
    r = await client.get("/dashboard/tools-registry?limit=20", headers=headers)
    for t in r.json()["items"]:
        assert "domains_breadth" in t
        assert "domains" in t and isinstance(t["domains"], list)
        assert t["domains_breadth"] == len(t["domains"])
