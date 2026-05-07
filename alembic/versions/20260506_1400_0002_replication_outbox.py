"""replication_outbox table for NATS-based server→local mirror

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-06 14:00:00 UTC

DS-architecture: Outbox-pattern. Каждое write-действие в L1/L3/L4/agent_states
ДОЛЖНО атомарно (в той же tx) писать в replication_outbox. Outbox publisher
читает unprocessed строки и публикует в NATS subjects:
  cognitive.repl.l1
  cognitive.repl.l3
  cognitive.repl.l4
  cognitive.repl.agent_state

Idempotent на consumer-стороне через UNIQUE event_id + ON CONFLICT DO NOTHING.
"""
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS replication_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE DEFAULT uuid_generate_v4(),
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ NULL,
    publish_attempts INT NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    CONSTRAINT replication_outbox_kind_chk CHECK (
        kind IN ('l1_event','l3_knowledge','l3_tool','l4_snapshot','agent_state','l5_audit')
    )
);

CREATE INDEX IF NOT EXISTS idx_replication_outbox_unpublished
    ON replication_outbox (created_at)
    WHERE published_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_replication_outbox_event_id
    ON replication_outbox (event_id);

-- Cursor для consumer'а на local-стороне. Опционально — local трекает свой
-- last_processed через NATS durable consumer, но также может писать сюда
-- для cross-check.
CREATE TABLE IF NOT EXISTS replication_consumer_cursor (
    consumer_name TEXT PRIMARY KEY,
    last_event_id UUID NULL,
    last_published_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE replication_outbox IS
    'Outbox-pattern для NATS-based server→local replication. См. AGENT_OPERATIONS.md.';
COMMENT ON COLUMN replication_outbox.kind IS
    'Тип события: l1_event, l3_knowledge, l3_tool, l4_snapshot, agent_state, l5_audit';
COMMENT ON COLUMN replication_outbox.event_id IS
    'UUID для idempotency на consumer-стороне (UNIQUE).';
COMMENT ON COLUMN replication_outbox.payload IS
    'JSONB с полным состоянием объекта для воспроизведения у consumer.';
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS replication_consumer_cursor;
DROP TABLE IF EXISTS replication_outbox;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
