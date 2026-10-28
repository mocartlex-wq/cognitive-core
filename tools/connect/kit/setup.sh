#!/usr/bin/env bash
# Подключить этот ПК (Linux/macOS) к Cognitive Core: память, комната агентов, контрольные точки.
#
#   COGCORE_API_KEY=<ключ> bash -c "$(curl -fsSL https://mcp.me-ai.ru/connect/kit/setup.sh)"
#   или с одноразовым кодом из кабинета (/ui/profile -> «Передать помощнику»):
#   COGCORE_CLAIM=ABCD-1234 bash -c "$(curl -fsSL https://mcp.me-ai.ru/connect/kit/setup.sh)"
#
# Повторный запуск безопасен. Ключ не печатается и не уходит в URL.
set -euo pipefail
API="${COGCORE_API:-https://mcp.me-ai.ru}"
DIR="$HOME/.cognitive-core"
mkdir -p "$DIR"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "Нужен Python 3.10+ (python3), затем повторите."; exit 1; fi
for f in cogcore_kit.py setup_core.py; do
  curl -fsSL "$API/connect/kit/$f" -o "$DIR/$f"
done
ARGS=(-X utf8 "$DIR/setup_core.py" --api "$API")
if [ -n "${COGCORE_CLAIM:-}" ]; then ARGS+=(--claim "$COGCORE_CLAIM"); fi
exec "$PY" "${ARGS[@]}"
