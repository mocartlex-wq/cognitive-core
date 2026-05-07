#!/usr/bin/env bash
# Cognitive Core — Remote Bootstrap
#
# Запускается локально на dev-машине. Через SSH разворачивает Cognitive Core
# на удалённом Linux-сервере (Ubuntu 22/24, Debian 12+).
#
# Требования:
#   - SSH доступ к серверу с sudo привилегиями
#   - Сервер с минимум 4 CPU / 8 GB RAM / 50 GB free
#   - Интернет на сервере (для apt + Docker Hub + DeepSeek API)
#
# Usage:
#   SERVER_HOST=192.168.0.100 SERVER_USER=admin \
#   SERVER_KEY=~/.ssh/id_rsa \
#   bash scripts/bootstrap-remote.sh
#
#   # или с паролем:
#   SERVER_HOST=192.168.0.100 SERVER_USER=admin SERVER_PASS='secret' \
#   bash scripts/bootstrap-remote.sh

set -e

SERVER_HOST="${SERVER_HOST:?Set SERVER_HOST env var (e.g. 192.168.0.100)}"
SERVER_USER="${SERVER_USER:?Set SERVER_USER env var (e.g. admin)}"
SERVER_PORT="${SERVER_PORT:-22}"
SERVER_PATH="${SERVER_PATH:-/opt/cognitive-core}"

echo "=== Cognitive Core remote bootstrap ==="
echo "Target: ${SERVER_USER}@${SERVER_HOST}:${SERVER_PORT}"
echo "Install path: ${SERVER_PATH}"
echo

SSH_CMD="ssh -p ${SERVER_PORT} -o StrictHostKeyChecking=accept-new"
if [ -n "${SERVER_KEY:-}" ]; then
    SSH_CMD="${SSH_CMD} -i ${SERVER_KEY}"
fi
if [ -n "${SERVER_PASS:-}" ]; then
    if ! command -v sshpass >/dev/null 2>&1; then
        echo "ERROR: sshpass needed for password auth. Install: sudo apt install sshpass"
        exit 1
    fi
    SSH_CMD="sshpass -p '${SERVER_PASS}' ${SSH_CMD}"
fi

REMOTE="${SERVER_USER}@${SERVER_HOST}"

# Проверка connectivity
echo "[1/7] SSH connectivity..."
${SSH_CMD} ${REMOTE} 'uname -a && echo === / disk && df -h / && echo === RAM && free -h && echo === CPU && nproc' || {
    echo "FAIL: cannot SSH to server"
    exit 1
}
echo

# Установка docker если нет
echo "[2/7] Docker install (если нет)..."
${SSH_CMD} ${REMOTE} 'bash -s' <<'REMOTE_SCRIPT'
set -e
if command -v docker >/dev/null 2>&1; then
    echo "  Docker already: $(docker --version)"
else
    echo "  Installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker $USER
fi
if docker compose version >/dev/null 2>&1; then
    echo "  Compose already: $(docker compose version)"
else
    echo "  Installing docker-compose-plugin..."
    sudo apt install -y docker-compose-plugin
fi
REMOTE_SCRIPT
echo

# Подготовка install path
echo "[3/7] Preparing ${SERVER_PATH}..."
${SSH_CMD} ${REMOTE} "sudo mkdir -p ${SERVER_PATH} && sudo chown \$USER:\$USER ${SERVER_PATH}"
echo

# Перенос проекта (rsync если есть, иначе scp)
echo "[4/7] Transferring project..."
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if command -v rsync >/dev/null 2>&1; then
    rsync -az -e "ssh -p ${SERVER_PORT}${SERVER_KEY:+ -i $SERVER_KEY}" \
        --exclude='.git' --exclude='backups' --exclude='__pycache__' \
        --exclude='.env' --exclude='node_modules' --exclude='*.log' \
        "${LOCAL_DIR}/" "${REMOTE}:${SERVER_PATH}/"
else
    echo "  rsync not found — using tar over SSH..."
    cd "${LOCAL_DIR}"
    tar --exclude='.git' --exclude='backups' --exclude='__pycache__' \
        --exclude='.env' --exclude='*.log' -czf - . \
      | ${SSH_CMD} ${REMOTE} "tar -xzf - -C ${SERVER_PATH}"
fi
echo

# Запуск install-server.sh на сервере
echo "[5/7] Running install-server.sh on remote..."
${SSH_CMD} ${REMOTE} "cd ${SERVER_PATH} && bash install-server.sh"
echo

# Health probe
echo "[6/7] Waiting for healthy..."
for i in {1..30}; do
    if ${SSH_CMD} ${REMOTE} "curl -sf http://localhost:9001/health >/dev/null 2>&1"; then
        echo "  HEALTHY at attempt $i"
        break
    fi
    sleep 5
done
echo

# Final report
echo "[7/7] Final report:"
${SSH_CMD} ${REMOTE} "curl -s http://localhost:9001/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:9001/health"
echo
echo "=== DONE ==="
echo "Connect from this PC:"
echo "  curl http://${SERVER_HOST}:9001/health"
echo "  open: http://${SERVER_HOST}:9001/  (dashboard)"
