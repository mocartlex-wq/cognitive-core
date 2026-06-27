"""Регрессия (2026-06-14 аудит): consolidator должен помечать события
обработанными в той же транзакции, что и INSERT L2-буфера.

До фикса INSERT и mark_events_processed выполнялись в разных соединениях:
сбой посередине → события и в L2, и снова на следующем daily-цикле, что
портило source_event_ids/confidence в ON CONFLICT-ветке.

Здесь юнит-тесты на тонкий контракт:
  1. mark_events_processed(ids, conn=...) использует переданное соединение
     (не дёргает pool).
  2. mark_events_processed(ids) (без conn) сохраняет обратную совместимость:
     берёт соединение из pool.
  3. _daily_consolidate_impl передаёт ОДНО И ТО ЖЕ соединение в INSERT и
     в mark_events_processed (атомарность шагов 3+4).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import consolidator, ingestor


@pytest.mark.asyncio
async def test_mark_events_processed_uses_passed_conn():
    """Если conn передан явно — execute() идёт на нём, не через pool."""
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    with patch.object(ingestor, "get_pool", new=AsyncMock()) as mock_pool:
        await ingestor.mark_events_processed([uuid4(), uuid4()], conn=mock_conn)
        mock_conn.execute.assert_awaited_once()
        mock_pool.assert_not_called()


@pytest.mark.asyncio
async def test_mark_events_processed_acquires_pool_when_no_conn():
    """Обратная совместимость: без conn берёт соединение из pool."""
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    class _AcqCtx:
        async def __aenter__(self_inner):
            return mock_conn

        async def __aexit__(self_inner, *args):
            return False

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AcqCtx())
    with patch.object(ingestor, "get_pool", new=AsyncMock(return_value=mock_pool)):
        await ingestor.mark_events_processed([uuid4()])
        mock_conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_events_processed_noop_on_empty():
    """Пустой список — ни pool, ни conn не должны трогаться."""
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    with patch.object(ingestor, "get_pool", new=AsyncMock()) as mock_pool:
        await ingestor.mark_events_processed([], conn=mock_conn)
        mock_conn.execute.assert_not_called()
        mock_pool.assert_not_called()


@pytest.mark.asyncio
async def test_daily_consolidate_uses_one_transaction_for_insert_and_mark():
    """Главное: один conn разделяется между INSERT INTO l2_daily_buffers
    и mark_events_processed. Этим транзакция оборачивает оба шага."""
    eid1, eid2 = uuid4(), uuid4()
    events = [
        {"id": eid1, "domain": "test_dom", "source_agent": "a", "raw_payload": {}, "timestamp": None},
        {"id": eid2, "domain": "test_dom", "source_agent": "a", "raw_payload": {}, "timestamp": None},
    ]

    # Один общий mock conn — через него идут и INSERT, и UPDATE.
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    class _TxCtx:
        async def __aenter__(self_inner):
            return None

        async def __aexit__(self_inner, *args):
            return False

    mock_conn.transaction = MagicMock(return_value=_TxCtx())

    class _AcqCtx:
        async def __aenter__(self_inner):
            return mock_conn

        async def __aexit__(self_inner, *args):
            return False

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AcqCtx())

    captured_conn_in_mark = {}

    async def _fake_mark(ids, conn=None):
        captured_conn_in_mark["conn"] = conn
        captured_conn_in_mark["ids"] = list(ids)

    with (
        patch.object(consolidator, "get_pool", new=AsyncMock(return_value=mock_pool)),
        patch.object(consolidator, "get_unprocessed_events", new=AsyncMock(return_value=events)),
        patch.object(consolidator, "pre_daily_filter",
                     new=AsyncMock(return_value={"skip": False, "filtered_event_ids": [str(eid1), str(eid2)]})),
        patch.object(consolidator, "analyze_daily_events",
                     new=AsyncMock(return_value={"summary": "x", "confidence": 0.7})),
        patch.object(consolidator, "mark_events_processed", new=_fake_mark),
    ):
        result = await consolidator._daily_consolidate_impl(since_hours=24, domain="test_dom")

    assert result["status"] == "ok"
    # INSERT прошёл по тому же conn, что и mark_events_processed
    mock_conn.execute.assert_awaited()  # at least the INSERT
    mock_conn.transaction.assert_called_once()  # обёрнуто в транзакцию
    assert captured_conn_in_mark.get("conn") is mock_conn, \
        "mark_events_processed должен получить тот же conn, что и INSERT — иначе разные транзакции"
    assert set(captured_conn_in_mark.get("ids", [])) == {eid1, eid2}
