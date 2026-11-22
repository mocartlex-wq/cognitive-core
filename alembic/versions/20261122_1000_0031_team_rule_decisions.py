"""Owner-scoped decisions about the order of demonstration materials."""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE team_rule_cases (
            id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
            spec jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX team_case_owner ON team_rule_cases(owner_id);
        CREATE TABLE team_rule_runs (
            id uuid PRIMARY KEY, case_id uuid NOT NULL REFERENCES team_rule_cases ON DELETE CASCADE,
            instance_id uuid NOT NULL, agent text NOT NULL, assessment jsonb NOT NULL,
            choice text, duration text, source_id uuid REFERENCES team_rule_runs,
            decided_at timestamptz, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(case_id,instance_id)
        );
        CREATE UNIQUE INDEX team_case_future ON team_rule_runs(case_id)
            WHERE duration='future' AND revoked_at IS NULL;
        CREATE TABLE team_rule_refinements (
            case_id uuid NOT NULL REFERENCES team_rule_cases ON DELETE CASCADE, address text NOT NULL,
            rule jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(case_id,address)
        );
        CREATE TABLE team_rule_feedback (
            case_id uuid NOT NULL REFERENCES team_rule_cases ON DELETE CASCADE, id uuid NOT NULL,
            payload jsonb NOT NULL, receipt jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(case_id,id)
        );
        CREATE UNIQUE INDEX team_rule_one_observation
           ON team_rule_feedback(case_id,(payload->>'rule_id'),(payload->>'evidence_run_id'))
    """)


def downgrade():
    op.execute("DROP TABLE team_rule_feedback, team_rule_refinements, team_rule_runs, team_rule_cases")
