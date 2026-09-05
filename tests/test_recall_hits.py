"""Построчный лог показов знания.

Вопрос, ради которого он заведён: «какие записи памяти хоть раз пригодились».
Ответить на него было нечем — счётчиков не было, лога обращений не было,
а `feedback_record` писал в Redis с TTL 24 часа и не читался никем, то есть
создавал видимость обратной связи.

Счётчик на записи (`usage_count`) был бы дешевле, но теряет временнýю ось:
вопрос звучит «предотвратила ли запись повтор ПОСЛЕ того, как начала
показываться», и без даты показа он неотвечаем.

Отдельно проверяем терпимость к ненакаченной миграции: на прод они катятся
руками, значит код обязан работать на базе, где 0022 ещё нет — молча, без
пятисоток и без потери поиска.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.services.operative import feedback_record, record_recall_hits

OWNER = "11111111-1111-1111-1111-111111111111"
SESSION = "22222222-2222-2222-2222-222222222222"


def _pool(*, executemany_side_effect=None, execute_result="UPDATE 1"):
    conn = MagicMock()
    conn.executemany = AsyncMock(side_effect=executemany_side_effect)
    conn.execute = AsyncMock(return_value=execute_result)

    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool, conn


def _results(n=3):
    return [{"id": str(uuid4()), "record_type": "knowledge",
             "domain": "infra_lessons", "distance": 0.1 * i} for i in range(n)]


@pytest.mark.asyncio
async def test_writes_one_row_per_shown_record():
    pool, conn = _pool()
    res = _results(3)
    with patch("app.services.operative.get_pool", AsyncMock(return_value=pool)):
        written = await record_recall_hits(
            res, session_id=SESSION, domain="infra_lessons", owner_user_id=OWNER)

    assert written == 3
    _sql, rows = conn.executemany.call_args[0]
    assert len(rows) == 3
    ranks = [r[4] for r in rows]
    assert ranks == [0, 1, 2], "ранг обязан сохраняться: без него не отличить первую выдачу от последней"
    assert all(r[6] == UUID(OWNER) for r in rows)
    assert all(r[2] == UUID(SESSION) for r in rows), "без session_id обратную связь не с чем связать"


@pytest.mark.asyncio
async def test_missing_table_is_survivable():
    """Миграция 0022 не накачена — пишем ноль строк и НЕ падаем."""
    err = Exception('relation "l3_recall_hits" does not exist')
    pool, _ = _pool(executemany_side_effect=err)
    with patch("app.services.operative.get_pool", AsyncMock(return_value=pool)):
        written = await record_recall_hits(
            _results(2), session_id=SESSION, domain="d", owner_user_id=OWNER)
    assert written == 0


@pytest.mark.asyncio
async def test_any_db_failure_is_survivable():
    pool, _ = _pool(executemany_side_effect=RuntimeError("pool exhausted"))
    with patch("app.services.operative.get_pool", AsyncMock(return_value=pool)):
        written = await record_recall_hits(
            _results(2), session_id=SESSION, domain="d", owner_user_id=OWNER)
    assert written == 0


@pytest.mark.asyncio
async def test_non_uuid_ids_are_skipped_not_fatal():
    """В демо-данных и синтетике попадаются id вида 'id-1'."""
    pool, conn = _pool()
    res = [{"id": "id-1", "record_type": "knowledge"},
           {"id": str(uuid4()), "record_type": "tool", "distance": 0.3}]
    with patch("app.services.operative.get_pool", AsyncMock(return_value=pool)):
        written = await record_recall_hits(
            res, session_id=None, domain="d", owner_user_id=None)
    assert written == 1
    _sql, rows = conn.executemany.call_args[0]
    assert rows[0][1] == "tool"


@pytest.mark.asyncio
async def test_empty_results_write_nothing():
    with patch("app.services.operative.get_pool", AsyncMock()) as gp:
        written = await record_recall_hits([], session_id=SESSION, domain="d",
                                           owner_user_id=OWNER)
    assert written == 0
    gp.assert_not_called()


@pytest.mark.asyncio
async def test_feedback_is_persisted_not_only_in_redis():
    """Оценка обязана пережить сутки: раньше умирала вместе с TTL Redis."""
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    pool, conn = _pool(execute_result="UPDATE 1")

    with patch("app.services.operative.get_redis", AsyncMock(return_value=redis)), \
         patch("app.services.operative.get_pool", AsyncMock(return_value=pool)):
        out = await feedback_record(UUID(SESSION), uuid4(), "knowledge", True)

    assert out["persisted"] is True
    sql, *params = conn.execute.call_args[0]
    assert "l3_recall_hits" in sql and "useful" in sql
    assert True in params


@pytest.mark.asyncio
async def test_feedback_survives_missing_table():
    redis = MagicMock()
    redis.exists = AsyncMock(return_value=1)
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    pool, conn = _pool()
    conn.execute = AsyncMock(side_effect=Exception('relation "l3_recall_hits" does not exist'))

    with patch("app.services.operative.get_redis", AsyncMock(return_value=redis)), \
         patch("app.services.operative.get_pool", AsyncMock(return_value=pool)):
        out = await feedback_record(UUID(SESSION), uuid4(), "knowledge", False)

    assert out["status"] == "recorded", "обратная связь не должна отказывать из-за ненакаченной миграции"
    assert out["persisted"] is False
