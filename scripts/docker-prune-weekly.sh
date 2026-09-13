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

# Размеры берём через --format, а не колонками вывода для человека.
# Прежняя версия читала `awk '/Build Cache/ {print $4}'`, но название типа
# «Build Cache» — ДВА слова, и все колонки в этой строке сдвинуты на одну
# относительно строки «Images». $4 там — число активных записей, а не
# размер. Поэтому журнал каждый день писал build_cache=0, пока кэш весил
# 23 ГБ, — и этот ноль ещё и читался как «кэш уже пуст». Замер лгал с
# момента написания. Обнаружено 13.09.2026.
df_size() { docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null | awk -F'|' -v t="$1" '$1==t {print $2}'; }

BEFORE_DISK=$(df / | awk 'NR==2 {print $5}')
BEFORE_CACHE=$(df_size "Build Cache")
BEFORE_IMAGES=$(df_size "Images")

log "before: disk=$BEFORE_DISK build_cache=$BEFORE_CACHE images=$BEFORE_IMAGES"

# Кэш сборщика — УЖИМАЕМ до потолка, а не выметаем целиком.
#
# Было `docker builder prune -af`: каждый день в 04:30 удалялся ВЕСЬ кэш,
# который не использовался в эту секунду. Кэш сборщика на машине общий —
# им пользуются и наш образ, и соседний контур. Значит любая сборка после
# 04:30 шла с холодного кэша и заново качала все колёса. Соседи 13.09
# собирали прод больше часа и искали причину в отсутствии политики GC;
# политика была — самая агрессивная из возможных, и она была нашей.
#
# --max-used-space выметает по давности использования, пока кэш не
# уложится в потолок: свежие слои зависимостей переживают уборку, старые
# уходят. -a снят: он добавлял к удалению внутренние образы фронтенда
# сборки, которые при следующей сборке пришлось бы тянуть заново.
BUILD_CACHE_CAP="${BUILD_CACHE_CAP:-20GB}"
RECLAIMED_CACHE=$(docker builder prune -f --max-used-space "$BUILD_CACHE_CAP" 2>&1 | grep -iE "reclaimed" | tail -1)
RECLAIMED_CACHE="${RECLAIMED_CACHE:-Total reclaimed: 0B}"
log "builder prune: $RECLAIMED_CACHE"

# Prune unused images (-a = and untagged ones — dangling intermediate layers)
RECLAIMED_IMAGES=$(docker image prune -af 2>&1 | grep "reclaimed:" | tail -1 || echo "Total reclaimed: 0B")
log "image prune: $RECLAIMED_IMAGES"

AFTER_DISK=$(df / | awk 'NR==2 {print $5}')
AFTER_CACHE=$(df_size "Build Cache")

log "after: disk=$AFTER_DISK build_cache=$AFTER_CACHE"
log "done"

# Optional: alert if disk still >90% (something else eating space)
DISK_PCT=$(df / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_PCT" -gt 90 ]; then
    if [ -x /usr/local/bin/cognitive-notify.sh ]; then
        /usr/local/bin/cognitive-notify.sh "WARN: disk still ${DISK_PCT}% after weekly prune. Investigate other sources (db/minio/logs)."
    fi
fi
