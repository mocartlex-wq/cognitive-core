"""Тесты Postgres advisory lock в consolidator.

Проверяют что при параллельном вызове daily/weekly на одном домене:
- первый вызов идёт нормально
- второй параллельный возвращает {status: "lock_held"}
"""
import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_daily_concurrent_lock_held(client, headers):
    """Параллельный daily на тот же домен → второй вернёт lock_held."""
    domain = f"locktest_{asyncio.get_event_loop().time():.0f}"

    # Сначала залить событий чтобы daily было что обрабатывать
    for i in range(5):
        r = await client.post("/events", headers=headers, json={
            "source_agent": "agent_designer",
            "domain": domain,
            "payload": {"task": f"lock test {i}", "result": "ok", "feedback": "positive"},
        })
        assert r.status_code in (200, 201)

    # Запускаем 2 daily параллельно
    results = await asyncio.gather(
        client.post(f"/memory/consolidate/daily?domain={domain}", headers=headers, timeout=120),
        client.post(f"/memory/consolidate/daily?domain={domain}", headers=headers, timeout=120),
        return_exceptions=True,
    )

    statuses = []
    for r in results:
        if isinstance(r, Exception):
            statuses.append(f"exc:{r}")
            continue
        if r.status_code != 200:
            statuses.append(f"http:{r.status_code}")
            continue
        body = r.json()
        statuses.append(body.get("status"))

    # Один должен быть lock_held, второй ok/no_events
    held_count = sum(1 for s in statuses if s == "lock_held")
    assert held_count >= 1, f"Expected at least one lock_held, got {statuses}"


@pytest.mark.asyncio
async def test_weekly_lock_held_on_same_domain(client, headers):
    """Параллельный weekly на тот же домен → второй вернёт lock_held."""
    domain = "locktest_weekly_xyz"

    # Запускаем 2 weekly параллельно (даже если данных мало — лок всё равно сработает)
    results = await asyncio.gather(
        client.post(f"/memory/consolidate/weekly?domain={domain}", headers=headers, timeout=180),
        client.post(f"/memory/consolidate/weekly?domain={domain}", headers=headers, timeout=180),
        return_exceptions=True,
    )

    held_count = 0
    for r in results:
        if isinstance(r, httpx.Response) and r.status_code == 200:
            if r.json().get("status") == "lock_held":
                held_count += 1
    assert held_count >= 1, "At least one parallel weekly should be locked out"


@pytest.mark.asyncio
async def test_different_domains_no_lock_collision(client, headers):
    """Daily на РАЗНЫЕ домены параллельно → оба проходят без lock_held."""
    results = await asyncio.gather(
        client.post("/memory/consolidate/daily?domain=lock_a", headers=headers, timeout=60),
        client.post("/memory/consolidate/daily?domain=lock_b", headers=headers, timeout=60),
        return_exceptions=True,
    )

    held_count = sum(
        1 for r in results
        if isinstance(r, httpx.Response) and r.status_code == 200
        and r.json().get("status") == "lock_held"
    )
    assert held_count == 0, f"Different domains should not lock each other, got {[r.json() if isinstance(r, httpx.Response) else r for r in results]}"
