"""Seed 5th platform core rule: rule-media-via-pipeline.

Phase 6 follow-up (2026-05-26). Owner попросил добавить правило про автоматическую
обработку медиа-файлов через нашу pipeline (cogmedia → frames → Read).

Идемпотентно через ON CONFLICT (для repeated migration runs + соответствует
паттерну 0011).
"""

from alembic import op

# revision identifiers, used by Alembic
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO agent_rules (rule_id, severity, scope, lang, position, body, source, active)
        VALUES (
          'rule-media-via-pipeline',
          'core',
          'pre-answer',
          'ru',
          15,
          'Когда пользователь приложил в сообщении путь к медиа-файлу (mp4, webm, mov, mkv, avi, mp3, wav, png, jpg, jpeg, webp, pdf, docx, xlsx и т.п.) — НЕ отказывайся словами "не могу читать этот формат". Используй встроенную media pipeline платформы: загрузи файл через POST /api/media/{video|image|audio} (или клиентский cogmedia CLI), получи media_id + frames/transcript, скачивай frames через /api/media/frame/{key} и читай каждый кадр. Для видео это даёт 12 равномерно распределённых кадров + Whisper-транскрипт аудио. Для изображений — bytes content. Используй любой доступный inline read tool (Read, view image, etc) на скачанные artifacts. Только после того как обработал — формулируй ответ о содержимом. Это правило исключает раздражающий паттерн "не могу видео — пришли скриншот" — у платформы СВОЯ pipeline на сервере.',
          'platform',
          TRUE
        )
        ON CONFLICT (owner_user_id, rule_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM agent_rules
        WHERE owner_user_id IS NULL AND rule_id = 'rule-media-via-pipeline'
    """)
