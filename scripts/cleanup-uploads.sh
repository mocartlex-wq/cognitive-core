#!/usr/bin/env bash
# Hourly cleanup для PR #108 resumable upload orphan files.
#
# Background (2026-05-27): PR #108 resumable upload пишет raw bytes в
# /tmp/cogcore-uploads/{upload_id}.bin. На /finalize файл удаляется. Но если
# агент бросил workflow (init → PUT → но finalize не сделал), файл остаётся
# до Redis TTL eviction (1h) или вечно. На сервере с media-heavy use это
# может накопить десятки GB.
#
# Этот скрипт удаляет /tmp/cogcore-uploads/*.bin старше 2 часов (Redis TTL=1h,
# даём grace 1h на retry). Запускается hourly через cogcore-upload-cleanup.timer.

set -euo pipefail

UPLOAD_DIR="/tmp/cogcore-uploads"
MAX_AGE_HOURS=2

if [ ! -d "$UPLOAD_DIR" ]; then
    exit 0  # nothing to clean
fi

log() { logger -t cogcore-upload-cleanup -- "$*"; echo "[$(date -Iseconds)] $*"; }

before_count=$(find "$UPLOAD_DIR" -maxdepth 1 -type f -name "*.bin" 2>/dev/null | wc -l)
before_size_mb=$(du -sm "$UPLOAD_DIR" 2>/dev/null | awk '{print $1}')

# Find + delete orphans
deleted=$(find "$UPLOAD_DIR" -maxdepth 1 -type f -name "*.bin" -mmin +$((MAX_AGE_HOURS * 60)) -delete -print 2>/dev/null | wc -l)
# Also cleanup any non-.bin files that may have been left by finalize rename
find "$UPLOAD_DIR" -maxdepth 1 -type f -mmin +$((MAX_AGE_HOURS * 60)) -delete 2>/dev/null

after_count=$(find "$UPLOAD_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)
after_size_mb=$(du -sm "$UPLOAD_DIR" 2>/dev/null | awk '{print $1}')

if [ "$deleted" -gt 0 ]; then
    log "cleaned $deleted orphan files (was ${before_count} ${before_size_mb}MB → ${after_count} ${after_size_mb}MB)"
fi
