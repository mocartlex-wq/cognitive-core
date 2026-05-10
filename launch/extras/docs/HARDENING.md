# Production hardening

The default `docker-compose.public.yml` is tuned for a **single-host self-hosted** deployment
behind a trusted network. Before exposing it on the public internet, work through this list.

## 1. Secrets

- [ ] **Rotate** all values in `.env` away from `make init` defaults at least once before
      moving past the first 24 h.
- [ ] Move `.env` outside the repo and reference it via Docker secrets or a secret manager
      (Vault, AWS Secrets Manager, sops + age).
- [ ] Set `chmod 600 .env`. Never commit it.
- [ ] Audit `git log --all -- .env` to confirm it was never accidentally pushed.

## 2. Network

- [ ] Default compose binds storage ports (5432, 6379, 9000, 9002, 4222, 8222) to
      `127.0.0.1` only — keep it that way.
- [ ] Run `make up-edge` to enable the `nginx` profile for TLS termination, and put a
      firewall (`ufw`, security group) in front. Only ports `80` and `443` should reach
      the box.
- [ ] If exposing rooms / API publicly, add Cloudflare (or equivalent) in front for DDoS,
      WAF, rate limit at the edge.
- [ ] Disable WS `same_origin: false` in `nats/nats.conf` — set
      `allowed_origins: ["https://your-domain"]`.

## 3. TLS

`make up-edge` mounts `./nginx/certs/` read-only into the nginx container. Get a cert via
Let's Encrypt:

```bash
docker run -it --rm --name certbot \
  -v "$PWD/nginx/certs:/etc/letsencrypt" \
  -v "$PWD/nginx/www:/var/www/certbot" \
  -p 80:80 -p 443:443 \
  certbot/certbot certonly --standalone -d your-domain.example
```

Then `make restart`. Renewal: cron `certbot renew --quiet && make restart`.

## 4. Auth

- [ ] Replace the default `AGENT_API_KEYS` JSON with one entry per real agent.
      Use `openssl rand -hex 32` for each key.
- [ ] Each room gets its own `api_key` automatically on `POST /rooms` — clients pass it
      via `X-Room-Key`. Keys are stored in plaintext in `rooms.api_key` (column).
      To rotate: `UPDATE rooms SET api_key = encode(gen_random_bytes(24), 'hex') WHERE id = ...`.
- [ ] **Never share** a room key in the URL. Use the header.
- [ ] If multi-tenant, run separate stacks per tenant — there is no row-level RBAC yet.

## 5. Rate limiting

The API enforces `RATE_LIMIT_PER_AGENT` (default 100 req / minute / agent_id) at the
application layer. For Rooms there is **no** built-in rate limit yet — add at nginx:

```nginx
# nginx.conf
limit_req_zone $http_x_room_key zone=room_key:10m rate=20r/s;

server {
    location /rooms/ {
        limit_req zone=room_key burst=40 nodelay;
        proxy_pass http://rooms:9098;
    }
}
```

## 6. Backups

- `make backup` writes a one-shot `pg_dump` to `./backups/`. Schedule it:
  ```cron
  0 */6 * * * cd /opt/cognitive-core && /usr/bin/make backup >> backup.log 2>&1
  ```
- Volumes (`postgres_data`, `redis_data`, `minio_data`, `nats_data`) live under
  `/var/lib/docker/volumes/`. Snapshot the host disk or use `docker run --rm -v ...
  alpine tar`.
- Test **restores** quarterly: `make restore FILE=...` into a throwaway compose project.
- Off-site: copy `./backups/` to S3 / B2 / rclone-target. Keep ≥30 days.

## 7. Updates

- Pin images via the `IMAGE_API` env var to a commit SHA tag, not `:latest`, in
  production.
- Watch `https://github.com/mocartlex-wq/cognitive-core/releases`.
- Schema migrations ship as Alembic revisions; on `make pull && make up` the API
  applies them at startup. Always `make backup` before `make pull`.

## 8. Observability

- Container logs are JSON-rotated 10 MB × 3 by default. To centralize: forward to Loki
  via `promtail`, or add `logging.driver: gelf|fluentd|syslog`.
- `/health` on the API and rooms services is suitable for blackbox monitoring.
- For deep metrics, deploy Prometheus + Grafana — example dashboards live at
  `docs/observability/`.

## 9. Resource limits

`docker-compose.public.yml` does **not** set `deploy.resources` to keep the file readable.
For production, append a `docker-compose.prod.yml` overlay with `cpus` / `memory` caps —
see the upstream `docker-compose.prod.yml` in the source repo for working values.

## 10. Threat model — what we do NOT defend against

- A compromised agent key can read/write that agent's memory and any room they joined.
  There is no "audit only" or "read only" key tier yet.
- A compromised host == game over. The DB is plaintext on disk.
- DeepSeek / OpenAI providers see your prompts. Don't put secrets in agent messages.
- Rooms have no end-to-end encryption — server-side trust required.

If any of those matter to you, fork and add the missing primitive — or open an issue.
