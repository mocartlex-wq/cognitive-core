"""Тесты pgvector интеграции.

Проверяют:
- pgvector расширение установлено
- L3 таблицы имеют embedding колонку vector(384)
- HNSW индексы созданы
- KNN-поиск через pgvector работает
"""
import pytest
import asyncpg
import os


DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://cognitive:cognitive_secret@postgres:5432/cognitive_core",
)


@pytest.mark.asyncio
async def test_pgvector_extension_installed():
    """Расширение vector доступно в БД."""
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'"
        )
        assert row is not None, "vector extension is not installed"
        assert row["extname"] == "vector"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_l3_knowledge_has_embedding_column():
    """l3_master_knowledge содержит колонку embedding vector(384)."""
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            """
            SELECT column_name, udt_name
            FROM information_schema.columns
            WHERE table_name = 'l3_master_knowledge' AND column_name = 'embedding'
            """
        )
        assert row is not None, "embedding column missing in l3_master_knowledge"
        assert row["udt_name"] == "vector"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_l3_tools_has_embedding_column():
    """l3_tools_registry содержит колонку embedding vector(384)."""
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            """
            SELECT column_name, udt_name
            FROM information_schema.columns
            WHERE table_name = 'l3_tools_registry' AND column_name = 'embedding'
            """
        )
        assert row is not None
        assert row["udt_name"] == "vector"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_hnsw_indexes_exist():
    """HNSW индексы созданы для обеих L3 таблиц."""
    conn = await asyncpg.connect(DB_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE indexname IN ('idx_l3_knowledge_hnsw', 'idx_l3_tools_hnsw')
            """
        )
        names = {r["indexname"] for r in rows}
        assert "idx_l3_knowledge_hnsw" in names, f"idx_l3_knowledge_hnsw missing, got {names}"
        assert "idx_l3_tools_hnsw" in names, f"idx_l3_tools_hnsw missing, got {names}"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_pgvector_knn_query_runs():
    """KNN-запрос через pgvector выполняется без ошибок."""
    conn = await asyncpg.connect(DB_URL)
    try:
        # Тестовый вектор
        test_vec = "[" + ",".join(["0.1"] * 384) + "]"
        rows = await conn.fetch(
            """
            SELECT id, (embedding <=> $1::vector) AS distance
            FROM l3_master_knowledge
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 3
            """,
            test_vec,
        )
        # Может вернуть 0+ результатов — главное что не падает
        for r in rows:
            assert r["distance"] is not None
            assert isinstance(float(r["distance"]), float)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_search_pgvector_function():
    """Функция search_pgvector возвращает корректную структуру."""
    from app.services.operative import search_pgvector
    test_vec = [0.1] * 384
    results = await search_pgvector(test_vec, "test_no_such_domain", top_k=3)
    assert isinstance(results, list)
    # Для несуществующего домена — пустой результат
    for r in results:
        assert "id" in r
        assert "record_type" in r
        assert "distance" in r
