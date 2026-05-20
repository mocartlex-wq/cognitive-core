#!/usr/bin/env bash
# Идемпотентный установщик cogcore-mailapi + Postfix + OpenDKIM + Let's Encrypt
# для свежего Ubuntu 24.04 LTS VPS.
#
# Использование:
#     sudo bash setup.sh <DOMAIN>            # например: aimail.art
#     sudo bash setup.sh <DOMAIN> --no-cert  # пропустить certbot (если cert уже есть)
#
# Скрипт можно запускать многократно — он поправит только то, что съехало.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите от root: sudo bash setup.sh ..."
    exit 1
fi

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
    echo "Использование: $0 <DOMAIN> [--no-cert]"
    echo "  Пример: $0 aimail.art"
    exit 1
fi

HOSTNAME="mail.${DOMAIN}"
NO_CERT=false
if [[ "${2:-}" == "--no-cert" ]]; then
    NO_CERT=true
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo ">>> Setup cogcore mail-VPS для домена: $DOMAIN (hostname: $HOSTNAME)"
echo ">>> Файлы конфигурации читаются из: $SCRIPT_DIR"

# ─────────────────────────────────────────────────────────────────────────
# 1. APT — пакеты
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [1/9] Обновление пакетов и установка зависимостей..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
# Postfix задаёт интерактивные вопросы — предзаполняем
debconf-set-selections <<< "postfix postfix/mailname string ${HOSTNAME}"
debconf-set-selections <<< "postfix postfix/main_mailer_type string 'Internet Site'"
apt-get install -y \
    postfix postfix-pcre \
    opendkim opendkim-tools \
    dovecot-core dovecot-imapd \
    certbot \
    ufw \
    python3 python3-venv python3-pip \
    mailutils \
    rsyslog logrotate

# ─────────────────────────────────────────────────────────────────────────
# 2. UFW — firewall
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [2/9] Настройка firewall (UFW)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw allow 25/tcp comment 'smtp out'
ufw allow 80/tcp comment 'http for certbot'
ufw allow 443/tcp comment 'https'
ufw allow 587/tcp comment 'smtp submission'
# 8001 — mailapi только для localhost; ufw на нём не нужен, но если из cognitive-core
# хочется ходить напрямую — open только для определённого IP:
# ufw allow from <COGCORE_IP> to any port 8001 proto tcp
ufw --force enable

# ─────────────────────────────────────────────────────────────────────────
# 3. /etc/hostname + /etc/hosts
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [3/9] Установка hostname в $HOSTNAME..."
hostnamectl set-hostname "$HOSTNAME"
if ! grep -q "$HOSTNAME" /etc/hosts; then
    PUBLIC_IP=$(hostname -I | awk '{print $1}')
    echo "${PUBLIC_IP} ${HOSTNAME} mail" >> /etc/hosts
fi

# ─────────────────────────────────────────────────────────────────────────
# 4. Let's Encrypt (можно пропустить флагом)
# ─────────────────────────────────────────────────────────────────────────
if [[ "$NO_CERT" == false ]]; then
    echo ">>> [4/9] Запрос Let's Encrypt сертификата для $HOSTNAME..."
    if [[ ! -d "/etc/letsencrypt/live/$HOSTNAME" ]]; then
        # standalone режим — нужен свободный порт 80
        systemctl stop nginx 2>/dev/null || true
        certbot certonly --standalone --non-interactive --agree-tos \
            --email "postmaster@${DOMAIN}" -d "$HOSTNAME"
        systemctl start nginx 2>/dev/null || true
    else
        echo "    Сертификат уже существует, пропуск."
    fi
else
    echo ">>> [4/9] Пропуск certbot (--no-cert)"
fi

# ─────────────────────────────────────────────────────────────────────────
# 5. Postfix — main.cf + master.cf
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [5/9] Установка Postfix конфига..."
sed -e "s|__DOMAIN__|${DOMAIN}|g" -e "s|__HOSTNAME__|${HOSTNAME}|g" \
    "$SCRIPT_DIR/main.cf" > /etc/postfix/main.cf
