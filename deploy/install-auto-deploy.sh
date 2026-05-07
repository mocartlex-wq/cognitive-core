#!/usr/bin/env bash
# Cognitive Core — one-shot setup of auto-deploy on the server.
# Запускать ОДИН РАЗ на сервере после первого git pull.
#
# Что делает:
#   1. Копирует unit/timer в /etc/systemd/system/
#   2. systemctl daemon-reload
#   3. enable + start cognitive-deploy.timer
#   4. Показывает первый dry-run статус
#
# Pre-req:
#   - /opt/cognitive-core/ — рабочий git-checkout с настроенным remote
#   - /opt/cognitive-core/scripts/auto-deploy.sh, conditional_reload.sh — executable
#   - Sudoers разрешает salex выполнять docker без пароля (или скрипты запускаются от root)

set -euo pipefail

REPO_DIR="${COGNITIVE_REPO_DIR:-/opt/cognitive-core}"

if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: $REPO_DIR not found. Clone the repo first."
    exit 1
fi

echo "==> Making deploy scripts executable"
chmod +x "$REPO_DIR/scripts/auto-deploy.sh" "$REPO_DIR/scripts/conditional_reload.sh"

echo "==> Installing systemd units"
sudo cp "$REPO_DIR/deploy/cognitive-deploy.service" /etc/systemd/system/cognitive-deploy.service
sudo cp "$REPO_DIR/deploy/cognitive-deploy.timer"   /etc/systemd/system/cognitive-deploy.timer

echo "==> systemctl daemon-reload"
sudo systemctl daemon-reload

echo "==> Enabling and starting timer"
sudo systemctl enable --now cognitive-deploy.timer

echo "==> Status:"
sudo systemctl status cognitive-deploy.timer --no-pager || true

echo
echo "OK. Auto-deploy is live. From now on:"
echo "  - Push to origin/main from any machine"
echo "  - Within ~60s the server pulls and conditionally reloads"
echo "  - Watch deploys: journalctl -u cognitive-deploy -n 50 -f"
