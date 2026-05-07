#!/usr/bin/env bash
# Cognitive Core — Linux server installer (Ubuntu 22.04 / 24.04).
#
# One command on a fresh Ubuntu server:
#   curl -fsSL https://your-host/install-server.sh | bash
# Or:
#   git clone <repo> /opt/cognitive-core && cd /opt/cognitive-core && bash install-server.sh
#
# Что делает:
#   1. apt install Docker, Docker Compose, openssl, curl
#   2. clone repo (если ещё нет)
#   3. gen-secrets → .env (с интерактивным DeepSeek key)
#   4. setup-tls self-signed (или letsencrypt если задан домен)
#   5. UFW firewall: allow 22, 80, 443
#   6. docker compose -f ... -f docker-compose.prod.yml up -d --build
#   7. systemd unit для auto-start на boot
#   8. Wait for healthy
#   9. Print connection info

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/cognitive-core}"
REPO_URL="${REPO_URL:-}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"

step() { echo -e "\033[36m==> $1\033[0m"; }
ok() { echo -e "    \033[32mOK: $1\033[0m"; }
warn() { echo -e "    \033[33m!  $1\033[0m"; }
err() { echo -e "    \033[31mFAIL: $1\033[0m"; }

if [ "$EUID" -ne 0 ] && ! groups | grep -q docker; then
    warn "Not root and not in docker group. Some steps may need sudo."
fi

# === Step 1: install dependencies ===
step "1/9 Installing system dependencies"
sudo apt update -qq
sudo apt install -y -qq curl git openssl ca-certificates ufw 2>/dev/null

if ! command -v docker &>/dev/null; then
    echo "    Installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
    ok "Docker installed (re-login if you want to use docker without sudo)"
else
    ok "Docker already installed: $(docker --version)"
fi

if ! docker compose version &>/dev/null; then
    sudo apt install -y -qq docker-compose-plugin
fi

# === Step 2: project directory ===
step "2/9 Project directory"
if [ ! -d "$PROJECT_DIR" ]; then
    if [ -n "$REPO_URL" ]; then
        sudo git clone "$REPO_URL" "$PROJECT_DIR"
        sudo chown -R "$USER:$USER" "$PROJECT_DIR"
    else
        err "No project at $PROJECT_DIR and REPO_URL not set"
        echo "Set: export REPO_URL=https://github.com/youruser/cognitive-core.git"
        exit 1
    fi
fi
cd "$PROJECT_DIR"
ok "$PROJECT_DIR"

# === Step 3: secrets ===
step "3/9 Production secrets"
if [ ! -f .env ]; then
    read -rp "    DeepSeek API key (sk-..., empty to skip): " DS_KEY
    DEEPSEEK_API_KEY="$DS_KEY" bash scripts/gen-secrets.sh > .env
    chmod 600 .env
    ok ".env generated and chmod 600"
else
    warn ".env already exists, keeping"
fi

# === Step 4: TLS ===
step "4/9 TLS certificate"
if [ -f nginx/certs/server.crt ]; then
    warn "Cert already exists at nginx/certs/server.crt"
elif [ -n "$DOMAIN" ] && [ -n "$EMAIL" ]; then
    bash scripts/setup-tls.sh letsencrypt "$DOMAIN" "$EMAIL"
else
    bash scripts/setup-tls.sh self-signed
    ok "Self-signed cert (set DOMAIN + EMAIL env to use Let's Encrypt)"
fi

# === Step 5: firewall ===
step "5/9 UFW firewall"
sudo ufw allow 22/tcp 2>/dev/null || true
sudo ufw allow 80/tcp 2>/dev/null || true
sudo ufw allow 443/tcp 2>/dev/null || true
sudo ufw --force enable 2>/dev/null || true
ok "UFW: 22, 80, 443 open"

# === Step 6: docker compose up (prod overlay) ===
step "6/9 Starting Docker stack with prod overlay"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
ok "Stack started"

# === Step 7: systemd unit ===
step "7/9 Systemd auto-start on boot"
SYSTEMD_UNIT=/etc/systemd/system/cognitive-core.service
sudo tee "$SYSTEMD_UNIT" >/dev/null <<EOF
[Unit]
Description=Cognitive Core
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml stop
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable cognitive-core.service
ok "systemd unit installed (will auto-start on boot)"

# === Step 8: wait for healthy ===
step "8/9 Waiting for API to be healthy"
for i in {1..60}; do
    if curl -sf -k https://localhost/health 2>/dev/null | grep -q '"healthy":true'; then
        ok "API healthy via HTTPS"
        break
    fi
    if curl -sf http://localhost:9001/health 2>/dev/null | grep -q '"healthy":true' 2>/dev/null; then
        warn "API healthy via plain HTTP (TLS not active yet)"
        break
    fi
    sleep 3
done

# === Step 9: print info ===
step "9/9 Done"
echo ""
echo "========================================"
echo "  Cognitive Core deployed!"
echo "========================================"
echo ""
echo "Endpoints (pick which one matches your TLS setup):"
if [ -n "$DOMAIN" ]; then
    echo "  https://$DOMAIN/         — Главная"
    echo "  https://$DOMAIN/ui       — Dashboard"
    echo "  https://$DOMAIN/health   — Health JSON"
else
    SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<server-ip>")
    echo "  https://$SERVER_IP/      — main (self-signed cert, ignore browser warning)"
fi
echo ""
echo "Agent API keys:"
grep "^AGENT_API_KEYS=" .env | sed 's/AGENT_API_KEYS=//'
echo ""
echo "For client (Cherry Studio, Claude Desktop) connection:"
echo "  Use the agent key as X-API-Key header."
echo "  Use HTTP/SSE MCP transport against https://<your-host>/mcp/sse"
echo "  (See AGENT_GUIDE.md → Remote MCP setup)"
echo ""
echo "Logs:    docker compose logs -f --tail 50"
echo "Status:  docker compose ps"
echo "Backup:  /opt/cognitive-core/backups/postgres/"
echo ""
