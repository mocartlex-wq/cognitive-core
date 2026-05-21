#!/usr/bin/env bash
# Cognitive Core — Linux/macOS installer
#
# Ставит cogmedia CLI + сохраняет API-key для последующих вызовов.
# Запускать одной командой (от wizard'а /ui/connect):
#
#   curl -sSL https://mcp.xn----8sbwawqx4fza.xn--p1ai/static/install-cogcore.sh \
#     | COGNITIVE_API_KEY='your-key-from-wizard' bash
#
# Что делает:
#   1. mkdir ~/.local/bin/ и ~/.config/cogcore/
#   2. Скачивает cogmedia bash-скрипт
#   3. Сохраняет API-key chmod 600
#   4. Подсказывает добавить ~/.local/bin в PATH (если ещё нет)
#   5. Smoke test: GET /health

set -euo pipefail

BASE_URL="${COGCORE_BASE_URL:-https://mcp.xn----8sbwawqx4fza.xn--p1ai}"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/cogcore"
KEY_FILE="$CFG_DIR/api-key"

# ─── Цвета для terminal ─────────────────────────────────────────────────
if [ -t 1 ]; then
    C_CYAN='\033[36m'; C_GREEN='\033[32m'; C_RED='\033[31m'
    C_YELLOW='\033[33m'; C_DIM='\033[2m'; C_RESET='\033[0m'
else
    C_CYAN=''; C_GREEN=''; C_RED=''; C_YELLOW=''; C_DIM=''; C_RESET=''
fi

echo -e "${C_CYAN}Cognitive Core Linux/macOS installer${C_RESET}"
echo -e "${C_CYAN}====================================${C_RESET}"
echo ""

# ─── 0. Pre-flight ──────────────────────────────────────────────────────
API_KEY="${COGNITIVE_API_KEY:-}"
if [ -z "$API_KEY" ]; then
    echo -e "${C_RED}❌ Не задан COGNITIVE_API_KEY.${C_RESET}"
    echo "   Получить ключ: $BASE_URL/ui/connect"
    echo ""
    echo "   Запустить так:"
    echo "     curl -sSL $BASE_URL/static/install-cogcore.sh | COGNITIVE_API_KEY='your-key' bash"
    exit 1
fi

# ─── 1. Создать директории ──────────────────────────────────────────────
echo "[1/5] Создание директорий…"
mkdir -p "$BIN_DIR" "$CFG_DIR"
echo "      $BIN_DIR"
echo "      $CFG_DIR"

# ─── 2. Сохранить API-key (chmod 600) ──────────────────────────────────
echo "[2/5] Сохранение API-key…"
echo -n "$API_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
echo "      $KEY_FILE (chmod 600)"

# ─── 3. Скачать cogmedia ────────────────────────────────────────────────
echo "[3/5] Загрузка cogmedia…"
COGMEDIA_PATH="$BIN_DIR/cogmedia"
if ! curl -fsSL "$BASE_URL/static/cogmedia" -o "$COGMEDIA_PATH"; then
    echo -e "${C_RED}❌ Ошибка загрузки cogmedia${C_RESET}"
    exit 1
fi
chmod +x "$COGMEDIA_PATH"
echo "      $COGMEDIA_PATH"

# ─── 4. PATH hint ───────────────────────────────────────────────────────
echo "[4/5] Проверка PATH…"
if ! echo "$PATH" | tr ':' '\n' | grep -Fxq "$BIN_DIR"; then
    echo -e "      ${C_YELLOW}⚠ $BIN_DIR не в PATH.${C_RESET}"
    echo "      Добавьте в ~/.bashrc или ~/.zshrc:"
    echo -e "        ${C_DIM}export PATH=\"$BIN_DIR:\$PATH\"${C_RESET}"
    echo "      Или используйте полный путь: $COGMEDIA_PATH"
else
    echo "      $BIN_DIR в PATH ✓"
fi

# Hint для env-переменной
echo ""
echo "      Для удобства добавьте в ~/.bashrc или ~/.zshrc:"
echo -e "        ${C_DIM}export COGNITIVE_API_KEY='$API_KEY'${C_RESET}"
echo "      Тогда cogmedia будет использовать env вместо ~/.config/cogcore/api-key."

# ─── 5. Smoke test ──────────────────────────────────────────────────────
echo ""
echo "[5/5] Smoke test: GET $BASE_URL/health…"
if health=$(curl -sf --max-time 8 "$BASE_URL/health"); then
    if command -v python3 >/dev/null 2>&1; then
        echo "$health" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"      healthy={d['healthy']} version={d['version']} L1={d['layers']['l1']}\")
except Exception as e:
    print(f'      raw: {sys.stdin.read()[:120]}')
" || echo "      raw: $(echo "$health" | head -c 200)"
    else
        echo "      $(echo "$health" | head -c 200)"
    fi
else
    echo -e "      ${C_YELLOW}⚠ health endpoint недоступен (сеть?)${C_RESET}"
fi

echo ""
echo -e "${C_GREEN}✅ Готово!${C_RESET}"
echo ""
echo "Используйте:"
echo -e "  ${C_CYAN}cogmedia ~/photo.png${C_RESET}"
echo -e "  ${C_CYAN}cogmedia ~/Videos/demo.mp4${C_RESET}"
echo ""
echo "Документация: $BASE_URL/ui/connect"
