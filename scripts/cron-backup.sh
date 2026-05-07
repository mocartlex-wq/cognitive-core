#!/bin/sh
# Auto-backup script — runs every 6h via cron in backup container.
# Backs up: Postgres dump + MinIO L4 snapshots mirror.

set -eu

TS=$(date +%Y%m%d_%H%M%S)
PG_OUT=/backups/postgres/${DB_NAME}_${TS}.sql.gz
S3_OUT=/backups/minio/${TS}/

mkdir -p /backups/postgres /backups/minio

echo "[$(date -u +%FT%TZ)] Backup START"

# === Postgres ===
echo "[pg_dump] $PG_OUT"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
    --no-owner --no-acl | gzip -9 > "$PG_OUT"

PG_SIZE=$(stat -c%s "$PG_OUT" 2>/dev/null || stat -f%z "$PG_OUT")
echo "[pg_dump] size=${PG_SIZE} bytes"
if [ "$PG_SIZE" -lt 1024 ]; then
    echo "[ERROR] pg_dump output too small ($PG_SIZE bytes), backup likely failed" >&2
    exit 1
fi

ln -sf "$(basename "$PG_OUT")" "/backups/postgres/latest.sql.gz"

# === MinIO L4 snapshots mirror ===
mkdir -p "$S3_OUT"
mc alias set local "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" --quiet 2>/dev/null || true
mc mirror --quiet --overwrite local/l4-snapshots "$S3_OUT" 2>&1 | tail -3 || \
    echo "[WARN] MinIO mirror failed (bucket may be empty)"

# === Rotation: delete backups older than RETENTION_DAYS ===
RETENTION=${RETENTION_DAYS:-14}
DELETED_PG=$(find /backups/postgres -name "*.sql.gz" -mtime "+$RETENTION" -print -delete 2>/dev/null | wc -l)
DELETED_S3=$(find /backups/minio -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION" -print -exec rm -rf {} + 2>/dev/null | wc -l)
[ "$DELETED_PG" -gt 0 ] && echo "[rotation] removed $DELETED_PG old pg backups"
[ "$DELETED_S3" -gt 0 ] && echo "[rotation] removed $DELETED_S3 old s3 backups"

echo "[$(date -u +%FT%TZ)] Backup OK"
