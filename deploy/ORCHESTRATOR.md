# Cognitive Orchestrator (port 9099)

**Receiver → 1–3 Executors → Synthesizer** pipeline. A user submits a goal
(`POST /orchestrator/ask` or the `/ui/ask` PWA); a DeepSeek "receiver" routes it to
executor agents (via the existing `/agents/message` handoff); results are synthesized
and returned. Includes JWT auth, a capabilities registry, SSE live updates, heartbeat.

## Why this is in git now
The orchestrator historically ran from a hand-installed `/usr/local/lib/cognitive-orchestrator.py`
that was **not tracked in the repo**. On 2026-05-29 that caused an incident: a separate change
reused the `cognitive-orchestrator.service` name and overwrote its unit. Version-controlling it
here (source of truth = `scripts/cognitive-orchestrator.py`) prevents a repeat.

## Files
- `scripts/cognitive-orchestrator.py` — the service (config entirely via env).
- `deploy/cognitive-orchestrator.service` — systemd unit (repo-path version).
- `deploy/cognitive-orchestrator-launch.sh` — launch wrapper: resolves the live postgres
  docker IP into `ORCH_DB_DSN` at each start (host service → docker IP changes on recreate).
- `deploy/cognitive-orchestrator.env.example` — env template; real file is
  `/etc/cognitive-orchestrator.env` (chmod 600, secrets, not in git).

## Current deployment vs repo
As of 2026-05-29 the running service still execs `/usr/local/lib/cognitive-orchestrator.py`
(hand-installed) via the launch wrapper. To migrate to repo-managed (after the auto-deploy
syncs this dir to `/opt/cognitive-core/`):

```
sudo cp /opt/cognitive-core/deploy/cognitive-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart cognitive-orchestrator
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9099/orchestrator/capabilities   # expect 200
```

## Stability TODO
- Postgres has no host-published port; the wrapper works around IP churn. Cleaner long-term:
  publish the PG port to the host **or** containerize the orchestrator (then use the docker
  service name `cognitive_postgres` directly).
- If the DB password was ever exposed, rotate it and update `ORCH_DB_DSN` here **and** the api DSN.