cp "$SCRIPT_DIR/master.cf" /etc/postfix/master.cf

# Создать opendkim socket-dir
mkdir -p /var/spool/postfix/opendkim
chown opendkim:postfix /var/spool/postfix/opendkim
chmod 750 /var/spool/postfix/opendkim
usermod -aG opendkim postfix || true

systemctl enable --now postfix
systemctl restart postfix

# ─────────────────────────────────────────────────────────────────────────
# 6. OpenDKIM — конфиг + ключ
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [6/9] Установка OpenDKIM и генерация ключа (если нужно)..."
mkdir -p "/etc/opendkim/keys/$DOMAIN"

if [[ ! -f "/etc/opendkim/keys/$DOMAIN/default.private" ]]; then
    cd "/etc/opendkim/keys/$DOMAIN"
    opendkim-genkey -b 2048 -d "$DOMAIN" -s default
    chown opendkim:opendkim default.private default.txt
    chmod 600 default.private
    cd -
else
    echo "    Ключ уже существует, пропуск генерации."
fi

# Подставить domain в опен-длайн конфиги
sed "s|__DOMAIN__|${DOMAIN}|g" "$SCRIPT_DIR/opendkim.conf" > /etc/opendkim.conf
sed "s|__DOMAIN__|${DOMAIN}|g" "$SCRIPT_DIR/KeyTable" > /etc/opendkim/KeyTable
sed "s|__DOMAIN__|${DOMAIN}|g" "$SCRIPT_DIR/SigningTable" > /etc/opendkim/SigningTable
sed -e "s|__DOMAIN__|${DOMAIN}|g" -e "s|__HOSTNAME__|${HOSTNAME}|g" \
    "$SCRIPT_DIR/TrustedHosts" > /etc/opendkim/TrustedHosts

systemctl enable --now opendkim
systemctl restart opendkim
systemctl restart postfix

# ─────────────────────────────────────────────────────────────────────────
# 7. Dovecot (для SASL auth на submission 587)
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [7/9] Настройка Dovecot SASL..."
mkdir -p /etc/dovecot/conf.d
cat > /etc/dovecot/conf.d/10-master.conf <<'EOF'
service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0660
    user = postfix
    group = postfix
  }
}
EOF

cat > /etc/dovecot/conf.d/10-auth.conf <<'EOF'
disable_plaintext_auth = no
auth_mechanisms = plain login
passdb {
  driver = passwd-file
  args = scheme=SHA512-CRYPT username_format=%u /etc/dovecot/users
}
userdb {
  driver = static
  args = uid=vmail gid=vmail home=/var/mail/%u
}
EOF

# Создать SMTP-юзера для cognitive-core если ещё нет
if [[ ! -f /root/mailapi-creds.txt ]]; then
    SMTP_USER="cogcore@${DOMAIN}"
    SMTP_PASS=$(openssl rand -base64 24)
    SMTP_HASH=$(doveadm pw -s SHA512-CRYPT -p "$SMTP_PASS")
    echo "${SMTP_USER}:${SMTP_HASH}" > /etc/dovecot/users
    chmod 600 /etc/dovecot/users
    cat > /root/mailapi-creds.txt <<EOF
# SMTP credentials для cognitive-core .env
SMTP_HOST=${HOSTNAME}
SMTP_PORT=587
SMTP_USER=${SMTP_USER}
SMTP_PASSWORD=${SMTP_PASS}
EMAIL_FROM=noreply@${DOMAIN}
EMAIL_FROM_NAME=AImail
EMAIL_REPLY_TO=noreply@${DOMAIN}
EMAIL_BACKEND=postfix
EOF
    chmod 600 /root/mailapi-creds.txt
    echo ">>> Created SMTP user. Credentials в /root/mailapi-creds.txt"
fi

# Создать vmail user если нет
id vmail >/dev/null 2>&1 || useradd -r -d /var/mail -s /usr/sbin/nologin vmail

systemctl enable --now dovecot
systemctl restart dovecot

