# Mail-VPS: свой Postfix-relay для AImail

Что: outgoing-only SMTP-relay на Selectel VPS (Ubuntu 24.04 LTS), с DKIM + SPF + DMARC + PTR, выдаёт собственный канал отправки писем (`mail.aimail.art`).

Используется как **Фаза 1B** плана 2026-05-17 — пока Yandex SMTP работает на старте (Фаза 1A), параллельно прогреваем этот сервер, потом переключаем в `.env` cognitive-core: `EMAIL_BACKEND=postfix`.

## Что в этой папке

| Файл | Назначение |
|------|------------|
| `setup.sh` | Идемпотентный install-script. Можно запускать многократно — поправит только то, что съехало. |
| `main.cf` | Базовый Postfix-конфиг (outgoing-only, TLS, DKIM-aware) |
| `master.cf` | Включает submission на порту 587 + relay на 25 |
| `opendkim.conf` | OpenDKIM сервис-конфиг (UNIX socket для Postfix) |
| `KeyTable` | Селекторы → приватные ключи (1 на домен) |
| `SigningTable` | From-домены → селекторы |
| `mailapi.py` | Мини HTTP-API (порт 8001) — `POST /send` для приёма заявок от cognitive-core |
| `cogcore-mailapi.service` | systemd unit для mailapi |
| `requirements.txt` | Зависимости mailapi (fastapi, uvicorn) |

## Быстрый старт на свежем VPS

```bash
# 1. Подключиться по SSH
ssh root@<IP_VPS>

# 2. Залить эту папку
scp -r deploy/mail-vps root@<IP_VPS>:/tmp/mail-vps

# 3. Запустить установщик
ssh root@<IP_VPS> 'cd /tmp/mail-vps && bash setup.sh aimail.art'

# Скрипт:
#   apt update + установит postfix, opendkim, certbot, python3-fastapi, ufw
#   сгенерирует DKIM ключ (RSA 2048), напечатает DNS-запись для Porkbun
#   получит Let's Encrypt сертификат для mail.aimail.art
#   стартанёт postfix + opendkim + cogcore-mailapi
#   откроет UFW порты 22, 25, 80, 443, 587, 8001
#   создаст SMTP-юзера для cognitive-core (логин/пароль в /root/mailapi-creds.txt)
```

## DNS-записи которые нужно добавить (после setup.sh)

В Porkbun (для aimail.art):

| Тип | Имя | Значение |
|-----|-----|----------|
| A | mail | `<IP_VPS>` |
| MX | @ | `10 mail.aimail.art.` |
| TXT | @ | `v=spf1 a:mail.aimail.art mx -all` (заменить старую с redirect=yandex) |
| TXT | `default._domainkey` | `v=DKIM1; k=rsa; p=<PUBLIC_KEY>` (script выведет) |
| TXT | _dmarc | `v=DMARC1; p=quarantine; rua=mailto:postmaster@aimail.art; pct=100` |

В панели Selectel — Reverse DNS:

```
<IP_VPS>  →  mail.aimail.art.
```

## Проверка после прогрева (14 дней)

```bash
# 1. Через mail-tester.com
echo "тест" | mail -s "Test from mail.aimail.art" test-XXX@mail-tester.com
# Открыть https://www.mail-tester.com/<XXX> → ≥9/10

# 2. SPF/DKIM/DMARC через mxtoolbox
# https://mxtoolbox.com/spf.aspx?domain=aimail.art
# https://mxtoolbox.com/dkim.aspx?action=dkim:default._domainkey.aimail.art
# https://mxtoolbox.com/dmarc.aspx?domain=aimail.art

# 3. Reverse DNS
dig -x <IP_VPS> +short
# Должно вернуть: mail.aimail.art.
```

## Переключение cognitive-core на этот mail-VPS

В `/opt/cognitive-core/.env`:

```env
EMAIL_BACKEND=postfix
SMTP_HOST=mail.aimail.art
SMTP_PORT=587
SMTP_USER=cogcore@aimail.art   # из /root/mailapi-creds.txt
SMTP_PASSWORD=<пароль>
EMAIL_FROM=noreply@aimail.art
EMAIL_FROM_NAME=AImail
EMAIL_REPLY_TO=noreply@aimail.art
```

Затем `docker compose restart cognitive_api`. Тест:

```bash
curl -X POST https://mcp.xn----8sbwawqx4fza.xn--p1ai/auth/email/request \
    -H 'Content-Type: application/json' \
    -d '{"email":"test@gmail.com"}'
# 200 OK + письмо в Gmail в входящие
```

## Безопасность

- Postfix настроен **только** на исходящую отправку (`smtpd_relay_restrictions`).
- На входящие 25/587 — `permit_sasl_authenticated` + reject_unauth_destination.
- mailapi.py на порту 8001 — только internal через UFW (можно отключить port 8001 в UFW и оставить только nginx proxy).
- DKIM приватный ключ только под root, `chmod 600`.
- Let's Encrypt сертификат продлевается автоматически через `certbot.timer`.
