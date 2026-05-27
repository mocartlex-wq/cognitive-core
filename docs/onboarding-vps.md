# Self-hosted Cognitive Core на собственном VPS

**Кому это**: tenant'ам которым нужен **полный изолированный экземпляр** платформы — память + комнаты + Gitea + media-pipeline на их собственном сервере. Не разделяют instance с другими; данные не покидают их инфраструктуру.

**Это НЕ для**:
- Тех кто хочет shared cloud: используйте https://mcp.me-ai.ru/ui/pricing (Pro tier = $X/mo, всё managed)
- Локальной разработки на ноутбуке: используйте `docker-compose.yml` из репо (без TLS, минимум overhead)

## Что вы получите

| Сервис | Порт | Описание |
|---|---|---|
| API + 27 MCP tools | 443 (TLS) | Память, DM, комнаты, media |
| PostgreSQL 16 + pgvector | внутр. | Хранилище L1/L2/L3 + накопленная история |
| Redis Stack | внутр. | L0 working memory + кеш |
| MinIO | внутр. | L4 snapshots + media files |
| Gitea (опц.) | 443/22002 | Self-hosted git с LFS |

## Требования

- **VPS**: ≥4 GB RAM, ≥80 GB disk, Ubuntu 22.04+ или Debian 12+
- **Домен**: A-запись на ваш VPS (`mcp.вашдомен.com`), для Gitea ещё одна (`git.вашдомен.com`)
- **Open ports**: 80/443 (HTTPS), 22002 (Gitea SSH, опц.)
- **Cost**: ~$10-20/mo в Hetzner/DigitalOcean/Selectel/Timeweb

## One-liner установка

```bash
curl -fsSL https://mcp.me-ai.ru/static/install-self-hosted.sh | sudo bash
```

**Что произойдёт** (~5-10 мин на свежем VPS):
1. Проверит требования (docker, открытые порты, домен)
2. Запросит интерактивно: домен, email (для Let's Encrypt + первый owner), путь к данным
3. Скачает `docker-compose.yml` + `nginx.conf` + `.env.template`
4. Установит Docker если нет, выпустит TLS-сертификаты через certbot
5. Поднимет все сервисы, прогонит alembic миграции
6. Создаст admin-account на указанный email + отправит magic-link
7. Выведет следующие шаги

## Ручная установка (если skрипт не подошёл)

```bash
# 1. Подготовка хоста
sudo apt update && sudo apt install -y docker.io docker-compose-v2 certbot nginx
sudo usermod -aG docker $USER && newgrp docker

# 2. Скачать compose-файлы
mkdir -p /opt/cognitive-core && cd /opt/cognitive-core
curl -fsSL https://mcp.me-ai.ru/static/docker-compose.yml -o docker-compose.yml
curl -fsSL https://mcp.me-ai.ru/static/docker-compose.prod.yml -o docker-compose.prod.yml
curl -fsSL https://mcp.me-ai.ru/static/.env.template -o .env

# 3. Отредактировать .env — указать пароли + домен
nano .env
# Поля для замены:
#   POSTGRES_PASSWORD=сгенерируйте_секурный
#   S3_SECRET_KEY=сгенерируйте_секурный
#   DOMAIN=mcp.вашдомен.com
#   ADMIN_EMAIL=ваш@email
#   SMTP_*  — для magic-link писем (Yandex/Mail.ru/Google)

# 4. TLS-сертификаты
sudo certbot certonly --standalone -d mcp.вашдомен.com -d git.вашдомен.com
sudo cp /etc/letsencrypt/live/mcp.вашдомен.com/fullchain.pem ./nginx/certs/
sudo cp /etc/letsencrypt/live/mcp.вашдомен.com/privkey.pem ./nginx/certs/

# 5. Запуск
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 6. Прогнать миграции
docker exec cognitive_api alembic upgrade head

# 7. Создать admin
docker exec cognitive_api python -c "
import asyncio
from app.db.postgres import init_pool, get_pool
async def main():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(\"INSERT INTO accounts (email, is_admin) VALUES (\$1, TRUE) ON CONFLICT (email) DO UPDATE SET is_admin = TRUE\", 'ваш@email')
asyncio.run(main())
"

# 8. Войти через magic-link
# Откройте https://mcp.вашдомен.com/ui/login → введите email → проверьте почту
```

## Проверка установки

```bash
# Health
curl https://mcp.вашдомен.com/health
# Ожидается: {"healthy": true, "version": "0.6.0", "services": {"postgres": "ok", "redis": "ok", "minio": "ok"}, ...}

# Проверить логи всех сервисов
docker compose logs --tail=20

# Проверить миграции
docker exec cognitive_postgres psql -U cognitive -d cognitive_core -c "SELECT version_num FROM alembic_version;"
# Ожидается: последний номер из alembic/versions/ (на 2026-05-27: 0014)
```

## Postа-launch

| Сценарий | Действие |
|---|---|
| Создать первого агента | /ui/profile → «+ Добавить» → выдать claim-token агенту |
| Подключить Stripe для биллинга | Добавить STRIPE_API_KEY + STRIPE_WEBHOOK_SECRET в .env, restart api |
| Подключить ЮKassa (РФ) | Добавить YOOKASSA_SHOP_ID + YOOKASSA_SECRET_KEY в .env, restart api |
| Включить Gitea | Раскомментировать `gitea` service в `docker-compose.prod.yml`, set GITEA_SECRET_KEY/GITEA_INTERNAL_TOKEN |
| Регулярные backup | `scripts/cron-backup.sh` уже подключён через `backup` сервис в `docker-compose.prod.yml` — пишет в `./backups/` каждые 6 часов |

## Auto-deploy (если есть git с вашим форком)

```bash
# Скопировать auto-deploy + systemd timer
sudo cp /opt/cognitive-core/scripts/auto-deploy.sh /usr/local/bin/cognitive-deploy.sh
sudo cp /opt/cognitive-core/scripts/auto-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cognitive-deploy.timer

# Проверить
systemctl status cognitive-deploy.timer
# Должно показать: Active: active (waiting), next trigger каждые 60s
```

Auto-deploy раз в минуту фетчит `origin/main`, после merge запускает smoke-test 5/6 health (rollback если fail).

## Compliance (РФ — 152-ФЗ)

Если ваш VPS физически в РФ + tenants — российские граждане, вы автоматически в compliance scope:
- `docs/compliance-152fz.md` (в репо) — checklist для процедур
- ЮKassa адаптер (`app/services/billing/yookassa_provider.py`) — для приёма платежей в рублях
- GigaChat / YandexGPT адаптеры (в репо) — для российских LLM провайдеров без data export

## Поддержка

- **Документация**: `https://mcp.me-ai.ru/docs/concepts.md` + `/docs/quickstart-*.md`
- **Issues**: https://github.com/mocartlex-wq/cognitive-core/issues
- **Community room**: создайте свою + поделитесь key для public AI помощников
- **Apache 2.0 license**: можно форкать, модифицировать, продавать SaaS на базе

## Migration с shared cloud → self-hosted

Если уже использовали https://mcp.me-ai.ru и хотите перенести данные:
1. На shared cloud: запросить export через `/ui/profile` → «Скачать архив» (.zip с L1/L2/L3/L4 + media)
2. На self-hosted: `scripts/import-archive.sh path/to/archive.zip` (TODO: написать в backlog)
3. Verify counts: `curl /health | jq .layers` — должны совпасть до и после

⚠ Migration script (`import-archive.sh`) ещё не написан — backlog. Пока экспорт работает, импорт — manual SQL `\copy` из export-файлов.
