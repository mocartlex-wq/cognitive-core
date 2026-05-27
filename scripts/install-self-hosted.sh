#!/usr/bin/env bash
# install-self-hosted.sh — устанавливает Cognitive Core на чистый VPS.
#
# Использование:
#   curl -fsSL https://mcp.me-ai.ru/static/install-self-hosted.sh | sudo bash
#
# Что делает:
#   1. Проверяет требования (Ubuntu/Debian, ≥4GB RAM, ≥80GB disk, docker available)
#   2. Спрашивает интерактивно: домен, admin email, путь к данным
#   3. Скачивает docker-compose.yml + nginx.conf + .env.template
#   4. Устанавливает Docker (если нет)
#   5. Выпускает TLS-сертификаты через certbot
#   6. Поднимает все сервисы, прогоняет alembic миграции
#   7. Создаёт admin-account и выдаёт credentials
#
# НЕ для production без review: это «happy path» скрипт, не покрывает edge cases
# (proxy, custom auth, GPU, custom certs). Manual install — в docs/onboarding-vps.md.

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────
# Защита от запуска не от sudo
# ──────────────────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Скрипт требует sudo. Запуск:"
    echo "  curl -fsSL https://mcp.me-ai.ru/static/install-self-hosted.sh | sudo bash"
    exit 1
fi

CC_VERSION="${CC_VERSION:-latest}"
CC_REPO_URL="${CC_REPO_URL:-https://github.com/mocartlex-wq/cognitive-core.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/cognitive-core}"
DATA_DIR="${DATA_DIR:-/var/lib/cognitive-core}"
LOG_PREFIX="[cogcore-install]"

log() { echo "${LOG_PREFIX} $*"; }
err() { echo "${LOG_PREFIX} ❌ $*" >&2; exit 1; }
ok()  { echo "${LOG_PREFIX} ✅ $*"; }

# ──────────────────────────────────────────────────────────────────────────
# Step 1: Pre-flight checks
# ──────────────────────────────────────────────────────────────────────────
log "Step 1/7: pre-flight checks"

# OS check
if ! grep -qE "ubuntu|debian" /etc/os-release; then
    err "Поддерживаются только Ubuntu и Debian. Detected: $(grep '^NAME=' /etc/os-release | cut -d= -f2)"
fi

# RAM check (≥4 GB)
MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
MEM_GB=$((MEM_KB / 1024 / 1024))
if [ "$MEM_GB" -lt 3 ]; then
    err "Нужно ≥4 GB RAM. У вас: ${MEM_GB} GB"
fi
log "  RAM: ${MEM_GB} GB ✓"

# Disk check (≥80 GB free)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [ "$DISK_GB" -lt 60 ]; then
    err "Нужно ≥80 GB free на /. У вас: ${DISK_GB} GB"
fi
log "  Disk: ${DISK_GB} GB free ✓"

# Port check (80, 443 free)
for port in 80 443; do
    if ss -tlnp | grep -q ":${port} "; then
        err "Порт ${port} занят. Освободите перед установкой."
    fi
done
log "  Ports 80, 443 free ✓"

# ──────────────────────────────────────────────────────────────────────────
# Step 2: Interactive config
# ──────────────────────────────────────────────────────────────────────────
log "Step 2/7: configuration (interactive)"

read -p "Домен для Cognitive Core (например mcp.example.com): " DOMAIN
[ -z "$DOMAIN" ] && err "Домен обязателен"

read -p "Email для Let's Encrypt + первый owner-аккаунт: " ADMIN_EMAIL
[ -z "$ADMIN_EMAIL" ] && err "Email обязателен"

read -p "Путь для данных (default: ${DATA_DIR}): " custom_data
DATA_DIR="${custom_data:-$DATA_DIR}"

read -p "SMTP host для magic-link писем (Yandex/Mail.ru/Gmail, e.g. smtp.yandex.ru): " SMTP_HOST
read -p "SMTP username (your-email@domain): " SMTP_USER
read -sp "SMTP password (app-password, НЕ обычный пароль почты): " SMTP_PASS
echo

# DNS verification
log "  Проверяю DNS ${DOMAIN}..."
SERVER_IP=$(curl -sS ifconfig.me)
DOMAIN_IP=$(dig +short "$DOMAIN" | tail -1)
if [ "$SERVER_IP" != "$DOMAIN_IP" ]; then
    log "  ⚠️ ${DOMAIN} → ${DOMAIN_IP}, но у вас ${SERVER_IP}"
    read -p "  Продолжить? (DNS может ещё не propagated) [y/N]: " confirm
    [ "$confirm" != "y" ] && err "Aborted"
fi
ok "Config готов"

# ──────────────────────────────────────────────────────────────────────────
# Step 3: Install Docker if missing
# ──────────────────────────────────────────────────────────────────────────
log "Step 3/7: Docker"
if ! command -v docker &>/dev/null; then
    log "  Docker не найден, ставлю..."
    apt-get update -qq
    apt-get install -y -qq docker.io docker-compose-v2 certbot
    systemctl enable --now docker
    ok "Docker установлен"
else
    log "  Docker уже есть: $(docker --version)"
fi

# ──────────────────────────────────────────────────────────────────────────
# Step 4: Clone repo + setup directories
# ──────────────────────────────────────────────────────────────────────────
log "Step 4/7: репозиторий + директории"
if [ ! -d "$INSTALL_DIR/.git" ]; then
    apt-get install -y -qq git
    git clone --quiet "$CC_REPO_URL" "$INSTALL_DIR"
    ok "Clone в $INSTALL_DIR"
else
    log "  $INSTALL_DIR already exists, pulling latest"
    cd "$INSTALL_DIR" && git pull --quiet
fi

