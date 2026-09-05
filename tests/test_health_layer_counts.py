"""Счётчики слоёв в /health считают ЖИВЫЕ записи, а не все строки.

Найдено 17.08 взглядом на главную страницу сайта: там стояло
«2 959 инструментов». В реестре действительно 2959 строк — но живых из них
157, остальные депрецированы чисткой дублей 16.08. То же у знаний: 373
против 284.

Цифра идёт в три места сразу: на витрину (`sandbox/home.html:1000`), в ответ
`/health` и в gauge `cognitive_layer_records`. То есть мониторинг показывал
рост там, где шла уборка.

L1/L2/L4 мягкого удаления не имеют — колонки `effective_to` в этих таблицах
нет, и фильтр там уронил бы запрос. Поэтому проверяем обе стороны.

Меряем ФАКТИЧЕСКИЙ SQL, а не текст исходника: подменяем пул и смотрим, с
чем позвали fetchval. Проверка по тексту совпала бы и с закомментированным
кодом.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _Conn:
    """Пишет все SELECT COUNT, которые через него прошли."""

    def __init__(self):
        self.count_queries: list[str] = []

    async def execute(self, *a, **k):
        return "SELECT 1"

    async def fetchval(self, sql, *a, **k):
        if "COUNT(*)" in sql:
            self.count_queries.append(" ".join(sql.split()))
        return 0

    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None


def _pool_with(conn):
    class _Acquire:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acquire())
    return pool


async def _collect_count_queries() -> list[str]:
    from app.main import health

    conn = _Conn()
    with patch("app.db.postgres.get_pool", AsyncMock(return_value=_pool_with(conn))), \
         patch("app.db.redis.get_redis", AsyncMock(side_effect=RuntimeError("redis off"))), \
         patch("app.db.s3.get_s3", MagicMock(side_effect=RuntimeError("s3 off"))):
        try:
            await health()
        except Exception:
            # Нас интересуют только запросы к Postgres; падение на прочих
            # проверках здоровья допустимо — они замоканы недоступными.
            pass
    return conn.count_queries


def _query_for(queries: list[str], table: str) -> str:
    hits = [q for q in queries if f"FROM {table}" in q]
    assert hits, f"счётчик по {table} не выполнялся вовсе: {queries}"
    return hits[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["l3_master_knowledge", "l3_tools_registry"])
async def test_soft_deleted_layers_are_filtered(table):
    """Слои с мягким удалением обязаны отбрасывать депрецированное."""
    q = _query_for(await _collect_count_queries(), table)
    assert "effective_to IS NULL" in q, (
        f"{table} считается без фильтра — витрина и метрика покажут "
        f"вычищенное как действующее (2959 вместо 157 на 17.08). SQL: {q}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["l1_raw_events", "l2_daily_buffers", "l4_snapshots"])
async def test_plain_layers_are_not_filtered(table):
    """У этих таблиц колонки effective_to нет — фильтр уронит /health."""
    q = _query_for(await _collect_count_queries(), table)
    assert "effective_to" not in q, (
        f"{table} не имеет колонки effective_to, а фильтр добавлен. SQL: {q}"
    )


@pytest.mark.asyncio
async def test_all_five_layers_are_counted():
    """Слой, выпавший из счётчиков, — молчаливая потеря наблюдаемости."""
    queries = await _collect_count_queries()
    for table in ("l1_raw_events", "l2_daily_buffers", "l3_master_knowledge",
                  "l3_tools_registry", "l4_snapshots"):
        _query_for(queries, table)
