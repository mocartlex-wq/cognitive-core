"""Схема в коде должна совпадать со схемой в миграциях.

У проекта ДВА источника DDL:
  • `app/db/postgres.py` → CREATE_TABLES_SQL, исполняется `init_db()` при старте;
  • `alembic/versions/*` — миграции.

Они разошлись незаметно: `owner_user_id` жил ТОЛЬКО в миграции 0006, поэтому
база, поднятая через `init_db` без alembic (CI, self-hosted, локальная
разработка), не имела колонки вовсе — и любой owner-фильтр падал с
UndefinedColumnError. На проде это не проявлялось: там таблицы уже созданы, и
`CREATE TABLE IF NOT EXISTS` их не трогает. Из-за этого расхождение прожило
долго и всплыло только при разборе 2026-08-11.

Тест не сверяет схемы побайтово — он проверяет ключевые колонки и индексы,
которые обязаны быть в обоих источниках. Добавляя колонку миграцией, добавь её
и в CREATE_TABLES_SQL, иначе тест упадёт.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DDL_FILE = ROOT / "app" / "db" / "postgres.py"
MIGRATIONS_DIR = ROOT / "alembic" / "versions"

# Таблицы, которые обязаны нести владельца: на них стоят owner-фильтры.
OWNER_SCOPED_TABLES = [
    "l1_raw_events",
    "l2_daily_buffers",
    "l3_master_knowledge",
    "l3_tools_registry",
    "l4_snapshots",
]

# Индексы, без которых код работает неверно, а не просто медленно.
REQUIRED_INDEXES = {
    "idx_l2_date_domain_owner": "уникальность свёртки в границах владельца — иначе тенанты склеиваются",
    "idx_l3_tools_unique_active": "ON CONFLICT в консолидаторе не с чем сопоставляться — реестр заполнится копиями",
    "idx_l3_fts": "полнотекстовая половина гибридного поиска",
}


@pytest.fixture(scope="module")
def ddl() -> str:
    return DDL_FILE.read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("table", OWNER_SCOPED_TABLES)
def test_owner_column_present_in_create_tables(ddl: str, table: str):
    """У owner-скоупной таблицы обязана быть колонка владельца в CREATE_TABLES_SQL."""
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);", ddl, re.S)
    assert m, f"{table}: определение таблицы не найдено в {DDL_FILE.name}"
    assert "owner_user_id" in m.group(1), (
        f"{table}: нет owner_user_id в CREATE_TABLES_SQL. База, поднятая через "
        f"init_db без alembic, упадёт на owner-фильтре с UndefinedColumnError."
    )


@pytest.mark.parametrize("index,why", list(REQUIRED_INDEXES.items()))
def test_required_index_present_in_create_tables(ddl: str, index: str, why: str):
    """Индекс, от которого зависит КОРРЕКТНОСТЬ, должен быть в обоих источниках."""
    assert index in ddl, f"{index} отсутствует в {DDL_FILE.name}: {why}"


@pytest.mark.parametrize("index,why", list(REQUIRED_INDEXES.items()))
def test_required_index_present_in_migrations(index: str, why: str):
    """…и в миграциях — для баз, которые обновляются, а не создаются заново."""
    if not MIGRATIONS_DIR.exists():
        pytest.skip("каталог миграций отсутствует в этой сборке")
    found = any(
        index in p.read_text(encoding="utf-8-sig")
        for p in MIGRATIONS_DIR.glob("*.py")
    )
    assert found, f"{index} не создаётся ни одной миграцией: {why}"
