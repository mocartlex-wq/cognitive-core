"""Тесты per-agent state checkpoint (recovery после срыва сессии)."""
import pytest


@pytest.mark.asyncio
async def test_state_not_exists_returns_no_message(client, headers):
    """Restore несуществующего агента → exists=false с подсказкой."""
    r = await client.get("/agents/test_no_such_agent_xyz/state", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["exists"] is False
    assert "cognitive_save_state" in data["message"]


@pytest.mark.asyncio
async def test_save_and_restore_checkpoint(client, headers):
    """Save → restore возвращает то же state."""
    agent_id = "test_agent_save_restore"
    payload = {
        "current_task": "разработка лендинга для X",
        "state_data": {"plan": ["wireframe", "design", "code"], "step": 1},
        "notes": "тестовый чекпоинт",
        "trigger": "manual",
    }
    save_r = await client.post(f"/agents/{agent_id}/checkpoint", headers=headers, json=payload)
    assert save_r.status_code == 200
    saved = save_r.json()
    assert saved["agent_id"] == agent_id
    assert saved["trigger"] == "manual"
    assert saved["state_size_bytes"] > 0

    restore_r = await client.get(f"/agents/{agent_id}/state", headers=headers)
    assert restore_r.status_code == 200
    state = restore_r.json()
    assert state["exists"] is True
    assert state["current_task"] == "разработка лендинга для X"
    assert state["state_data"] == {"plan": ["wireframe", "design", "code"], "step": 1}
    assert state["notes"] == "тестовый чекпоинт"


@pytest.mark.asyncio
async def test_history_returns_multiple_checkpoints(client, headers):
    """Несколько save → history содержит все snapshots."""
    agent_id = "test_agent_history"
    for i in range(3):
        await client.post(
            f"/agents/{agent_id}/checkpoint",
            headers=headers,
            json={"current_task": f"step {i}", "state_data": {"i": i}, "trigger": "auto"},
        )
    r = await client.get(f"/agents/{agent_id}/history", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["agent_id"] == agent_id
    assert len(data["items"]) >= 3
    # Сортировка по убыванию даты
    times = [item["checkpoint_at"] for item in data["items"]]
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_state_size_limit_413(client, headers):
    """state_data > 256KB → 413 Payload Too Large."""
    agent_id = "test_agent_size_limit"
    big_value = "x" * (300 * 1024)  # 300KB
    r = await client.post(
        f"/agents/{agent_id}/checkpoint",
        headers=headers,
        json={"current_task": "too big", "state_data": {"big": big_value}, "trigger": "manual"},
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_invalid_trigger_rejected(client, headers):
    """trigger вне ENUM → 422."""
    r = await client.post(
        "/agents/test_invalid/checkpoint",
        headers=headers,
        json={"current_task": "x", "trigger": "bogus_trigger"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_upsert_increments_total_checkpoints(client, headers):
    """Повторный save для того же agent_id → total_checkpoints растёт."""
    agent_id = "test_agent_upsert"
    for _ in range(3):
        await client.post(
            f"/agents/{agent_id}/checkpoint", headers=headers,
            json={"current_task": "loop", "state_data": {"x": 1}, "trigger": "manual"},
        )
    r = await client.get(f"/agents/{agent_id}/state", headers=headers)
    assert r.json()["total_checkpoints"] >= 3


@pytest.mark.asyncio
async def test_list_all_agents(client, headers):
    """GET /agents возвращает всех агентов с активным state."""
    # Сначала убедимся что хотя бы один агент есть
    await client.post(
        "/agents/test_listed/checkpoint", headers=headers,
        json={"current_task": "ping", "state_data": {}, "trigger": "manual"},
    )
    r = await client.get("/agents", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "count" in data and "items" in data
    assert any(a["agent_id"] == "test_listed" for a in data["items"])


@pytest.mark.asyncio
async def test_restore_includes_recent_events(client, headers):
    """Restore включает последние L1 события агента (если есть)."""
    agent_id = "test_with_events_recent"
    # Создаём событие от этого агента
    await client.post(
        "/events", headers={**headers},
        json={
            "source_agent": "agent_designer",  # должен быть из AGENT_API_KEYS
            "domain": "test_recent",
            "payload": {"task": "test", "result": "ok", "feedback": "positive"},
        },
    )
    # Save state
    await client.post(
        f"/agents/{agent_id}/checkpoint", headers=headers,
        json={"current_task": "smth", "state_data": {}, "trigger": "manual"},
    )
    r = await client.get(f"/agents/{agent_id}/state?recent_events=20", headers=headers)
    assert r.status_code == 200
    # recent_events может быть пустым если у этого agent_id нет L1 событий — это OK
    assert "recent_events" in r.json()
    assert isinstance(r.json()["recent_events"], list)
