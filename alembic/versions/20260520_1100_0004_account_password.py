"""accounts.password_hash + password_set_at columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-20 11:00:00 UTC

Добавляет возможность входа по паролю как альтернатива magic-link:
  - accounts.password_hash — argon2 хеш (NULL = пароль не установлен)
  - accounts.password_set_at — когда последний раз меняли пароль

Логика входа:
  • Если password_hash IS NULL → пароль ещё не задан, можно только magic-link
  • Если password_hash IS NOT NULL → можно по паролю ИЛИ magic-link
  • Magic-link всегда работает как recovery — отозвать его нельзя
  • После установки пароля можно отозвать все сессии и заставить переавторизоваться

Bootstrap для владельца:
  Если в .env задана OWNER_BOOTSTRAP_PASSWORD и приходит запрос на
  /auth/password/login с email = OWNER_BOOTSTRAP_EMAIL и совпадающим паролем —
  аккаунт создаётся (если не было) и сразу выдаётся сессия. Это позволяет
  владельцу войти СРАЗУ без сначала-магик-ссылки.
"""
from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS password_set_at TIMESTAMPTZ;

COMMENT ON COLUMN accounts.password_hash IS
    'argon2id хеш пароля. NULL = пароль не задан, можно только magic-link.';
COMMENT ON COLUMN accounts.password_set_at IS
    'Когда пароль был установлен / последний раз изменён.';
"""

DOWNGRADE_SQL = """
ALTER TABLE accounts DROP COLUMN IF EXISTS password_set_at;
ALTER TABLE accounts DROP COLUMN IF EXISTS password_hash;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