# ─────────────────────────────────────────────────────────────────────────
# 8. cogcore-mailapi (HTTP-обёртка) — опционально
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [8/9] Установка cogcore-mailapi..."
id cogcore-mailapi >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -m -d /opt/cogcore-mailapi cogcore-mailapi
mkdir -p /opt/cogcore-mailapi
cp "$SCRIPT_DIR/mailapi.py" /opt/cogcore-mailapi/mailapi.py
cp "$SCRIPT_DIR/requirements.txt" /opt/cogcore-mailapi/requirements.txt
chown -R cogcore-mailapi:cogcore-mailapi /opt/cogcore-mailapi

# venv (один раз)
if [[ ! -d /opt/cogcore-mailapi/venv ]]; then
    sudo -u cogcore-mailapi python3 -m venv /opt/cogcore-mailapi/venv
    sudo -u cogcore-mailapi /opt/cogcore-mailapi/venv/bin/pip install --upgrade pip
    sudo -u cogcore-mailapi /opt/cogcore-mailapi/venv/bin/pip install -r /opt/cogcore-mailapi/requirements.txt
fi

# env-файл с api-key (один раз)
if [[ ! -f /etc/cogcore-mailapi.env ]]; then
    API_KEY=$(openssl rand -base64 32)
    cat > /etc/cogcore-mailapi.env <<EOF
MAILAPI_KEY=${API_KEY}
MAILAPI_FROM=noreply@${DOMAIN}
MAILAPI_FROM_NAME=AImail
MAILAPI_RATE_LIMIT_PER_HOUR=1000
EOF
    chmod 600 /etc/cogcore-mailapi.env
    echo "MAILAPI_KEY сохранён в /etc/cogcore-mailapi.env"
fi

# Лог-файл
touch /var/log/cogcore-mailapi.log
chown cogcore-mailapi:cogcore-mailapi /var/log/cogcore-mailapi.log

# Systemd unit
cp "$SCRIPT_DIR/cogcore-mailapi.service" /etc/systemd/system/cogcore-mailapi.service
systemctl daemon-reload
systemctl enable --now cogcore-mailapi
systemctl restart cogcore-mailapi

# ─────────────────────────────────────────────────────────────────────────
# 9. Тестовый прогон + вывод DNS-записей
# ─────────────────────────────────────────────────────────────────────────
echo ">>> [9/9] Готово! Проверка..."
sleep 2
systemctl is-active postfix opendkim dovecot cogcore-mailapi || true

echo ""
echo "=========================================================================="
echo "DNS-записи для добавления в Porkbun (для домена $DOMAIN):"
echo "=========================================================================="
echo ""
echo "A     mail                $(hostname -I | awk '{print $1}')"
echo "MX    @  10               ${HOSTNAME}."
echo "TXT   @                   \"v=spf1 a:${HOSTNAME} mx -all\""
echo ""
echo "TXT   default._domainkey  <see below>"
echo ""
cat "/etc/opendkim/keys/$DOMAIN/default.txt"
echo ""
echo "TXT   _dmarc              \"v=DMARC1; p=quarantine; rua=mailto:postmaster@${DOMAIN}; pct=100\""
echo ""
echo "В панели Selectel — Reverse DNS:"
echo "    $(hostname -I | awk '{print $1}')  →  ${HOSTNAME}."
echo ""
echo "=========================================================================="
echo "SMTP credentials для cognitive-core /opt/cognitive-core/.env:"
echo "=========================================================================="
cat /root/mailapi-creds.txt
echo ""
echo "=========================================================================="
echo "mailapi (HTTP-канал) — api key:"
echo "=========================================================================="
grep MAILAPI_KEY /etc/cogcore-mailapi.env
echo ""
echo ">>> Тест: на mail-tester.com отправьте тестовое письмо после добавления DNS"
echo ">>> Прогрев: 10-20 писем/день первую неделю, 50-100 вторую, 200+ третью"
echo ">>> Через 14-21 день переключайте EMAIL_BACKEND=postfix в cognitive-core"
