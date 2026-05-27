#!/usr/bin/env bash
# Weekly Docker build cache + unused images prune.
# Runs via cogcore-docker-prune.timer (раз в неделю в 04:00 воскресенье).
#
# Background (2026-05-27): без регулярного prune build cache рос до 147 GB →
# disk 86% за месяц. Этот скрипт chищет всё что docker считает «reclaimable»
# (build cache никогда не используется повторно, dangling images, stopped
# containers). Не трогает running images, named volumes, networks.
#
# Safe to run на live production: docker builder prune не блокирует
# существующие builds, docker image prune не удаляет images используемые
# running контейнерами.

set -euo pipefail

LOG_TAG="cogcore-docker-prune"
log() { logger -t "$LOG_TAG" -- "$*"; echo "[$(date -Iseconds)] $*"; }

log "starting weekly docker prune"

BEFORE_DISK=$(df / | awk 'NR==2 {print $5}')
BEFORE_CACHE=$(docker system df 2>/dev/null | awk '/Build Cache/ {print $4}')
BEFORE_IMAGES=$(docker system df 2>/dev/null | awk '/Images/ {print $4}')

log "before: disk=$BEFORE_DISK build_cache=$BEFORE_CACHE images=$BEFORE_IMAGES"

# Prune builder cache (всегда reclaimable — кеш build steps от прошлых сборок)
RECLAIMED_CACHE=$(docker builder prune -af 2>&1 | grep "reclaimed:" | tail -1 || echo "Total reclaimed: 0B")
log "builder prune: $RECLAIMED_CACHE"

# Prune unused images (-a = and untagged ones — dangling intermediate layers)
RECLAIMED_IMAGES=$(docker image prune -af 2>&1 | grep "reclaimed:" | tail -1 || echo "Total reclaimed: 0B")
log "image prune: $RECLAIMED_IMAGES"

AFTER_DISK=$(df / | awk 'NR==2 {print $5}')
AFTER_CACHE=$(docker system df 2>/dev/null | awk '/Build Cache/ {print $4}')

log "after: disk=$AFTER_DISK build_cache=$AFTER_CACHE"
log "done"

# Optional: alert if disk still >90% (something else eating space)
DISK_PCT=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_PCT" -gt 90 ]; then
    if [ -x /usr/local/bin/cognitive-notify.sh ]; then
        /usr/local/bin/cognitive-notify.sh "WARN: disk still ${DISK_PCT}% after weekly prune. Investigate other sources (db/minio/logs)."
    fi
fi
