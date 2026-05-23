#!/usr/bin/env bash
# Cognitive Core — Linux/macOS installer v3 (idempotent + auto-onboard)
#
# Usage:
#   # Первая установка (нужен claim-token):
#   curl -sSL https://mcp.me-ai.ru/static/install-cogcore.sh | COGNITIVE_API_KEY='...' bash
#
#   # Re-install / heartbeat — без COGNITIVE_API_KEY (использует существующий):
#   curl -sSL https://mcp.me-ai.ru/static/install-cogcore.sh | bash
#
# v3 features:
#   - Machine fingerprint = sha256(hostname+user+os)[:16] (стабильный id машины)
#   - ~/.cognitive-core/agent.json — local registry
#   - При re-run чекает существующий agent.json + pings /agents/heartbeat:
#       • 200 OK → reuse, exit "already installed as X"
#       • 401 → key revoked → нужен новый claim-token
#       • No file → claim flow
#   - Auto-onboard: server смотрит fingerprint, если уже есть agent с тем же fp
#     → reuse (предотвращает дубликаты)

set -euo pipefail

BASE_URL="${COGCORE_BASE_URL:-https://mcp.me-ai.ru}"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.cognitive-core"
AGENT_REG="$CFG_DIR/agent.json"
LEGACY_KEY="$HOME/.config/cogcore/api-key"

# Colors
if [ -t 1 ]; then
    C_CYAN='\033[36m'; C_GREEN='\033[32m'; C_RED='\033[31m'
    C_YELLOW='\033[33m'; C_DIM='\033[2m'; C_RESET='\033[0m'
else
    C_CYAN=''; C_GREEN=''; C_RED=''; C_YELLOW=''; C_DIM=''; C_RESET=''
fi

echo -e "${C_CYAN}Cognitive Core installer v3 (idempotent + multi-agent registry)${C_RESET}"
echo -e "${C_CYAN}================================================================${C_RESET}"
echo ""

mkdir -p "$BIN_DIR" "$CFG_DIR"

# ─── 1. Machine fingerprint (stable per-machine identifier) ─────────────
HOSTNAME_S=$(hostname 2>/dev/null || echo "unknown")
USER_S=$(whoami 2>/dev/null || echo "unknown")
OS_S=$(uname -s 2>/dev/null || echo "unknown")
MACHINE_FP=$(echo -n "${HOSTNAME_S}|${USER_S}|${OS_S}" | sha256sum 2>/dev/null | cut -c1-16)
if [ -z "$MACHINE_FP" ]; then
    # macOS fallback (shasum instead of sha256sum)
    MACHINE_FP=$(echo -n "${HOSTNAME_S}|${USER_S}|${OS_S}" | shasum -a 256 | cut -c1-16)
fi
MACHINE_LABEL="${USER_S}@${HOSTNAME_S} (${OS_S})"
echo -e "machine_fp:    ${C_DIM}${MACHINE_FP}${C_RESET} ($MACHINE_LABEL)"

# ─── 2. Check existing installation (idempotent re-run) ─────────────────
KEY=""
if [ -f "$AGENT_REG" ] && command -v python3 >/dev/null; then
    EXISTING_KEY=$(python3 -c "import json; print(json.load(open('$AGENT_REG')).get('api_key',''))" 2>/dev/null || echo "")
    if [ -n "$EXISTING_KEY" ]; then
        echo "[1/5] Проверка существующей установки..."
        STATUS=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 5 \
                 -H "X-API-Key: $EXISTING_KEY" \
                 "$BASE_URL/agents/inbox?limit=1" 2>/dev/null || echo "000")
        if [ "$STATUS" = "200" ]; then
            EXISTING_AGENT=$(python3 -c "import json; print(json.load(open('$AGENT_REG')).get('agent_id','?'))" 2>/dev/null)
            echo -e "      ${C_GREEN}✓ Helper уже установлен: $EXISTING_AGENT${C_RESET}"
            KEY="$EXISTING_KEY"
            # Refresh server-side machine_label + heartbeat
            curl -s --max-time 5 -X POST "$BASE_URL/user/connect/auto-onboard" \
                 -H "Content-Type: application/json" \
                 -d "{\"machine_fingerprint\":\"$MACHINE_FP\",\"machine_label\":\"$MACHINE_LABEL\",\"api_key\":\"$EXISTING_KEY\"}" \
                 >/dev/null 2>&1 || true
            echo "      heartbeat обновлён на сервере"
        elif [ "$STATUS" = "401" ]; then
            echo -e "      ${C_YELLOW}⚠ api_key revoked${C_RESET} — нужен новый claim-token"
        else
            echo -e "      ${C_YELLOW}⚠ server unreachable (HTTP $STATUS)${C_RESET}"
        fi
    fi
fi

