#!/usr/bin/env bash
# Cogcore Orchestrator — one-time setup script for production server.
#
# Идемпотентен: можно запускать многократно, не ломает существующий setup.
# При повторном запуске не пересоздаёт ключ если он уже есть в env.
#
# Что делает:
#  1. Регистрирует agent_id=orchestrator через /agents/register если ещё нет ключа
#  2. Сохраняет ключ в /etc/cogcore-orchestrator.env (chmod 600 root:root)
#  3. Устанавливает systemd unit /etc/systemd/system/cogcore-orchestrator.service
#  4. Копирует scripts/cogcore-orchestrator-daemon.py в /usr/local/bin/
#  5. systemctl daemon-reload + enable + start
#  6. Smoke test: проверяет что daemon живой через journalctl
#
# Usage (на сервере):
#   sudo bash /opt/cognitive-core/scripts/cogcore-orchestrator-setup.sh
#
# Optional env override:
#   OWNER_AGENT_ID=cognitive-core-laptop bash setup.sh
#
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────
ENV_FILE="${ENV_FILE:-/etc/cogcore-orchestrator.env}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/cogcore-orchestrator.service}"
DAEMON_INSTALL_PATH="${DAEMON_INSTALL_PATH:-/usr/local/bin/cogcore-orchestrator-daemon.py}"
DEPLOY_ENV="${DEPLOY_ENV:-/etc/cognitive-deploy.env}"
REPO_DIR="${REPO_DIR:-/opt/cognitive-core}"
# Production API доступен внешне через nginx https://mcp.me-ai.ru
# (127.0.0.1:9001 как у sub-agent был неверным предположением — там никто не слушает)
COGCORE_API_BASE="${COGCORE_API_BASE:-https://mcp.me-ai.ru}"
ORCHESTRATOR_AGENT_ID="${ORCHESTRATOR_AGENT_ID:-orchestrator}"
OWNER_AGENT_ID="${OWNER_AGENT_ID:-cognitive-core-laptop}"
OWNER_EMAIL="${OWNER_EMAIL:-mocartlex@yandex.ru}"
PG_CONTAINER="${PG_CONTAINER:-cognitive_postgres}"
PG_DB="${PG_DB:-cognitive_core}"
PG_USER="${PG_USER:-cognitive}"

