"""Add durable task coordination to the existing orchestrator ledger.

Revision ID: 0027
Revises: 0026
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    # The legacy orchestrator created this table outside Alembic. Bootstrap it
    # on fresh installations without replacing any existing tasks or statuses.
    op.execute("""
        CREATE TABLE IF NOT EXISTS orchestrator_tasks (
            task_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id text NOT NULL, request_text text NOT NULL,
            context jsonb DEFAULT '{}'::jsonb, status text NOT NULL DEFAULT 'queued',
            assigned_agents jsonb DEFAULT '[]'::jsonb, responses jsonb DEFAULT '{}'::jsonb,
            final_answer text, proxy_used boolean DEFAULT false,
            trace_events jsonb DEFAULT '[]'::jsonb,
            created_at timestamptz DEFAULT now(), completed_at timestamptz,
            parent_task_id uuid, cascading_depth integer DEFAULT 0,
            session_id text, owner_user_id uuid REFERENCES accounts(user_id) ON DELETE SET NULL
        );
        ALTER TABLE orchestrator_tasks ADD COLUMN IF NOT EXISTS coord_data jsonb;
        ALTER TABLE orchestrator_tasks ADD COLUMN IF NOT EXISTS coord_version bigint NOT NULL DEFAULT 0;
        ALTER TABLE orchestrator_tasks ADD COLUMN IF NOT EXISTS coord_updated_at timestamptz NOT NULL DEFAULT now();
        ALTER TABLE orchestrator_tasks ADD COLUMN IF NOT EXISTS coord_lease_until timestamptz;
        CREATE INDEX IF NOT EXISTS idx_coord_tasks_owner
            ON orchestrator_tasks(owner_user_id, coord_updated_at DESC) WHERE coord_data IS NOT NULL;
        CREATE TABLE coord_task_operations (
            task_id uuid NOT NULL REFERENCES orchestrator_tasks(task_id) ON DELETE CASCADE,
            operation_key text NOT NULL,
            request_hash text NOT NULL,
            request_data jsonb NOT NULL,
            actor jsonb NOT NULL,
            action text NOT NULL,
            result jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(task_id, operation_key)
        );
    """)


def downgrade():
    # Prevent an accidental destructive rollback after business tasks exist.
    # Roll back application code first; retain the additive schema in production.
    op.execute("""
        LOCK TABLE orchestrator_tasks IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM orchestrator_tasks WHERE coord_data IS NOT NULL) THEN
                RAISE EXCEPTION 'Coordination tasks exist: retain schema and roll back code only';
            END IF;
        END $$;
        DROP TABLE coord_task_operations;
        DROP INDEX IF EXISTS idx_coord_tasks_owner;
        ALTER TABLE orchestrator_tasks DROP COLUMN coord_lease_until;
        ALTER TABLE orchestrator_tasks DROP COLUMN coord_updated_at;
        ALTER TABLE orchestrator_tasks DROP COLUMN coord_version;
        ALTER TABLE orchestrator_tasks DROP COLUMN coord_data;
    """)
