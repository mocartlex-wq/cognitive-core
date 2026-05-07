#!/usr/bin/env bash
# Cognitive Core - one-click installer for Linux/macOS.
# Подключение к Claude Desktop через docker exec.
#
# Usage:
#   cd /path/to/cognitive-core
#   bash installer.sh

set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

step() { echo -e "\033[36m==> $1\033[0m"; }
ok() { echo -e "    \033[32mOK: $1\033[0m"; }
err() { echo -e "    \033[31mFAIL: $1\033[0m"; }

# Step 1: Docker
step "Step 1/5: Checking Docker"
if ! command -v docker &>/dev/null; then
    err "Docker not found"
    echo "Install Docker: https://docs.docker.com/get-docker/"
    exit 1
fi
if ! docker info &>/dev/null; then
    err "Docker daemon not running"
    echo "Start Docker Desktop or 'sudo systemctl start docker' on Linux"
    exit 1
fi
ok "$(docker --version)"

# Step 2: .env
step "Step 2/5: Configuring .env"
if [ ! -f .env ]; then
    cp .env.example .env
    read -p "Enter your DeepSeek API key (sk-...) or press Enter to skip: " api_key
    if [[ "$api_key" == sk-* ]]; then
        sed -i.bak "s|DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$api_key|" .env && rm .env.bak
        ok ".env updated"
    else
        echo "    Skipped. Edit .env manually later."
    fi
else
    ok ".env already exists"
fi

# Step 3: docker compose up
step "Step 3/5: Starting stack"
docker compose up -d --build >/dev/null 2>&1
ok "Stack started"

# Step 4: Wait for healthy
step "Step 4/5: Waiting for API to be healthy"
for i in {1..30}; do
    if curl -sf http://localhost:9001/health 2>/dev/null | grep -q '"healthy":true'; then
        ok "API healthy"
        break
    fi
    sleep 2
done

# Step 5: Configure Claude Desktop
step "Step 5/5: Configuring Claude Desktop"
case "$OSTYPE" in
    darwin*)  CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    linux*)   CLAUDE_CONFIG="$HOME/.config/Claude/claude_desktop_config.json" ;;
    *)        err "Unsupported OS: $OSTYPE"; exit 1 ;;
esac

CLAUDE_DIR="$(dirname "$CLAUDE_CONFIG")"
if [ ! -d "$CLAUDE_DIR" ]; then
    err "Claude Desktop config dir not found: $CLAUDE_DIR"
    echo "Install Claude Desktop: https://claude.ai/download"
    exit 1
fi

AGENT_KEY=$(grep -oP '"\K[^"]+(?="\s*\}\s*$)' .env 2>/dev/null | head -1 || echo "key-design-001")

python3 - <<EOF
import json, os
path = "$CLAUDE_CONFIG"
config = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        try: config = json.load(f)
        except: config = {}
config.setdefault("mcpServers", {})
config["mcpServers"]["cognitive-core"] = {
    "command": "docker",
    "args": ["exec", "-i", "cognitive_api", "python", "-m", "mcp_server.server"],
    "env": {
        "COGNITIVE_API_KEY": "$AGENT_KEY",
        "COGNITIVE_AGENT_NAME": "claude_desktop",
        "CC_IN_CONTAINER": "1"
    }
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print("    OK: claude_desktop_config.json updated")
EOF

echo ""
echo -e "\033[32m========================================\033[0m"
echo -e "\033[32m  Cognitive Core installed successfully!\033[0m"
echo -e "\033[32m========================================\033[0m"
echo ""
echo "Next: Quit Claude Desktop completely and restart."
echo "Then in a new chat: 'Use cognitive_health and show system status.'"
echo ""
echo "Dashboard: http://localhost:9001/ui"
