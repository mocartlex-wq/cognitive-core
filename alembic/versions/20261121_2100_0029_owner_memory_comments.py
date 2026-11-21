"""Owner annotations on original memory records, separate from agent memory."""
from sqlalchemy import text

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE l1_raw_events ADD COLUMN IF NOT EXISTS has_owner_comment boolean NOT NULL DEFAULT false")
    op.execute("""
        CREATE TABLE IF NOT EXISTS owner_memory_comments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            record_id uuid NOT NULL REFERENCES l1_raw_events(id) ON DELETE CASCADE,
            owner_user_id uuid NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
            request_id uuid NOT NULL,
            text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 4000),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (owner_user_id, request_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_comments_record
        ON owner_memory_comments(record_id, owner_user_id, created_at DESC, id DESC)
    """)
    with op.get_context().autocommit_block():
        # Concurrent index creation can leave an invalid index on cancellation.
        valid = op.get_bind().execute(text("""
            SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('idx_l1_memory_feed')
        """)).scalar()
        if valid is False:
            op.execute("DROP INDEX CONCURRENTLY idx_l1_memory_feed")
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_l1_memory_feed
            ON l1_raw_events(owner_user_id, timestamp DESC, id DESC)
            WHERE owner_user_id IS NOT NULL AND raw_payload ? 'task'
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_l1_memory_feed")
    op.execute("DROP TABLE IF EXISTS owner_memory_comments")
    op.execute("ALTER TABLE l1_raw_events DROP COLUMN IF EXISTS has_owner_comment")