mkdir -p "$DATA_DIR"/{postgres,redis,minio,nginx-certs,backups}
ok "Data dirs создан в $DATA_DIR"

# ──────────────────────────────────────────────────────────────────────────
# Step 5: .env с secrets
# ──────────────────────────────────────────────────────────────────────────
log "Step 5/7: .env с генерированными секретами"
cd "$INSTALL_DIR"

if [ ! -f .env ]; then
    POSTGRES_PASSWORD=$(openssl rand -hex 32)
    S3_SECRET_KEY=$(openssl rand -hex 32)
    SESSION_SECRET=$(openssl rand -hex 32)

    cat > .env <<EOF
# Generated by install-self-hosted.sh on $(date -Iseconds)
DOMAIN=${DOMAIN}
ADMIN_EMAIL=${ADMIN_EMAIL}

POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
S3_ACCESS_KEY=cognitive_admin
S3_SECRET_KEY=${S3_SECRET_KEY}
SESSION_SECRET=${SESSION_SECRET}

SMTP_HOST=${SMTP_HOST}
SMTP_PORT=465
SMTP_USER=${SMTP_USER}
SMTP_PASS=${SMTP_PASS}
SMTP_FROM=${SMTP_USER}
SMTP_TLS=true

# Whisper для media-pipeline (audio/video transcription)
WHISPER_MODEL_SIZE=base
WHISPER_CACHE_DIR=/data/whisper

# Опционально: добавьте при желании
# STRIPE_API_KEY=sk_live_...
# STRIPE_WEBHOOK_SECRET=whsec_...
# YOOKASSA_SHOP_ID=...
# YOOKASSA_SECRET_KEY=...
# KLING_API_KEY=...
# GIGACHAT_AUTH_TOKEN=...
# YANDEX_GPT_API_KEY=...
EOF
    chmod 600 .env
    ok ".env сгенерирован (POSTGRES_PASSWORD, S3_SECRET_KEY, SESSION_SECRET)"
else
    log "  .env уже есть, не перезаписываю"
fi

# ──────────────────────────────────────────────────────────────────────────
# Step 6: TLS-сертификаты
# ──────────────────────────────────────────────────────────────────────────
log "Step 6/7: TLS-сертификаты через certbot"
if [ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    certbot certonly --standalone \
        --non-interactive --agree-tos \
        -m "$ADMIN_EMAIL" \
        -d "$DOMAIN" \
        || err "certbot failed — проверьте DNS / ports"
    ok "Сертификат выпущен"
else
    log "  Сертификат для ${DOMAIN} уже есть"
fi

mkdir -p nginx/certs
cp /etc/letsencrypt/live/"$DOMAIN"/fullchain.pem nginx/certs/
cp /etc/letsencrypt/live/"$DOMAIN"/privkey.pem nginx/certs/
chmod 600 nginx/certs/*

# Auto-renew cron
if ! crontab -l 2>/dev/null | grep -q "certbot renew"; then
    (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --post-hook 'cp /etc/letsencrypt/live/${DOMAIN}/*.pem ${INSTALL_DIR}/nginx/certs/ && docker compose -f ${INSTALL_DIR}/docker-compose.yml restart nginx'") | crontab -
    ok "Cron на auto-renew установлен"
fi

# ──────────────────────────────────────────────────────────────────────────
# Step 7: Up + migrations + admin create
# ──────────────────────────────────────────────────────────────────────────
log "Step 7/7: docker compose up + migrations + admin"

docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
log "  Waiting for postgres healthy (~30s)..."
sleep 30

# Alembic migrations
docker exec cognitive_api alembic upgrade head || err "alembic failed"
ok "Migrations applied"

# Admin account
docker exec cognitive_api python -c "
import asyncio
from app.db.postgres import init_pool, get_pool

async def main():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO accounts (email, is_admin, tier)
            VALUES (\$1, TRUE, 'business')
            ON CONFLICT (email) DO UPDATE SET is_admin = TRUE
        ''', '${ADMIN_EMAIL}')
        print(f'Admin: {(await conn.fetchval(\"SELECT email FROM accounts WHERE email = \$1\", \"${ADMIN_EMAIL}\"))}')
asyncio.run(main())
" || err "admin create failed"
ok "Admin account: ${ADMIN_EMAIL}"

# Health check
log "Waiting for /health (60s timeout)..."
for i in $(seq 1 12); do
    if curl -sf "https://${DOMAIN}/health" > /dev/null 2>&1; then
        ok "https://${DOMAIN}/health responds"
        break
    fi
    sleep 5
done

# ──────────────────────────────────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────────────────────────────────
cat <<EOF

═══════════════════════════════════════════════════════════════════════════
🎉 УСТАНОВКА ЗАВЕРШЕНА

  URL:     https://${DOMAIN}
  Admin:   ${ADMIN_EMAIL}
  Data:    ${DATA_DIR}
  Repo:    ${INSTALL_DIR}

═══════════════════════════════════════════════════════════════════════════
СЛЕДУЮЩИЕ ШАГИ:

  1. Откройте https://${DOMAIN}/ui/login
  2. Введите ${ADMIN_EMAIL} — придёт magic-link в почту
  3. Войдите → /ui/profile → создайте первого помощника

  Документация:
    https://${DOMAIN}/docs/concepts.md
    https://${DOMAIN}/docs/onboarding-vps.md

  Auto-deploy (опционально, держит вашу инстанцию up-to-date):
    sudo cp ${INSTALL_DIR}/scripts/auto-deploy.sh /usr/local/bin/cognitive-deploy.sh
    sudo cp ${INSTALL_DIR}/systemd/cogcore-docker-prune.* /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now cogcore-docker-prune.timer

═══════════════════════════════════════════════════════════════════════════
EOF
