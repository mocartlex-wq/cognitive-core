#!/usr/bin/env bash
# Scene 1 — install. Drive this from the recording host.
# Resets to fresh state each take.

set -euo pipefail
INSTALL_DIR="${INSTALL_DIR:-$HOME/cognitive-core-demo}"

# Reset
docker compose -f "$INSTALL_DIR/docker-compose.public.yml" down -v 2>/dev/null || true
rm -rf "$INSTALL_DIR"

# Pretty: clear, position cursor at top-left, "type" the command at human speed.
clear
echo -ne '\033[0;36m$ \033[0m'
sleep 0.5

CMD='curl -fsSL https://raw.githubusercontent.com/cognitive-core/launch/main/quickstart.sh | bash'
for ((i=0; i<${#CMD}; i++)); do
  printf '%s' "${CMD:$i:1}"
  sleep 0.04
done
echo
sleep 0.6

# Now actually run a local copy (the curl URL won't exist until launch day).
INSTALL_DIR="$INSTALL_DIR" REPO_URL="$INSTALL_DIR" \
  bash "$INSTALL_DIR/quickstart.sh" 2>&1 | sed -u 's/^/  /'
