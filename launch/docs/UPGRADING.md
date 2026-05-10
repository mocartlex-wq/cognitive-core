# Upgrading

Cognitive Core uses Alembic for schema migrations. Migrations are baked into the API
image and applied automatically at container start.

## Routine update

```bash
make backup            # ALWAYS first
make pull              # fetch new images
make up                # recreate containers; alembic upgrade runs in api startup
make ps                # confirm everything is healthy
make smoke             # E2E sanity check
```

If `make smoke` fails, see logs:
```bash
make logs-api | head -100
```

## Rollback

```bash
docker compose -f docker-compose.public.yml down
make restore FILE=./backups/<pre-upgrade-snapshot>.sql.gz
# Pin previous image tag in .env:
echo 'IMAGE_API=ghcr.io/cognitive-core/api:v0.4.7' >> .env
make up
```

## Major version bumps

Major versions (e.g. v0.4 → v0.5) may include **breaking** schema changes that cannot
be auto-rolled-back. Read the release notes:

```bash
curl -s https://api.github.com/repos/cognitive-core/launch/releases | \
  jq '.[0:3] | .[] | {tag_name, body}'
```

Always do a dry-run on a copy:

```bash
docker compose -f docker-compose.public.yml -p cogcore-test up -d
make backup    # in main project
make restore FILE=./backups/...sql.gz   # into test project, override COMPOSE_PROJECT
```

## Schema version pin

For air-gapped environments, pin the migration head in `.env`:

```
ALEMBIC_TARGET=head    # default — apply all pending
# or:
ALEMBIC_TARGET=3a4f8c12abcd
```

## Volume layout

Volumes that **MUST be preserved** between upgrades:

| Volume | Holds |
|--------|-------|
| `postgres_data` | All L1–L5 tables, room data |
| `redis_data` | L0 blackboard, locks, presence |
| `minio_data` | L4 snapshots |
| `nats_data` | JetStream durable streams |
| `pg_nats_state` | Replay buffer for the push pipeline |

Volumes that are **safe to drop** (will be recreated):

| Volume | Holds |
|--------|-------|
| `nginx_logs` | Access/error logs (rotate to disk first) |

## Downgrade not supported

Alembic supports `downgrade`, but Cognitive Core has not committed to forward-compatible
migrations. **Treat downgrade as "restore from backup"**.
