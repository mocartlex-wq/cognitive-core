#!/usr/bin/env bash
# Cognitive Core — TLS setup script.
# Создаёт self-signed cert для тестов или настраивает Let's Encrypt при наличии домена.
#
# Usage:
#   bash scripts/setup-tls.sh self-signed
#   bash scripts/setup-tls.sh letsencrypt cognitive.example.com [email protected]

set -eu

MODE="${1:-self-signed}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="$PROJECT_ROOT/nginx/certs"
mkdir -p "$CERT_DIR"

case "$MODE" in
    self-signed)
        echo "Generating self-signed certificate for testing..."
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
            -keyout "$CERT_DIR/server.key" \
            -out "$CERT_DIR/server.crt" \
            -subj "/CN=cognitive.local/O=Cognitive Core/C=US" \
            -addext "subjectAltName=DNS:cognitive.local,DNS:localhost,IP:127.0.0.1"
        chmod 600 "$CERT_DIR/server.key"
        chmod 644 "$CERT_DIR/server.crt"
        echo ""
        echo "OK: self-signed cert at $CERT_DIR/"
        echo "  Valid for: 365 days"
        echo "  CN: cognitive.local"
        echo ""
        echo "Add to /etc/hosts (or C:\\Windows\\System32\\drivers\\etc\\hosts):"
        echo "  127.0.0.1  cognitive.local"
        echo ""
        echo "Then test: curl -k https://cognitive.local/health"
        ;;

    letsencrypt)
        DOMAIN="${2:-}"
        EMAIL="${3:-}"
        if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
            echo "Usage: bash scripts/setup-tls.sh letsencrypt <domain> <email>"
            exit 1
        fi
        if ! command -v certbot &>/dev/null; then
            echo "Installing certbot..."
            sudo apt update && sudo apt install -y certbot python3-certbot-nginx
        fi
        echo "Obtaining Let's Encrypt cert for $DOMAIN..."
        sudo certbot certonly --standalone --preferred-challenges http \
            -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive
        # Symlink certs into nginx/certs
        sudo cp "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$CERT_DIR/server.crt"
        sudo cp "/etc/letsencrypt/live/$DOMAIN/privkey.pem" "$CERT_DIR/server.key"
        sudo chmod 644 "$CERT_DIR/server.crt"
        sudo chmod 600 "$CERT_DIR/server.key"
        echo "OK: Let's Encrypt cert installed for $DOMAIN"
        echo "Auto-renewal: systemctl status certbot.timer"
        ;;

    *)
        echo "Unknown mode: $MODE"
        echo "Usage: bash scripts/setup-tls.sh [self-signed|letsencrypt domain email]"
        exit 1
        ;;
esac