# ─── 3. New install (need claim-token) ──────────────────────────────────
if [ -z "$KEY" ]; then
    KEY="${COGNITIVE_API_KEY:-}"
    if [ -z "$KEY" ]; then
        # Пытаемся auto-onboard по fingerprint (без key) — server подскажет если уже есть
        AUTO_RESP=$(curl -s --max-time 5 -X POST "$BASE_URL/user/connect/auto-onboard" \
                    -H "Content-Type: application/json" \
                    -d "{\"machine_fingerprint\":\"$MACHINE_FP\",\"machine_label\":\"$MACHINE_LABEL\"}" \
                    2>/dev/null || echo "{}")
        if echo "$AUTO_RESP" | grep -q '"status":"found_but_locked"'; then
            AGENT_HINT=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_id','?'))" <<< "$AUTO_RESP" 2>/dev/null || echo "?")
            echo -e "${C_YELLOW}⚠ На этой машине уже зарегистрирован helper «$AGENT_HINT»${C_RESET}"
            echo "  но api_key не найден локально (~/.cognitive-core/agent.json пусто)."
            echo "  Owner должен сгенерировать новый claim-token в:"
            echo -e "    ${C_CYAN}$BASE_URL/ui/profile${C_RESET} → «🪄 Передать помощнику»"
        else
            echo -e "${C_RED}❌ COGNITIVE_API_KEY не задан${C_RESET}"
            echo "  Получите ключ:"
            echo -e "    ${C_CYAN}$BASE_URL/ui/connect${C_RESET}"
            echo "  Запустите так:"
            echo -e "    ${C_DIM}curl -sSL $BASE_URL/static/install-cogcore.sh | COGNITIVE_API_KEY='your-key' bash${C_RESET}"
        fi
        exit 1
    fi
    echo "[1/5] Новая установка с предоставленным api_key"

    # Bind fingerprint к этому ключу + сохранить agent metadata
    RESP=$(curl -s --max-time 8 -X POST "$BASE_URL/user/connect/auto-onboard" \
           -H "Content-Type: application/json" \
           -d "{\"machine_fingerprint\":\"$MACHINE_FP\",\"machine_label\":\"$MACHINE_LABEL\",\"api_key\":\"$KEY\"}" \
           2>/dev/null || echo "{}")
    AGENT_ID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('agent_id','unknown'))" <<< "$RESP" 2>/dev/null || echo "unknown")
    if [ "$AGENT_ID" = "unknown" ]; then
        echo -e "${C_RED}❌ Auto-onboard failed${C_RESET}: $RESP"
        exit 1
    fi
    echo -e "      ${C_GREEN}✓ Agent: $AGENT_ID${C_RESET}"

    # Save to registry
    cat > "$AGENT_REG" << JSON
{
  "agent_id": "$AGENT_ID",
  "api_key": "$KEY",
  "machine_fingerprint": "$MACHINE_FP",
  "machine_label": "$MACHINE_LABEL",
  "server": "$BASE_URL",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
    chmod 600 "$AGENT_REG"
    echo "      registry → $AGENT_REG (chmod 600)"
fi

# ─── 4. Install cogmedia + legacy key file ──────────────────────────────
echo "[3/5] Загрузка cogmedia..."
COGMEDIA_PATH="$BIN_DIR/cogmedia"
if curl -fsSL "$BASE_URL/static/cogmedia" -o "$COGMEDIA_PATH" 2>/dev/null; then
    chmod +x "$COGMEDIA_PATH"
    echo -e "      ${C_GREEN}✓${C_RESET} $COGMEDIA_PATH"
else
    echo -e "      ${C_YELLOW}⚠ Не удалось загрузить cogmedia (не критично — agent работает без него)${C_RESET}"
fi

# Legacy key file для cogmedia compatibility
mkdir -p "$(dirname "$LEGACY_KEY")"
echo -n "$KEY" > "$LEGACY_KEY"
chmod 600 "$LEGACY_KEY"
echo "      legacy key → $LEGACY_KEY"

# ─── 5. PATH hint ──────────────────────────────────────────────────────
echo "[4/5] Проверка PATH..."
if ! echo "$PATH" | tr ':' '\n' | grep -Fxq "$BIN_DIR"; then
    echo -e "      ${C_YELLOW}⚠ $BIN_DIR не в PATH${C_RESET}"
    echo "      Добавьте в ~/.bashrc или ~/.zshrc:"
    echo -e "        ${C_DIM}export PATH=\"$BIN_DIR:\$PATH\"${C_RESET}"
else
    echo -e "      ${C_GREEN}✓${C_RESET} $BIN_DIR в PATH"
fi

# ─── 6. Smoke test ──────────────────────────────────────────────────────
echo "[5/5] Smoke test..."
if health=$(curl -sf --max-time 5 "$BASE_URL/health" 2>/dev/null); then
    if command -v python3 >/dev/null; then
        python3 -c "
import json, sys
d = json.loads('''$health''')
print(f'      \033[32m✓\033[0m cognitive-core healthy={d[\"healthy\"]} version={d[\"version\"]} L1={d[\"layers\"][\"l1\"]}')
" || echo "      $health"
    fi
fi

echo ""
echo -e "${C_GREEN}════════════════════════════════════════════════${C_RESET}"
echo -e "${C_GREEN}✅ Готово!${C_RESET} agent: ${C_CYAN}$([ -f "$AGENT_REG" ] && python3 -c "import json; print(json.load(open('$AGENT_REG'))['agent_id'])" 2>/dev/null || echo "?")${C_RESET}"
echo -e "${C_DIM}Команды:${C_RESET}"
echo -e "  ${C_CYAN}cogmedia ~/photo.png${C_RESET}     # analyze image"
echo -e "  ${C_CYAN}cogmedia ~/video.mp4${C_RESET}     # analyze video"
echo -e "  Re-run этого installer → idempotent (не дублирует)"
echo ""
echo -e "${C_DIM}Dashboard: $BASE_URL/ui/profile${C_RESET}"
