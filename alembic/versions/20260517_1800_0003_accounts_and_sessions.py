"""accounts + sessions + email_verification + owner_user_id columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-17 18:00:00 UTC

Введение полноценной системы пользовательских аккаунтов:
  - accounts: профили пользователей (email + display_name + is_admin)
  - sessions: HTTP-only cookie-сессии с rolling TTL
  - email_verification_tokens: одноразовые magic-link токены
  - rooms.owner_user_id (FK accounts) — кто владелец комнаты
  - agent_states.owner_user_id (FK accounts) — кому принадлежит помощник
  - orchestrator_tasks.session_id — какая сессия запустила задачу

Backward-compat:
  - Все new колонки NULLABLE — существующие комнаты/помощники остаются
    «анонимными» (без владельца) пока владелец первый раз не войдёт
  - rooms.created_by (TEXT) НЕ удаляем — оставляем для legacy
  - agent_keys таблица (per-agent API keys) — продолжает работать как раньше

Auto-migration cogowner-2026:
  - При первом входе под OWNER_BOOTSTRAP_EMAIL в auth-handler выполняется
    UPDATE rooms SET owner_user_id = NEW.user_id WHERE created_by = 'cogowner-2026'
  - См. app/api/auth.py
"""
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
-- ─── ACCOUNTS: профили пользователей ────────────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    user_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    avatar_url TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ NULL  -- soft delete с 30-дневной отсрочкой
);
CREATE INDEX IF NOT EXISTS idx_accounts_email_active ON accounts(email)
    WHERE deleted_at IS NULL;

-- ─── EMAIL_VERIFICATION_TOKENS: magic-link tokens (одноразовые) ─────────
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token_hash TEXT PRIMARY KEY,  -- SHA-256(token) — сам токен НЕ хранится
    email TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    used_at TIMESTAMPTZ,           -- single-use: после использования помечается
    ip_address TEXT,                -- для аудита (откуда запросили ссылку)
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_verif_email_active ON email_verification_tokens(email, expires_at DESC)
    WHERE used_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_email_verif_expires ON email_verification_tokens(expires_at)
    WHERE used_at IS NULL;

-- ─── SESSIONS: cookie-based, opaque token + DB lookup ───────────────────
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,    -- 32 random bytes hex (64 chars)
    user_id UUID NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    device_info JSONB NOT NULL DEFAULT '{}'::jsonb,  -- user-agent, ip, geo
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at TIMESTAMPTZ
);
-- NB: partial-index predicates must be IMMUTABLE — NOW() is only STABLE, so
-- "WHERE expires_at > NOW()" is rejected by Postgres ("functions in index
-- predicate must be marked IMMUTABLE"). Keep the immutable part of the predicate
-- (revoked flag) and index expires_at plainly; the planner still uses these for
-- the active-session lookup and the expiry-cleanup scan.
CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, last_used_at DESC)
    WHERE NOT revoked;
CREATE INDEX IF NOT EXISTS idx_sessions_cleanup ON sessions(expires_at);

-- ─── OWNER_USER_ID: привязка существующих объектов к user ──────────────
ALTER TABLE agent_states ADD COLUMN IF NOT EXISTS owner_user_id UUID
    REFERENCES accounts(user_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_agent_states_owner ON agent_states(owner_user_id)
    WHERE owner_user_id IS NOT NULL;

-- ─── ORCHESTRATOR_TASKS: привязка к сессии ─────────────────────────────
-- Таблица создаётся cognitive-orchestrator.py (порт 9099), может быть в другом deploy.
-- Не критично если её нет — IF EXISTS защищает.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'orchestrator_tasks') THEN
        ALTER TABLE orchestrator_tasks ADD COLUMN IF NOT EXISTS session_id TEXT;
        ALTER TABLE orchestrator_tasks ADD COLUMN IF NOT EXISTS owner_user_id UUID
            REFERENCES accounts(user_id) ON DELETE SET NULL;
        CREATE INDEX IF NOT EXISTS idx_orch_tasks_user ON orchestrator_tasks(owner_user_id, created_at DESC)
            WHERE owner_user_id IS NOT NULL;
    END IF;
END $$;

-- ─── ROOMS: привязка к user (rooms таблица создаётся cognitive-rooms.py) ─
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'rooms') THEN
        ALTER TABLE rooms ADD COLUMN IF NOT EXISTS owner_user_id UUID
            REFERENCES accounts(user_id) ON DELETE SET NULL;
        ALTER TABLE rooms ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE;
        CREATE INDEX IF NOT EXISTS idx_rooms_owner ON rooms(owner_user_id)
            WHERE owner_user_id IS NOT NULL;
    END IF;
END $$;

-- ─── Cleanup периодически — DELETE used_at IS NOT NULL OR expires_at < NOW() ─
-- Запускается worker.py каждые 6 часов; здесь только создаём индексы для скорости.

COMMENT ON TABLE accounts IS
    'Пользовательские аккаунты. user_id используется как owner_user_id в rooms/agent_states.';
COMMENT ON TABLE email_verification_tokens IS
    'Одноразовые magic-link токены. Сам токен НЕ хранится — только SHA-256 хеш.';
COMMENT ON TABLE sessions IS
    'HTTP-only cookie-сессии. Opaque session_id + DB lookup. Rolling TTL 30 дней.';
COMMENT ON COLUMN agent_states.owner_user_id IS
    'Владелец помощника. NULL означает legacy (привязка через cogowner-2026).';
"""

DOWNGRADE_SQL = """
-- Сохраняем accounts данные на случай отката (DROP CASCADE не делаем)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'rooms') THEN
        ALTER TABLE rooms DROP COLUMN IF EXISTS owner_user_id;
        ALTER TABLE rooms DROP COLUMN IF EXISTS is_public;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'orchestrator_tasks') THEN
        ALTER TABLE orchestrator_tasks DROP COLUMN IF EXISTS session_id;
        ALTER TABLE orchestrator_tasks DROP COLUMN IF EXISTS owner_user_id;
    END IF;
END $$;

ALTER TABLE agent_states DROP COLUMN IF EXISTS owner_user_id;

DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS email_verification_tokens;
DROP TABLE IF EXISTS accounts;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
