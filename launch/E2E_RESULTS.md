# E2E validation results — 2026-05-10

**Status**: ✅ **ALL SMOKE CHECKS PASS** on fresh isolated deploy.

## Test setup

- Server: production host (i5-7500, 32 GB RAM)
- Compose project: `cogcore-test` (separate from production `cognitive-*`)
- Ports: 19001 (api), 19098 (rooms), 15432/16379/19000/19002/14222/18222/18443/18765/19097
- Volumes: dedicated `cogcore-test_*` volumes
- Image: `ghcr.io/cognitive-core/api:latest` retagged from local `cognitive-core-api:latest`
- Extras image: `cognitive-core-extras:latest` built from `extras/Dockerfile` (python:3.12-slim + psycopg+httpx+fastapi+redis+openai)

## Smoke results

```
Cognitive Core smoke test
  API:   http://localhost:19001
  Rooms: http://localhost:19098

  ▶ API /health ...
  ✓ API healthy
  ▶ POST /rooms ...
  ✓ room created: dd38e95f (key=rk_syOs6…)
  ▶ two participants join ...
  ✓ alice + bob joined
  ▶ POST broadcast ...
  ✓ broadcast posted
  ▶ GET /messages ...
  ✓ messages returned (1)
  ▶ GET /sync-pending ...
  ✓ sync-pending OK

✅ all smoke checks passed
```

Stack status after smoke:

| Service     | Status              |
|-------------|---------------------|
| api         | Up (health: starting → ok) |
| mcp         | Up                  |
| minio       | Up (healthy)        |
| nats        | Up                  |
| pg-to-nats  | Up                  |
| postgres    | Up (healthy)        |
| redis       | Up (healthy)        |
| rooms       | Up                  |

## Bugs found + fixed

### 1. `container_name:` blocked multi-instance
**Symptom**: `Error response from daemon: Conflict. The container name "/cognitive_postgres" is already in use`
**Root cause**: 8 services hardcoded `container_name:` → can't run a second compose project on same host.
**Fix**: Removed all `container_name:` entries from `docker-compose.public.yml`. Compose now derives names as `<project>-<service>-1` automatically.

### 2. redis-stack image with wrong command skipped modules
**Symptom**: api startup fails with `redis.exceptions.ResponseError: unknown command 'FT.CREATE'`
**Root cause**: Compose used `redis/redis-stack:latest` but overrode `command:` with `redis-server` (which doesn't load modules). Plain redis works for AOF but not for the API's RediSearch index.
**Fix**: Switched image to `redis/redis-stack-server:latest` and command to `redis-stack-server` so modules load.

### 3. rooms + pg-to-nats containers missing psycopg v3
**Symptom**: `pg-to-nats-1 | ERROR: pip install psycopg[binary]` (script's own import-error handler).
**Root cause**: api image has `psycopg2-binary` but not `psycopg` v3; the rooms.py / pg-to-nats.py scripts use psycopg v3 (for proper LISTEN/NOTIFY support).
**Fix**: Added `extras/Dockerfile` that builds `cognitive-core-extras:latest` from `python:3.12-slim` with the right deps. Compose `rooms` and `pg-to-nats` services use `build: ./extras` instead of the api image.

### 4. rooms.py shelled out to `docker exec` to derive DSN
**Symptom**: `{"error": "[Errno 2] No such file or directory: 'docker'"}`
**Root cause**: Production `cognitive-rooms.py` derives `PG_DSN` via `docker exec cognitive_postgres printenv POSTGRES_PASSWORD` — works on host (where docker CLI exists), not inside a container.
**Fix**: Patched `_get_pg_conn()` to prefer `os.environ['DATABASE_URL']` / `PG_DSN` first, fall back to docker derivation. Same fix applied to `get_deepseek_key()`.

### 5. Rooms tables didn't exist in fresh DB
**Symptom**: First request after stack came up returned 500 because `rooms` table missing.
**Root cause**: `cognitive-rooms.py` expected schema to exist; production DB was hand-migrated.
**Fix**: Added `extras/init/01-rooms-schema.sql` mounted into postgres `/docker-entrypoint-initdb.d/`. Runs once on first start (when data dir is empty). Includes all 4 tables, indexes, the `notify_room_message()` function and trigger.

### 6. smoke-test.sh greps mismatched actual API response shape
**Symptom**: All responses came back valid JSON, but test failed because grep patterns didn't match.
**Root cause**: I wrote the test based on assumed response shape. Real responses use:
- `/health` → `{"healthy":true,...}` (not `"status":...`)
- `/rooms/{id}/join` → `{"ok":true,...}` (not `"joined":...`)
- `/rooms/{id}/post` → `{"id":"..."}` (not `"message_id":...`)
- `/sync-pending` → `{"pending_questions":[...]}` (not `"pending":...`)
**Fix**: Broadened grep patterns to accept either shape: `'"(healthy|status)"'`, `'"(ok|joined)"'`, `'"(message_id|id)"'`, `'"(pending|pending_questions)"'`.

## Pre-existing issue (DID NOT FIX in this pass)

Production rooms.py has a code path (line ~138) that still uses `docker exec ... psql` as a fallback for the `pg()` helper. It's only triggered when `_HAVE_PSYCOPG` is False — which can't happen in our extras image because psycopg is always installed. So this is dormant code and doesn't affect the smoke-tested deploy. Should be removed in a follow-up cleanup PR.

## Re-test instructions

For any future hacker:

```bash
cd /opt/cognitive-server-docs/launch
mkdir -p /tmp/cogcore-test && cp -r * .env.example .github .markdownlint.json /tmp/cogcore-test/
cd /tmp/cogcore-test
cp .env.example .env
sed -i 's|CHANGE_ME_postgres_password|test_pwd|; s|CHANGE_ME_minio_access_key|tk|; s|CHANGE_ME_minio_secret_key|ts|' .env
docker compose -f docker-compose.public.yml up -d --build
sleep 30
bash scripts/smoke-test.sh
docker compose -f docker-compose.public.yml down -v
```

## Inventory after fixes

- `46 files`, `~316 KB` total in `/opt/cognitive-server-docs/launch/`
- New files added during this validation pass:
  - `extras/Dockerfile`
  - `extras/init/01-rooms-schema.sql`
  - `extras/cognitive-rooms.py` (production copy + 2 env-driven patches)
  - `extras/cognitive-pg-to-nats.py` (production copy verbatim)
  - `E2E_RESULTS.md` (this file)
