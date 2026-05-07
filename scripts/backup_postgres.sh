#!/usr/bin/env bash
# Cognitive Core — Postgres backup script
# Делает pg_dump + ротация (хранит N дней)
# Cron: 0 */6 * * * /opt/cognitive-core/scripts/backup_postgres.sh

set -euo pipefail

# Конфигурация (можно переопределить через env)
BACKUP_DIR="${BACKUP_DIR:-/opt/cognitive-core/backups/postgres}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
CONTAINER="${CONTAINER:-cognitive_postgres}"
DB_USER="${DB_USER:-cognitive}"
DB_NAME="${DB_NAME:-cognitive_core}"

mkdir -p "$BACKUP_DIR"

TS=$(date +%Y%m%d_%H%M%S)
OUT="$BACKUP_DIR/${DB_NAME}_${TS}.sql.gz"

echo "[$(date)] Backup $DB_NAME → $OUT"

# pg_dump через docker exec, сжимаем налету
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl \
    | gzip -9 > "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo "[$(date)] Done: $SIZE"

# Ротация: удалить старше RETENTION_DAYS
DELETED=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime "+$RETENTION_DAYS" -print -delete | wc -l)
if [ "$DELETED" -gt 0 ]; then
    echo "[$(date)] Rotated $DELETED old backups (>$RETENTION_DAYS days)"
fi

# Симлинк latest для удобства восстановления
ln -sf "$(basename "$OUT")" "$BACKUP_DIR/latest.sql.gz"

# Health-check: бэкап не должен быть подозрительно маленьким
MIN_SIZE_BYTES=1024
ACTUAL_BYTES=$(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT")
if [ "$ACTUAL_BYTES" -lt "$MIN_SIZE_BYTES" ]; then
    echo "[$(date)] ERROR: backup suspiciously small ($ACTUAL_BYTES bytes)" >&2
    exit 1
fi

echo "[$(date)] Backup OK"
