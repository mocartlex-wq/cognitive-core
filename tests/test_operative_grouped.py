"""Тесты grouped=true response для /operative/query.

Проверяют структурированный пакет знаний для агента:
{frame: {patterns, mistakes, rules, tools, all}, counts: {...}}
"""
import pytest


@pytest.mark.asyncio
async def test_query_default_returns_flat(client, headers):
    """Без grouped — backward-compat плоский список."""
    r = await client.post(
        "/operative/query",
        json={"domain": "memory_arch", "context": "test", "top_k": 3},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data
    assert "results" in data  # плоский список (старый формат)


@pytest.mark.asyncio
async def test_query_grouped_returns_frame(client, headers):
    """С grouped=true — frame по разделам + counts."""
    r = await client.post(
        "/operative/query?grouped=true",
        json={"domain": "memory_arch", "context": "test", "top_k": 3},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data
    assert "frame" in data
    assert "counts" in data
    # Все 5 разделов присутствуют
    for section in ("patterns", "mistakes", "rules", "tools", "all"):
        assert section in data["frame"], f"missing section: {section}"
        assert isinstance(data["frame"][section], list)
    # Counts валидны
    for k in ("patterns", "mistakes", "rules", "tools", "total"):
        assert k in data["counts"]
        assert isinstance(data["counts"][k], int)


@pytest.mark.asyncio
async def test_grouped_counts_match_frame_lengths(client, headers):
    """counts.* должны совпадать с len(frame.*) (кроме all)."""
    r = await client.post(
        "/operative/query?grouped=true",
        json={"domain": "memory_arch", "context": "test", "top_k": 5},
        headers=headers,
    )
    data = r.json()
    f = data["frame"]
    c = data["counts"]
    assert c["patterns"] == len(f["patterns"])
    assert c["mistakes"] == len(f["mistakes"])
    assert c["rules"] == len(f["rules"])
    assert c["tools"] == len(f["tools"])
    assert c["total"] == len(f["all"])


@pytest.mark.asyncio
async def test_grouped_unit_function():
    """_group_results_for_agent корректно сортирует разные типы."""
    from app.api.operative import _group_results_for_agent

    results = [
        {"record_type": "knowledge", "knowledge_type": "pattern", "id": "p1"},
        {"record_type": "knowledge", "knowledge_type": "mistake", "id": "m1"},
        {"record_type": "knowledge", "knowledge_type": "rule", "id": "r1"},
        {"record_type": "tool", "id": "t1"},
        {"record_type": "knowledge", "knowledge_type": "pattern", "id": "p2"},
        # без knowledge_type — должен пойти в patterns
        {"record_type": "knowledge", "id": "k_unknown"},
    ]
    g = _group_results_for_agent(results)
    assert len(g["patterns"]) == 3  # p1, p2, k_unknown
    assert len(g["mistakes"]) == 1  # m1
    assert len(g["rules"]) == 1     # r1
    assert len(g["tools"]) == 1     # t1
    assert len(g["all"]) == 6       # все
