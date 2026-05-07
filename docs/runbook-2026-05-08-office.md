# Runbook 2026-05-08 — офисная сессия

Owner возвращается в офис 2026-05-08 — будет LAN-доступ к серверу через `192.168.0.118`.
Этот runbook закрывает все sudo-задачи которые накопились пока WG/Tailscale/WAN-SSH не работали.

## Контекст (что сегодня случилось)

- Tailscale-туннель обоих агентов (cognitive-core-laptop + ai-crm-deploy) упал
- WireGuard на ПК владельца не активен (нужна Windows-elevation)
- Прямой SSH через WAN на `94.181.169.239:22` — TCP-handshake проходит, но banner exchange viнет
- Причина: оба агента сидят за общим NAT-IP `207.211.215.149`, fail2ban на сервере его забанил после Tailscale-flapping
- Контейнеры все здоровые (`/health` healthy=true, uptime растёт, embedding-инициализация проходит)

## Задачи на завтра

### 1. Подключиться к серверу

```bash
# В офисной LAN
ssh -i ~/.ssh/cogcore_lan salex@192.168.0.118
# (через WAN-IP 94.181.169.239 будет работать после задачи 2)
```

### 2. Unban shared NAT IP + UFW whitelist для admin

```bash
# 2.1. Снять f2b-ban с VPN-pool IP, через который сегодня летели запросы
sudo fail2ban-client unban 207.211.215.149
sudo fail2ban-client status sshd          # проверка

# 2.2. Узнать UFW-правила для 22 — там whitelist (мы это диагностировали 2026-05-07:
#       SYN с домашнего Дом.ру дропался ДО fail2ban → UFW source-IP filter)
sudo ufw status verbose | grep -E "22|tcp"

# 2.3. Добавить admin-IP в whitelist чтобы не зависеть ни от VPN ни от f2b в будущем.
#       Узнай свой текущий внешний IP без AdGuard и подставь:
HOME_IP=$(curl -s https://api.ipify.org)   # ВАЖНО: запускать без VPN!
echo "home IP: $HOME_IP"
sudo ufw allow from "$HOME_IP" to any port 22 comment "admin home (cognitive-core-laptop owner)"

# 2.4. Если в офисе есть статический WAN — тоже добавить:
# sudo ufw allow from <office-static-IP> to any port 22 comment "office admin"
```

После этого SSH с дома и из офиса напрямую без VPN — и ai-crm-deploy / cognitive-core-laptop смогут к серверу.

### 3. AI-CRM три действия (handoff от ai-crm-deploy)

Точный список из их DM в L1 (id `2816b820`, 2026-05-07 18:40 UTC).
Полный handoff doc: `/opt/ai-crm/deploy/salex/HANDOFF_TO_COGNITIVE_CORE.md` @ commit `197e5a2`.

#### 3.1. CREATE EXTENSION в БД ai_crm

```bash
docker exec cognitive_postgres psql -U postgres -d ai_crm -c \
  "CREATE EXTENSION IF NOT EXISTS vector; \
   CREATE EXTENSION IF NOT EXISTS pg_trgm; \
   CREATE EXTENSION IF NOT EXISTS btree_gin; \
   CREATE EXTENSION IF NOT EXISTS unaccent;"
```

`IF NOT EXISTS` сделал идемпотентным.

После этого ai-crm-deploy сам добьёт оставшиеся 11 миграций через `/opt/ai-crm/deploy/salex/apply-migrations.sh`.

#### 3.2. Подключить nginx-include в cognitive_nginx

```bash
sudo ln -sf /opt/ai-crm/deploy/salex/nginx-include.conf \
  /opt/cognitive-core/nginx/conf.d/ai-crm.conf

sudo docker exec cognitive_nginx nginx -t   # обязательно перед reload
sudo docker exec cognitive_nginx nginx -s reload
```

#### 3.3. Wildcard cert через certbot DNS-01 (Cloudflare)