# ─── Helpers ─────────────────────────────────────────────────────────────
log() { printf '[setup] %s\n' "$*" >&2; }
die() { printf '[setup ERROR] %s\n' "$*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "must run as root (sudo)"
    fi
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

# ─── Step 0: prereqs ─────────────────────────────────────────────────────
require_root
require_cmd curl
require_cmd python3
require_cmd systemctl
require_cmd docker

[[ -d "$REPO_DIR" ]] || die "missing repo at $REPO_DIR"
[[ -f "$REPO_DIR/scripts/cogcore-orchestrator-daemon.py" ]] || die "daemon script not in $REPO_DIR/scripts/"
[[ -f "$REPO_DIR/app/services/orchestrator.py" ]] || die "missing $REPO_DIR/app/services/orchestrator.py"
docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 || die "postgres container $PG_CONTAINER not reachable"

# ─── Step 1: register orchestrator agent (idempotent, via direct SQL) ───
# Прямой INSERT в agent_states + agent_keys через docker exec psql.
# Безопаснее чем HTTP /agents/register: (1) этот endpoint требует auth, у нас нет
# админ-ключа в setup, (2) обходим зависимость от docker network reachability.
EXISTING_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    EXISTING_KEY=$(grep -E '^ORCHESTRATOR_API_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
fi

if [[ -n "$EXISTING_KEY" ]]; then
    log "ORCHESTRATOR_API_KEY уже в $ENV_FILE (длина ${#EXISTING_KEY}), skip register"
    NEW_KEY=""
else
    log "Registering agent_id=$ORCHESTRATOR_AGENT_ID via direct SQL (no HTTP register endpoint)"
    OWNER_UID=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tA -c \
        "SELECT user_id FROM accounts WHERE email='$OWNER_EMAIL' LIMIT 1" | tr -d '[:space:]')
    [[ -n "$OWNER_UID" ]] || die "owner account $OWNER_EMAIL not found in accounts table"
    log "owner_user_id resolved: $OWNER_UID"
    NEW_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    [[ ${#NEW_KEY} -ge 32 ]] || die "failed to generate API key"
    docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" >/dev/null <<SQL
INSERT INTO agent_states (agent_id, status, machine_label, owner_user_id, created_at, updated_at)
VALUES ('$ORCHESTRATOR_AGENT_ID', 'active', 'server-daemon', '$OWNER_UID', NOW(), NOW())
ON CONFLICT (agent_id) DO UPDATE SET status='active', updated_at=NOW();

INSERT INTO agent_keys (api_key, agent_id, description, owner_user_id, created_at)
VALUES ('$NEW_KEY', '$ORCHESTRATOR_AGENT_ID', 'Cogcore AI orchestrator daemon', '$OWNER_UID', NOW())
ON CONFLICT (api_key) DO NOTHING;
SQL
    log "Registered orchestrator + api_key (length ${#NEW_KEY})"
fi

# ─── Step 2: load DEEPSEEK_API_KEY from deploy.env or .env ──────────────
DS_KEY=""
if [[ -f "$DEPLOY_ENV" ]]; then
    DS_KEY=$(grep -E '^DEEPSEEK_API_KEY=' "$DEPLOY_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [[ -z "$DS_KEY" && -f "$REPO_DIR/.env" ]]; then
    DS_KEY=$(grep -E '^DEEPSEEK_API_KEY=' "$REPO_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [[ -z "$DS_KEY" ]]; then
    # Также можно вытащить из работающего api контейнера
    DS_KEY=$(docker exec cognitive_api printenv DEEPSEEK_API_KEY 2>/dev/null || true)
fi
[[ -n "$DS_KEY" ]] || die "DEEPSEEK_API_KEY не найден ни в $DEPLOY_ENV, ни в $REPO_DIR/.env, ни в cognitive_api"

# ─── Step 3: write env file ────────────────────────────────────────────
log "Writing $ENV_FILE (mode 600 root:root)"
TMP_ENV=$(mktemp)
trap 'rm -f "$TMP_ENV"' EXIT
{
    echo "# Auto-generated by cogcore-orchestrator-setup.sh"
    echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "ORCHESTRATOR_AGENT_ID=$ORCHESTRATOR_AGENT_ID"
    if [[ -n "$NEW_KEY" ]]; then
        echo "ORCHESTRATOR_API_KEY=$NEW_KEY"
    else
        echo "ORCHESTRATOR_API_KEY=$EXISTING_KEY"
    fi
    echo "DEEPSEEK_API_KEY=$DS_KEY"
    echo "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1"
    echo "DEEPSEEK_MODEL=deepseek-chat"
    echo "COGCORE_API_BASE=$COGCORE_API_BASE"
    echo "ORCH_POLL_INTERVAL_S=5"
    echo "ORCH_APPROVAL_TIMEOUT_S=300"
    echo "ORCH_LOG_LEVEL=INFO"
    echo "ORCH_LOG_TO_L1=1"
    if [[ -n "$OWNER_AGENT_ID" ]]; then
        echo "OWNER_AGENT_ID=$OWNER_AGENT_ID"
    else
        echo "# OWNER_AGENT_ID=<set to your agent_id to enable approval flow>"
    fi
} >"$TMP_ENV"
install -m 600 -o root -g root "$TMP_ENV" "$ENV_FILE"
log "Env file written"

# ─── Step 4: install daemon script ─────────────────────────────────────
log "Installing daemon → $DAEMON_INSTALL_PATH"
install -m 755 -o root -g root \
    "$REPO_DIR/scripts/cogcore-orchestrator-daemon.py" \
    "$DAEMON_INSTALL_PATH"

# Verify Python dependencies (httpx должен быть установлен системно или в venv)
PYBIN="$(command -v python3)"
if ! "$PYBIN" -c "import httpx" 2>/dev/null; then
    log "httpx not found in system python — installing via apt or pip"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -qq -y python3-httpx || \
            "$PYBIN" -m pip install --break-system-packages httpx
    else
        "$PYBIN" -m pip install --break-system-packages httpx
    fi
fi
"$PYBIN" -c "import httpx; print('httpx', httpx.__version__)" || die "httpx still missing"

# ─── Step 5: systemd unit ──────────────────────────────────────────────
log "Writing $SERVICE_FILE"
cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Cogcore Orchestrator Daemon — task dispatcher between AI agents
After=network-online.target docker.service cognitive-core.service
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$REPO_DIR
ExecStart=$PYBIN $DAEMON_INSTALL_PATH
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cogcore-orchestrator

# Resource constraints — daemon должен быть лёгкий
MemoryMax=256M
CPUQuota=25%

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$SERVICE_FILE"

# ─── Step 6: enable + start ────────────────────────────────────────────
log "systemctl daemon-reload + enable + (re)start"
systemctl daemon-reload
systemctl enable cogcore-orchestrator.service >/dev/null
systemctl restart cogcore-orchestrator.service

# Wait для боота
sleep 4

# ─── Step 7: smoke test ────────────────────────────────────────────────
log "Status check"
if systemctl is-active --quiet cogcore-orchestrator.service; then
    log "OK: cogcore-orchestrator is active (running)"
else
    log "FAIL: service is not active"
    systemctl status --no-pager -l cogcore-orchestrator.service || true
    journalctl -u cogcore-orchestrator -n 30 --no-pager || true
    exit 4
fi

log "Last 10 log lines:"
journalctl -u cogcore-orchestrator -n 10 --no-pager || true

log "Setup complete!"
log ""
log "Next steps:"
log "  1. Если хочешь approval flow — задать OWNER_AGENT_ID:"
log "     sudo sed -i 's|^# OWNER_AGENT_ID=.*|OWNER_AGENT_ID=YOUR_AGENT_ID|' $ENV_FILE"
log "     sudo systemctl restart cogcore-orchestrator"
log ""
log "  2. Тест из Claude Code / MCP:"
log "     cognitive_send to:orchestrator text:'статус всех агентов'"
log ""
log "  3. Логи: journalctl -u cogcore-orchestrator -f"
