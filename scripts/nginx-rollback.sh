#!/usr/bin/env bash
# Cognitive Core — nginx config emergency rollback (config-only, no code revert).
# Usage:
#   bash scripts/nginx-rollback.sh           # list snapshots
#   bash scripts/nginx-rollback.sh <sha7>    # restore that snapshot + reload
#   bash scripts/nginx-rollback.sh latest    # restore most recent snapshot
#
# Snapshots are written by conditional_reload.sh into nginx/history/
# (last 10 known-working nginx.conf versions, named by their commit SHA-7).

set -euo pipefail

REPO_DIR="${COGNITIVE_REPO_DIR:-/opt/cognitive-core}"
HIST="$REPO_DIR/nginx/history"

if [ ! -d "$HIST" ] || [ -z "$(ls -A "$HIST" 2>/dev/null || true)" ]; then
    echo "No snapshots in $HIST yet (will populate after first nginx-touching deploy)."
    exit 1
fi

if [ $# -eq 0 ]; then
    echo "Available nginx.conf snapshots (newest first):"
    # shellcheck disable=SC2012
    ls -t "$HIST"/nginx.conf.* | head -10 | sed 's|^|  |'
    echo
    echo "Usage: $0 <sha7-or-latest>"
    exit 0
fi

if [ "$1" = "latest" ]; then
    SNAP=$(ls -t "$HIST"/nginx.conf.* | head -1)
else
    SNAP="$HIST/nginx.conf.$1"
fi

if [ ! -f "$SNAP" ]; then
    echo "ERROR: snapshot not found: $SNAP" >&2
    exit 1
fi

echo "Restoring nginx.conf from $SNAP"
cp "$SNAP" "$REPO_DIR/nginx/nginx.conf"

echo "Validating + reloading nginx..."
docker exec cognitive_nginx nginx -t
docker exec cognitive_nginx nginx -s reload

echo "OK. Note: this restores config IN WORKING TREE only. To make it the"
echo "canonical version, commit the change in your local clone:"
echo "  git checkout -b fix/nginx-rollback-\$(date +%Y%m%d)"
echo "  git add nginx/nginx.conf && git commit -m 'rollback nginx to <sha>' && git push"
