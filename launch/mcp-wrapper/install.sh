#!/usr/bin/env bash
# cognitive-core-mcp — one-line installer (no pip publish required).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mocartlex-wq/cognitive-core/main/mcp-wrapper/install.sh | bash
#   curl -fsSL https://.../install.sh | COGCORE_URL=https://srv COGCORE_AGENT_ID=alice bash

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
SCRIPT_NAME="cognitive_core_mcp"
RAW_URL="${RAW_URL:-https://raw.githubusercontent.com/mocartlex-wq/cognitive-core/main/mcp-wrapper/cognitive_core_mcp.py}"
COGCORE_URL="${COGCORE_URL:-}"
COGCORE_AGENT_ID="${COGCORE_AGENT_ID:-}"
COGCORE_ROOM_KEY="${COGCORE_ROOM_KEY:-}"

cyan()  { printf "\033[1;36m%s\033[0m\n" "$*"; }
green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m%s\033[0m\n" "$*"; }
red()   { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }

cyan "▶ installing $SCRIPT_NAME → $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
curl -fsSL "$RAW_URL" -o "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"
green "✓ script installed"

cyan "▶ installing python deps (mcp, httpx) ..."
if command -v pipx >/dev/null 2>&1; then
  pipx install mcp httpx 2>/dev/null || pip install --user mcp httpx
else
  pip install --user --quiet mcp httpx 2>/dev/null \
    || pip install --user --break-system-packages --quiet mcp httpx
fi
green "✓ deps installed"

# Interactive prompts if not given
if [ -z "$COGCORE_URL" ]; then
  read -r -p "Cognitive Core URL [https://mcp.example.com]: " COGCORE_URL
  COGCORE_URL="${COGCORE_URL:-https://mcp.example.com}"
fi
if [ -z "$COGCORE_AGENT_ID" ]; then
  read -r -p "Your agent id (e.g. alice): " COGCORE_AGENT_ID
fi
if [ -z "$COGCORE_ROOM_KEY" ]; then
  read -r -p "Default room api_key (optional, blank to skip): " COGCORE_ROOM_KEY
fi

# Try to add to Claude Code settings
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  warn "▶ $SETTINGS exists — append manually:"
else
  mkdir -p "$HOME/.claude"
  cat > "$SETTINGS" <<EOF
{
  "mcpServers": {
    "cogcore-rooms": {
      "command": "$INSTALL_DIR/$SCRIPT_NAME",
      "env": {
        "COGCORE_URL":      "$COGCORE_URL",
        "COGCORE_AGENT_ID": "$COGCORE_AGENT_ID",
        "COGCORE_ROOM_KEY": "$COGCORE_ROOM_KEY"
      }
    }
  }
}
EOF
  green "✓ wrote $SETTINGS"
fi

cat <<DONE

══════════════════════════════════════════════════
  ✅ cognitive-core-mcp installed
══════════════════════════════════════════════════
  Binary:        $INSTALL_DIR/$SCRIPT_NAME
  Settings:      $SETTINGS

  Snippet to merge into existing settings.json:

    {
      "mcpServers": {
        "cogcore-rooms": {
          "command": "$INSTALL_DIR/$SCRIPT_NAME",
          "env": {
            "COGCORE_URL":      "$COGCORE_URL",
            "COGCORE_AGENT_ID": "$COGCORE_AGENT_ID",
            "COGCORE_ROOM_KEY": "$COGCORE_ROOM_KEY"
          }
        }
      }
    }

  Restart Claude Code, then ask: "list cognitive_room_* tools"
══════════════════════════════════════════════════
DONE

# Make sure ~/.local/bin is on PATH
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) warn "⚠  $INSTALL_DIR is not on \$PATH — add to your shell rc:" ;
     warn "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
