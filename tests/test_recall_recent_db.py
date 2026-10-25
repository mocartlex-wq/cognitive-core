"""Секция recent на живом Postgres (условие приёмки ревью Codex 25.10.2026).

Проверяется то, что мок-пул не покрывает: изоляция двух владельцев в одном
домене, ранжирование полного FTS-совпадения выше частичного по первому слову,
и что план запроса строится (EXPLAIN). Без БД (локально) — пропуск; в CI
db-tests DATABASE_URL задан.
"""
from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

asyncpg = pytest.importorskip("asyncpg")
from app.services import operative as svc  # noqa: E402

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("COGCORE_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="нет DATABASE_URL — тест только на живой БД")


@pytest.fixture
async def pool():
    try:
        p = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2, timeout=5)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres недоступен: {e}")
    yield p
    await p.close()


@pytest.mark.asyncio
async def test_recent_isolates_owners_and_ranks_exact_first(pool):
    marker = f"tandem-{uuid.uuid4().hex[:8]}"
    owner_a, owner_b = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [
        # (owner, домен, payload) — у A три события: точное, частичное, чужой домен
        (owner_a, "cognitive_core", {"task": f"{marker} канал Codex Claude orchestra", "result": "полное совпадение"}),
        (owner_a, "cognitive_core", {"task": f"{marker} канал только одно слово", "result": "частичное"}),
        (owner_a, "deploy", {"task": f"{marker} канал Codex Claude orchestra", "result": "другой домен"}),
        (owner_b, "cognitive_core", {"task": f"{marker} канал Codex Claude orchestra", "result": "чужой владелец"}),
    ]
    ids = []
    async with pool.acquire() as conn:
        for owner, domain, payload in rows:
            rid = await conn.fetchval(
                "INSERT INTO l1_raw_events (source_agent, owner_user_id, domain, raw_payload) "
                "VALUES ($1, $2::uuid, $3, $4::jsonb) RETURNING id",
                "test-recent", owner, domain, json.dumps(payload, ensure_ascii=False),
            )
            ids.append(rid)
    try:
        with patch.object(svc, "get_pool", AsyncMock(return_value=pool)):
            got = await svc.recent_events(f"{marker} канал Codex Claude orchestra", owner_user_id=owner_a, domain="cognitive_core")
            # Частичное совпадение: в запросе нет второго слова из частичной записи,
            # но первое слово (marker) общее → fallback ILIKE поднимает её ниже точной.
            results = [r["content"]["result"] for r in got]
            assert results[0] == "полное совпадение", results
            assert "чужой владелец" not in results and "другой домен" not in results
            assert "частичное" in results, "частичное совпадение по первому слову должно попасть, но ниже точного"

            none = await svc.recent_events(marker, owner_user_id=None)
            assert none == [], "без владельца секция пуста"

            # План запроса строится (индексы по timestamp/domain есть, FTS без индекса —
            # приемлемо при окне 7 дней; здесь фиксируем, что запрос вообще планируется).
            async with pool.acquire() as conn:
                plan = await conn.fetch(
                    "EXPLAIN SELECT id FROM l1_raw_events WHERE owner_user_id = $1::uuid "
                    "AND timestamp >= NOW() - INTERVAL '7 day' "
                    "AND to_tsvector('simple', raw_payload::text) @@ plainto_tsquery('simple', $2::text) "
                    "ORDER BY timestamp DESC LIMIT 5",
                    owner_a, marker,
                )
                assert plan, "EXPLAIN вернул пустой план"
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM l1_raw_events WHERE id = ANY($1::uuid[])", ids)
