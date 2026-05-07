#!/usr/bin/env bash
# Cognitive Core — GPU readiness check
#
# Проверяет: можно ли на этом хосте запускать GPU-overlay (Dockerfile.gpu).
# Запуск: bash scripts/check_gpu.sh
#
# Exit codes:
#   0 — всё готово, можно запускать с GPU
#   1 — отсутствует NVIDIA driver
#   2 — отсутствует nvidia-container-toolkit
#   3 — Docker не видит GPU

set -e

echo "=== Cognitive Core GPU readiness check ==="
echo

# 1. NVIDIA driver
echo "[1/3] NVIDIA driver..."
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "  FAIL: nvidia-smi not found"
  echo "  Fix:  sudo apt install -y nvidia-driver-550 && sudo reboot"
  exit 1
fi
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
echo "  OK"
echo

# 2. nvidia-container-toolkit
echo "[2/3] nvidia-container-toolkit..."
if ! command -v nvidia-ctk >/dev/null 2>&1; then
  echo "  FAIL: nvidia-container-toolkit not installed"
  echo "  Fix:  see DEPLOY-SERVER.md \"Шаг 10\""
  exit 2
fi
echo "  $(nvidia-ctk --version | head -1)"
echo "  OK"
echo

# 3. Docker → GPU
echo "[3/3] Docker GPU access..."
if ! docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
  echo "  FAIL: Docker не видит GPU"
  echo "  Fix:  sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
  exit 3
fi
echo "  OK — Docker has GPU access"
echo

echo "=== ALL OK ==="
echo "Запуск с GPU:"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build"
