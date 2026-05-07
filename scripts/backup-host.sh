#!/usr/bin/env bash
# Cognitive Core — host-side backup
# Запускается из cron каждые 6 часов. Делает pg_dump + ротацию (14 копий)
set -e

TS=$(date +%Y%m%d_%H%M%S)
PG_DIR=/opt/cognitive-core/backups/postgres
LOG=/var/log/cognitive-core/backup.log

mkdir -p "$PG_DIR"

# Postgres dump (gzipped)
docker exec cognitive_postgres pg_dump -U cognitive cognitive_core 2>>$LOG | gzip > "$PG_DIR/dump_${TS}.sql.gz"
ln -sf "dump_${TS}.sql.gz" "$PG_DIR/latest.sql.gz"

# Ротация: оставляем 14 последних
ls -t "$PG_DIR"/dump_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm

SIZE=$(du -h "$PG_DIR/dump_${TS}.sql.gz" | cut -f1)
echo "[$(date -Iseconds)] OK dump_${TS}.sql.gz size=${SIZE}" | tee -a $LOG
