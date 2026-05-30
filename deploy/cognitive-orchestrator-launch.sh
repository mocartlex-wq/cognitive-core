#!/usr/bin/env bash
# Launch wrapper for the Cognitive Orchestrator (host systemd service).
# The orchestrator runs on the host but talks to postgres in the docker network,
# whose container IP changes on recreate. This wrapper resolves the CURRENT
# postgres IP and rewrites the host= in ORCH_DB_DSN (in-process) before exec,
# so the service survives postgres IP churn and reboots. Falls back to the
# EnvironmentFile value if docker inspect is unavailable.
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' cognitive_postgres 2>/dev/null)
[ -n "$IP" ] && export ORCH_DB_DSN=$(echo "$ORCH_DB_DSN" | sed "s/host=[0-9.]\+/host=$IP/")
exec /usr/bin/python3 /opt/cognitive-core/scripts/cognitive-orchestrator.py
