"""agent_rules — operating rules для AI-агентов + self-improvement proposals.

Phase 6 — Agent Operating Rules System (2026-05-26).

Создаёт три таблицы:

1. **agent_rules** — сами правила. Хранит:
   - Платформенные (owner_user_id IS NULL, source='platform') — IP платформы, скрыты от tenants в UI
   - User rules (owner_user_id=<uid>, source='user') — добавляются tenant'ом через /ui/profile
   - Promoted (source='promoted_from_user') — proposals approved owner'ом, переведены в platform

2. **rule_proposals** — предложения новых правил от tenants. Self-improvement loop:
   tenant proposes → 3+ votes_up → DeepSeek analyzes → admin approves → promoted в agent_rules

3. **rule_proposal_votes** — голоса tenants за proposals (один tenant — один голос)

Seed: 4 платформенных core правил от owner (2026-05-26):
- rule-memory-before-answer — проверка обеих памятей перед ответом
- rule-memory-after-task    — обновление обеих памятей после задачи
- rule-plan-before-task     — обязательный план + согласование на нетривиальной задаче
- rule-replan-on-new-request — patch plan ИЛИ создать новый при mid-task запросе

См. C:\\Users\\mocar\\.claude\\plans\\iridescent-enchanting-rabbit.md — Phase 6.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic
revision = "0011_agent_rules"
down_revision = "0010_user_external_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────────
    # 1. agent_rules
    # ─────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_rules (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id UUID NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
          rule_id       TEXT NOT NULL,
          severity      TEXT NOT NULL CHECK (severity IN ('core','recommended','user')),
          scope         TEXT NOT NULL,
          lang          TEXT NOT NULL DEFAULT 'ru',
          position      INT NOT NULL DEFAULT 0,
          body          TEXT NOT NULL,
          active        BOOLEAN NOT NULL DEFAULT TRUE,
          source        TEXT NOT NULL DEFAULT 'user'
                          CHECK (source IN ('platform','user','promoted_from_user')),
          promoted_from UUID NULL REFERENCES agent_rules(id) ON DELETE SET NULL,
          override_of   UUID NULL REFERENCES agent_rules(id) ON DELETE CASCADE,
          created_at    TIMESTAMPTZ DEFAULT NOW(),
          updated_at    TIMESTAMPTZ DEFAULT NOW(),
          UNIQUE (owner_user_id, rule_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_rules_owner
          ON agent_rules(owner_user_id) WHERE active = TRUE
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_rules_platform
          ON agent_rules(severity, scope) WHERE owner_user_id IS NULL AND active = TRUE
    """)

    # ─────────────────────────────────────────────────────────────────────
    # 2. rule_proposals
    # ─────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS rule_proposals (
          id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          owner_user_id         UUID NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
          proposed_body         TEXT NOT NULL,
          proposed_scope        TEXT NOT NULL,
          rationale             TEXT,
          status                TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending','reviewing','approved','rejected','duplicate')),
          votes_up              INT DEFAULT 0,
          votes_down            INT DEFAULT 0,
          vote_threshold        INT DEFAULT 3,
          ds_analysis           TEXT,
          ds_suggested_severity TEXT,
          ds_duplicate_of       UUID NULL,
          reviewed_by           UUID NULL REFERENCES accounts(user_id),
          reviewed_at           TIMESTAMPTZ,
          review_notes          TEXT,
          promoted_rule_id      UUID NULL REFERENCES agent_rules(id) ON DELETE SET NULL,
          created_at            TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_rule_proposals_status
          ON rule_proposals(status, created_at DESC)
    """)

    # ─────────────────────────────────────────────────────────────────────
    # 3. rule_proposal_votes
    # ─────────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS rule_proposal_votes (
          proposal_id   UUID NOT NULL REFERENCES rule_proposals(id) ON DELETE CASCADE,
          owner_user_id UUID NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
          vote          INT NOT NULL CHECK (vote IN (-1, 1)),
          created_at    TIMESTAMPTZ DEFAULT NOW(),
          PRIMARY KEY (proposal_id, owner_user_id)
        )
    """)

    # ─────────────────────────────────────────────────────────────────────
    # 4. Seed 4 платформенных core правил (owner_user_id=NULL, source='platform')
    # ─────────────────────────────────────────────────────────────────────
    op.execute("""
        INSERT INTO agent_rules (rule_id, severity, scope, lang, position, body, source, active)
        VALUES
        (
          'rule-memory-before-answer',
          'core',
          'pre-answer',
          'ru',
          10,
          'После вопроса пользователя, перед ответом — проверка локальной и серверной памяти на свежесть; при расхождении одной из памятей дописать недостающие моменты в отстающую, для уменьшения количества ошибок и багов в работе.',
          'platform',
          TRUE
        ),
        (
          'rule-memory-after-task',
          'core',
          'post-task',
          'ru',
          20,
          'После ответа или окончания процесса выполнения задачи — обновление локальной и серверной памяти, для уменьшения количества ошибок и багов в работе.',
          'platform',
          TRUE
        ),
        (
          'rule-plan-before-task',
          'core',
          'per-task',
          'ru',
          30,
          'На задачу пользователя — обязательное составление плана работ, с уточнением спорных моментов у пользователя и корректировки плана по уточнённым вопросам, вывод итогового плана для согласования с пользователем перед выполнением. Применимо если задача нетривиальна (более 2 шагов ИЛИ есть write-actions / production-изменения ИЛИ есть неопределённость).',
          'platform',
          TRUE
        ),
        (
          'rule-replan-on-new-request',
          'core',
          'mid-task',
          'ru',
          40,
          'При выполнении задачи от пользователя — в случае поступления нового запроса/задачи от пользователя требуется уточнение для корректировки текущего плана с дополнительной задачей и внесением изменений в итоговый план ИЛИ создание нового последующего плана. После реализации первоначального итогового — следующий план становится первоначальным итоговым, и к нему применимы те же действия.',
          'platform',
          TRUE
        )
        ON CONFLICT (owner_user_id, rule_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rule_proposal_votes")
    op.execute("DROP TABLE IF EXISTS rule_proposals")
    op.execute("DROP TABLE IF EXISTS agent_rules")
