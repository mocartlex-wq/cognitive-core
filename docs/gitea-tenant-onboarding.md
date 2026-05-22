# Gitea — self-hosted git для cognitive-core (Phase 5A)

## Что это и зачем

`git.ии-память.рф` — встроенный git-сервер на mcp-инстансе. Каждый владелец аккаунта (включая платных tenants) получает свой organization для хранения кода+configs+LFS на сервере, не на ноутбуке. Это:

- **Backup** — ноутбук сломался / другой ПК → `git clone https://git.ии-память.рф/<твой-org>/<repo>`
- **Изоляция** — private repos by default, другой tenant физически не видит
- **Single sign-on (будущее)** — пока отдельный Gitea-пароль, потом SSO с mcp-аккаунтом

## Day 1 Deployment Runbook (для admin / owner)

Шаги выполнить **в порядке**. Если что-то падает — STOP и пингуй разработчика.

### 1. DNS A-record (вне сервера)
Зайти в DNS-панель регистратора `ии-память.рф` (Yandex / другой):
- Добавить A-record: `git.ии-память.рф` → IP сервера (текущий: `94.181.169.239`)
- TTL: 300 (5 мин) на старт, потом увеличить до 3600
- **Проверка**: `dig +short git.xn----8sbwawqx4fza.xn--p1ai` через ~5 мин должно вернуть IP

### 2. TLS cert (на сервере)
```bash
ssh salex@server
sudo certbot certonly --webroot \
    -w /var/www/certbot \
    -d git.xn----8sbwawqx4fza.xn--p1ai \
    --email admin@ии-память.рф \
    --agree-tos --no-eff-email
# Должен сказать "Successfully received certificate"
# Cert lives in /etc/letsencrypt/live/git.xn----8sbwawqx4fza.xn--p1ai/
```

Auto-renewal уже работает через `certbot.timer` (общий со всеми остальными доменами на сервере).

### 3. Создать DB для Gitea
```bash
sudo docker exec cognitive_postgres psql -U cognitive -d postgres \
    -c "CREATE DATABASE gitea_db OWNER cognitive"
# Если уже существует — ERROR: database "gitea_db" already exists. Это OK.
```

### 4. Generate SECRET_KEY + INTERNAL_TOKEN (один раз)
```bash
# Сгенерировать random 64-char keys
SECRET=$(openssl rand -hex 32)
TOKEN=$(openssl rand -hex 32)

# Добавить в /etc/cognitive-deploy.env (вместе с POSTGRES_PASSWORD и т.д.)
sudo bash -c "echo 'GITEA_SECRET_KEY=$SECRET' >> /etc/cognitive-deploy.env"
sudo bash -c "echo 'GITEA_INTERNAL_TOKEN=$TOKEN' >> /etc/cognitive-deploy.env"
```

### 5. Запустить Gitea контейнер
```bash
cd /opt/cognitive-core
# git pull уже подтянул новый docker-compose.prod.yml через auto-deploy
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d gitea
# Wait healthy
sleep 30 && sudo docker compose ps gitea
# Status должен быть "healthy" (Gitea на старте мигрирует свои таблицы в gitea_db)
```

### 6. Reload nginx (подхватит conf.d/gitea.conf)
```bash
sudo docker exec cognitive_nginx nginx -t  # validate
sudo docker exec cognitive_nginx nginx -s reload
```

### 7. Smoke-test
```bash
curl -sS -o /dev/null -w "git landing: %{http_code}\n" \
    https://git.xn----8sbwawqx4fza.xn--p1ai
# Expected: HTTP 200 — Gitea login page
```

### 8. Initial admin setup (через UI)
1. Открыть в браузере `https://git.ии-память.рф`
2. Нажать «Register» (первая регистрация = admin)
3. Username: `admin`, email: `admin@ии-память.рф`, пароль (запомнить!)
4. После создания — admin-flag присваивается автоматически

### 9. Configure: отключить open registration
- Admin → `Site Administration` → `Configuration` → ensure `DISABLE_REGISTRATION = true`
- Уже стоит в env, но проверить

### 10. Создать твой org + import существующий repo
```bash
# В UI: New → Organization → name=mocartlex (или твой)
# Затем New Repository → name=cogcore-repo, private, init empty
```

Локально на ноуте:
```bash
cd ~/cogcore-work/cogcore-repo
git remote add gitea https://git.ии-память.рф/mocartlex/cogcore-repo.git
git push gitea main
# Запросит логин — твой Gitea username + пароль (или token)
```

Теперь GitHub остаётся для CI/auto-deploy (PR review, public visibility), Gitea — для приватных копий и tenant-acccess.

---

## Tenant onboarding (через UI после Phase 5C)

Когда клиент регистрируется через `/ui/pricing` → OTP → `/ui/welcome`:
1. Backend hook автоматически создаёт `gitea-org/<email-local-part>` через Gitea Admin API
2. Клиент видит в welcome ссылку «Открыть Git» → `https://git.ии-память.рф/<его-org>`
3. Первый visit → клиент задаёт пароль для Gitea (отдельный от mcp-аккаунта пока)
4. Клиент `git remote add gitea ...` локально, push'ит свой проект

## Limits per tier

| Tier | Gitea repos | LFS storage | Webhooks |
|---|---|---|---|
| Free | 1 | 500 MB | 0 |
| Pro ($5/мес) | 10 | 10 GB | 5 |
| Enterprise | unlimited | по запросу | unlimited |

Лимиты enforce'ятся через `owner_quotas.max_storage_mb` (Phase 4) — extending в Phase 5B.

## Troubleshooting

- **502 Bad Gateway** на git.ии-память.рф: проверить `docker ps | grep gitea` — healthy? Если нет — `docker logs cognitive_gitea --tail 50`.
- **cert error**: certbot выдал? `ls /etc/letsencrypt/live/git.xn----8sbwawqx4fza.xn--p1ai/`. Если пусто — повторить шаг 2.
- **DB connection**: gitea_db создана? `docker exec cognitive_postgres psql -U cognitive -l | grep gitea`. Пароль в env совпадает с реальным?
- **«SSH connection refused»** при `git clone git@git.ии-память.рф:...`: убедиться что порт 22002 открыт в UFW: `sudo ufw allow 22002/tcp comment "Gitea SSH"`.
