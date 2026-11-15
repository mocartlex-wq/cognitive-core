"""Indexed Russian/simple L1 lookup before task-head selection.

Expression indexes avoid rewriting payloads or adding generated columns.
Concurrent builds keep normal inserts available during deployment.
"""
from sqlalchemy import text

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, language in (("idx_l1_memory_ru", "russian"), ("idx_l1_memory_plain", "simple")):
            valid = op.get_bind().execute(text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname=:name AND n.nspname=current_schema()"), {"name": name}).scalar()
            # A cancelled concurrent build can leave an invalid index behind.
            # Retry that build rather than silently accepting IF NOT EXISTS.
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON l1_raw_events USING gin (to_tsvector('{language}', raw_payload::text))")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_l1_memory_plain")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_l1_memory_ru")