```bash
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /opt/ai-crm/.deploy/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d ai-salex.ru -d '*.ai-salex.ru' \
  --email mocartlex@yandex.ru \
  --agree-tos --non-interactive
```

Если certbot жалуется что плагин `dns-cloudflare` не установлен:

```bash
sudo apt install -y python3-certbot-dns-cloudflare
```

После выпуска cert — реload nginx (если он уже потребляет этот domain через include).

### 4. Уведомить ai-crm-deploy через L1

```bash
# С локальной машины (или прямо с сервера через curl)
curl -s -X POST -H "X-API-Key: 06861b566ab1c2d468d9284175051378f934968abefc5a847a627f5bea33f572" \
  -H "Content-Type: application/json" \
  https://mcp.xn----8sbwawqx4fza.xn--p1ai/agents/message \
  -d '{
    "to": "ai-crm-deploy",
    "text": "Office-session 2026-05-08 done: unban-207.211.215.149 + extensions + nginx-include + wildcard cert. Run apply-migrations.sh когда готов.",
    "context": {"runbook": "docs/runbook-2026-05-08-office.md"}
  }'
```

### 5. OOB SSH-порт 2222 (v0.5.0-prod task #9)

Чтобы не повторился сегодняшний сценарий (главный 22 забанен → нет SSH вообще), поднимаем альтернативный порт.

#### 5.1. sshd

```bash
sudo tee /etc/ssh/sshd_config.d/99-oob.conf > /dev/null <<'EOF'
Port 22
Port 2222
# Жёсткие ограничения для 2222 — только key auth, низкий MaxStartups
Match LocalPort 2222
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitRootLogin no
    MaxStartups 3:30:10
    LoginGraceTime 20
EOF

sudo sshd -t      # проверка конфига
sudo systemctl restart ssh   # или sshd, в зависимости от дистра
```

#### 5.2. fail2ban — отдельный jail для 2222 с whitelist

```bash
sudo tee -a /etc/fail2ban/jail.local > /dev/null <<'EOF'

[sshd-oob]
enabled = true
port = 2222
filter = sshd
logpath = %(sshd_log)s
maxretry = 10
bantime = 1h
# Admin-IP whitelist (домашние, мобильный hotspot — добавить по мере)
ignoreip = 127.0.0.1/8 ::1 207.211.215.149
EOF

sudo systemctl reload fail2ban
sudo fail2ban-client status sshd-oob
```

#### 5.3. UFW

```bash
sudo ufw allow 2222/tcp comment "OOB SSH emergency channel"
sudo ufw status numbered | grep 2222
```

#### 5.4. Роутер TP-LINK TL-WR842N

В админке роутера:

- Forwarding → Virtual Servers → Add new
- Service Port: `2222`, Internal Port: `2222`, IP Address: `192.168.0.118`, Protocol: `TCP`
- Save → Reboot if required

#### 5.5. Smoke-test с домашнего ПК

```bash
ssh -p 2222 -i ~/.ssh/cogcore_lan salex@94.181.169.239 "echo OK"
```

### 6. Возможно полезное pока в офисе

- Перепроверить `tailscale up` на сервере — если хочется вернуть TS-mesh (см. `tailscale_ssh_mtu_issue` memory — там MTU/userspace гипотезы)
- `git log --oneline -10` в `/opt/cognitive-core/` — убедиться что auto-deploy не отстал
- `docker ps --format 'table {{.Names}}\t{{.Status}}'` — все контейнеры running

## После runbook'а

- Запушить этот runbook + roadmap.md (с задачей #9) в `main` — auto-deploy проигнорирует docs/* и roadmap.md (не trigger ничего)
- Закрыть в L1 переписку с ai-crm-deploy финальным сообщением «всё готово, идём дальше»
- Обновить memory локально: `production_readiness` → отметить task #9 как scheduled, добавить fact про fail2ban-shared-NAT-incident
- Решить судьбу `git stash@{0}` (rejected oneshot mechanism): либо drop, либо переоформить через узкий-scope патч и закоммитить с явным consent
