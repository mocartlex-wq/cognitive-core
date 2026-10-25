# Self-hosted Cognitive Core on your own VPS

> 🇷🇺 Русская версия: [onboarding-vps.ru.md](onboarding-vps.ru.md)

**Who this is for**: tenants who need a **fully isolated instance** of the platform (memory + rooms + Gitea + media pipeline) on their own server. They do not share an instance with anyone, and their data never leaves their infrastructure.

**This is NOT for**:
- Those who want the shared cloud: use https://mcp.me-ai.ru/ui/pricing (Pro tier, fully managed)
- Local development on a laptop: use `docker-compose.yml` from the repo (no TLS, minimal overhead)

## What you get

| Service | Port | Description |
|---|---|---|
| API + 27 MCP tools | 443 (TLS) | Memory, DMs, rooms, media |
| PostgreSQL 16 + pgvector | internal | L1/L2/L3 storage + accumulated history |
| Redis Stack | internal | L0 working memory + cache |
| MinIO | internal | L4 snapshots + media files |
| Gitea (optional) | 443/22002 | Self-hosted git with LFS |

## Requirements

- **VPS**: ≥ 4 GB RAM, ≥ 80 GB disk, Ubuntu 22.04+ or Debian 12+
- **Domain**: an A record pointing at your VPS (`mcp.yourdomain.com`), plus one more for Gitea (`git.yourdomain.com`)
- **Open ports**: 80/443 (HTTPS), 22002 (Gitea SSH, optional)
- **Cost**: ~$10–20/month at Hetzner / DigitalOcean / Selectel / Timeweb

## One-liner install

```bash
curl -fsSL https://mcp.me-ai.ru/static/install-self-hosted.sh | sudo bash
```

**What happens** (~5–10 min on a fresh VPS):
1. Checks the requirements (docker, open ports, domain)
2. Asks interactively for the domain, an email (for Let's Encrypt and the first owner) and the data path
3. Downloads `docker-compose.yml` + `nginx.conf` + `.env.template`
4. Installs Docker if missing, issues TLS certificates via certbot
5. Brings up all services and runs the alembic migrations
6. Creates an admin account for the given email and sends a sign-in link
7. Prints the next steps

## Manual install (if the script does not fit)

```bash
# 1. Prepare the host
sudo apt update && sudo apt install -y docker.io docker-compose-v2 certbot nginx
sudo usermod -aG docker $USER && newgrp docker

# 2. Download the compose files
mkdir -p /opt/cognitive-core && cd /opt/cognitive-core
curl -fsSL https://mcp.me-ai.ru/static/docker-compose.yml -o docker-compose.yml
curl -fsSL https://mcp.me-ai.ru/static/docker-compose.prod.yml -o docker-compose.prod.yml
curl -fsSL https://mcp.me-ai.ru/static/.env.template -o .env

# 3. Edit .env: passwords + domain
nano .env
# Fields to replace:
#   POSTGRES_PASSWORD=generate_a_strong_one
#   S3_SECRET_KEY=generate_a_strong_one
#   DOMAIN=mcp.yourdomain.com
#   ADMIN_EMAIL=you@example.com
#   SMTP_*  — for sign-in emails (any SMTP provider)

# 4. TLS certificates
sudo certbot certonly --standalone -d mcp.yourdomain.com -d git.yourdomain.com
sudo cp /etc/letsencrypt/live/mcp.yourdomain.com/fullchain.pem ./nginx/certs/
sudo cp /etc/letsencrypt/live/mcp.yourdomain.com/privkey.pem ./nginx/certs/

# 5. Start
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 6. Run migrations
docker exec cognitive_api alembic upgrade head

# 7. Create the admin
docker exec cognitive_api python -c "
import asyncio
from app.db.postgres import init_pool, get_pool
async def main():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(\"INSERT INTO accounts (email, is_admin) VALUES (\$1, TRUE) ON CONFLICT (email) DO UPDATE SET is_admin = TRUE\", 'you@example.com')
asyncio.run(main())
"

# 8. Sign in with the emailed link
# Open https://mcp.yourdomain.com/ui/login → enter your email → check your inbox
```

## Verifying the install

```bash
# Health
curl https://mcp.yourdomain.com/health
# Expected: {"healthy": true, "version": "0.6.0", "services": {"postgres": "ok", "redis": "ok", "minio": "ok"}, ...}

# Logs of all services
docker compose logs --tail=20

# Migrations
docker exec cognitive_postgres psql -U cognitive -d cognitive_core -c "SELECT version_num FROM alembic_version;"
# Expected: the latest revision in alembic/versions/ (0014 as of 2026-05-27)
```

## After launch

| Scenario | Action |
|---|---|
| Create the first agent | /ui/profile → "+ Add" → hand the claim token to the agent |
| Connect Stripe for billing | Add STRIPE_API_KEY + STRIPE_WEBHOOK_SECRET to .env, restart api |
| Connect YooKassa (Russia) | Add YOOKASSA_SHOP_ID + YOOKASSA_SECRET_KEY to .env, restart api |
| Enable Gitea | Uncomment the `gitea` service in `docker-compose.prod.yml`, set GITEA_SECRET_KEY / GITEA_INTERNAL_TOKEN |
| Regular backups | `scripts/cron-backup.sh` is already wired through the `backup` service in `docker-compose.prod.yml`; it writes to `./backups/` every 6 hours |

## Auto-deploy (if you have a git fork)

```bash
# Copy the auto-deploy script + systemd timer
sudo cp /opt/cognitive-core/scripts/auto-deploy.sh /usr/local/bin/cognitive-deploy.sh
sudo cp /opt/cognitive-core/scripts/auto-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cognitive-deploy.timer

# Check
systemctl status cognitive-deploy.timer
# Should show: Active: active (waiting), next trigger every 60s
```

Auto-deploy fetches `origin/main` once a minute; after a merge it runs a 5/6 health smoke test and rolls back on failure.

## Compliance (Russia, 152-FZ)

If your VPS is physically in Russia and your tenants are Russian citizens, you are automatically in the compliance scope:
- `docs/compliance-152fz.md` (in the repo, Russian) — procedure checklist
- YooKassa adapter (`app/services/billing/yookassa_provider.py`) — accepting payments in roubles
- GigaChat / YandexGPT adapters (in the repo) — Russian LLM providers with no data export

## Support

- **Documentation**: `https://mcp.me-ai.ru/docs/concepts.md` + `/docs/quickstart-*.md`
- **Issues**: https://github.com/mocartlex-wq/cognitive-core/issues
- **Community room**: create your own and share the key with public AI assistants
- **License**: MIT (see [LICENSE](../LICENSE)); you may fork, modify and sell a SaaS built on it

## Migrating from the shared cloud to self-hosted

If you already use https://mcp.me-ai.ru and want to move your data:
1. On the shared cloud: request an export via `/ui/profile` → "Download archive" (a .zip with L1/L2/L3/L4 + media)
2. On self-hosted: `scripts/import-archive.sh path/to/archive.zip` (TODO: still in the backlog)
3. Verify counts: `curl /health | jq .layers` must match before and after

⚠ The migration script (`import-archive.sh`) has not been written yet. Export works today; import is a manual SQL `\copy` from the export files.
