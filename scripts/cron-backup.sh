#!/bin/sh
# Auto-backup script — runs every 6h via cron in backup container.
# Backs up: Postgres dumps (one file per DB) + MinIO L4 snapshots mirror.
#
# Multi-DB support (added 2026-05-08):
#   - DBS_TO_BACKUP="db1 db2 db3" (whitespace-separated). Each DB gets own
#     pg_dump file: /backups/postgres/<dbname>_<timestamp>.sql.gz
#   - Uses single $DB_USER credentials — that user must have CONNECT on all DBs.
#     For ai_crm DB owned by ai_crm_user, give cognitive (superuser in compose .env)
#     or grant CONNECT explicitly.
#   - If DBS_TO_BACKUP is empty, falls back to single $DB_NAME (legacy).
#
# Retention default lowered from 14 to 7 days (2026-05-08, DS-recommendation
# for 97 GB disk: 4 backups/day × 7 days × 2 DBs ≈ manageable footprint).

set -eu

TS=$(date +%Y%m%d_%H%M%S)
S3_OUT=/backups/minio/${TS}/

mkdir -p /backups/postgres /backups/minio

echo "[$(date -u +%FT%TZ)] Backup START"

# === Postgres — multi-DB loop ===
DBS="${DBS_TO_BACKUP:-$DB_NAME}"
for DB in $DBS; do
    PG_OUT=/backups/postgres/${DB}_${TS}.sql.gz
    echo "[pg_dump] $PG_OUT"
    if PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "$DB_HOST" -U "$DB_USER" -d "$DB" \
        --no-owner --no-acl | gzip -9 > "$PG_OUT"; then
        PG_SIZE=$(stat -c%s "$PG_OUT" 2>/dev/null || stat -f%z "$PG_OUT")
        echo "[pg_dump $DB] size=${PG_SIZE} bytes"
        if [ "$PG_SIZE" -lt 1024 ]; then
            echo "[ERROR] pg_dump $DB output too small ($PG_SIZE bytes), backup likely failed" >&2
            rm -f "$PG_OUT"
            # don't exit — try other DBs and rotation; final exit code reflects partial failure
            FAILED=1
        else
            ln -sf "$(basename "$PG_OUT")" "/backups/postgres/${DB}_latest.sql.gz"
        fi
    else
        echo "[ERROR] pg_dump $DB command failed" >&2
        FAILED=1
    fi
done

# === MinIO L4 snapshots mirror ===
mkdir -p "$S3_OUT"
mc alias set local "$S3_ENDPOINT" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" --quiet 2>/dev/null || true
mc mirror --quiet --overwrite local/l4-snapshots "$S3_OUT" 2>&1 | tail -3 || \
    echo "[WARN] MinIO mirror failed (bucket may be empty)"

# === Rotation: delete backups older than RETENTION_DAYS ===
RETENTION=${RETENTION_DAYS:-7}
DELETED_PG=$(find /backups/postgres -name "*.sql.gz" -mtime "+$RETENTION" -print -delete 2>/dev/null | wc -l)
DELETED_S3=$(find /backups/minio -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION" -print -exec rm -rf {} + 2>/dev/null | wc -l)
[ "$DELETED_PG" -gt 0 ] && echo "[rotation] removed $DELETED_PG old pg backups (TTL ${RETENTION}d)"
[ "$DELETED_S3" -gt 0 ] && echo "[rotation] removed $DELETED_S3 old s3 backups (TTL ${RETENTION}d)"

if [ "${FAILED:-0}" = "1" ]; then
    echo "[$(date -u +%FT%TZ)] Backup PARTIAL — at least one pg_dump failed, see errors above" >&2
    exit 1
fi

echo "[$(date -u +%FT%TZ)] Backup OK"
