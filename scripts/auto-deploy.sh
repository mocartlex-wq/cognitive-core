#!/usr/bin/env bash
# Cognitive Core — auto-deploy poller.
# Запускается systemd-timer-ом каждые 60 сек на сервере.
# Делает: git fetch → если HEAD сменился → git pull --ff-only → conditional_reload.sh.
# Идемпотентен. При отсутствии изменений — тихо завершает работу.
#
# Logs to /var/log/cognitive-deploy.log (через systemd journal).

set -euo pipefail

REPO_DIR="${COGNITIVE_REPO_DIR:-/opt/cognitive-core}"
BRANCH="${COGNITIVE_DEPLOY_BRANCH:-main}"

cd "$REPO_DIR"

# Не паникуем если git fetch упал по сети — попробуем в следующий тик
if ! git fetch --quiet origin "$BRANCH" 2>&1; then
    echo "[$(date -Iseconds)] git fetch failed, will retry next tick" >&2
    exit 0
fi

PREV=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")

if [ "$PREV" = "$NEW" ]; then
    exit 0
fi

echo "[$(date -Iseconds)] new commits detected: ${PREV:0:7} -> ${NEW:0:7}"

# Fast-forward only — отказываемся deploy-ить если local diverged
if ! git merge-base --is-ancestor "$PREV" "$NEW"; then
    echo "[$(date -Iseconds)] ERROR: local HEAD ${PREV:0:7} is not ancestor of origin/${BRANCH} ${NEW:0:7} — manual intervention required" >&2
    exit 1
fi

git pull --ff-only --quiet origin "$BRANCH"

# Передаём в conditional_reload range изменений — он сам решит что трогать
"$REPO_DIR/scripts/conditional_reload.sh" "$PREV" "$NEW"

echo "[$(date -Iseconds)] deploy complete: $NEW"
