#!/usr/bin/env bash
# Cognitive Core - generate strong production secrets.
#
# Usage:
#   bash scripts/gen-secrets.sh > .env.production
#   # then review and place at .env on server
#
# Generates:
#   - POSTGRES_PASSWORD (32 bytes base64)
#   - S3_ACCESS_KEY (24 bytes hex)
#   - S3_SECRET_KEY (40 bytes base64)
#   - 4 agent API keys (32 bytes hex each)

set -euo pipefail

# Detect random source
if command -v openssl &>/dev/null; then
    rand() { openssl rand -base64 "$1" | tr -d '/+=\n' | head -c "$1"; }
    rand_hex() { openssl rand -hex "$1"; }
else
    rand() { head -c "$1" /dev/urandom | base64 | tr -d '/+=\n' | head -c "$1"; }
    rand_hex() { head -c "$1" /dev/urandom | xxd -p | tr -d '\n' | head -c $((2*$1)); }
fi

PG_PASS=$(rand 32)
S3_ACCESS=$(rand_hex 12)
S3_SECRET=$(rand 40)
AGENT_DESIGNER=$(rand_hex 32)
AGENT_DEVELOPER=$(rand_hex 32)
AGENT_DEFAULT=$(rand_hex 32)
DEEPSEEK_KEY="${DEEPSEEK_API_KEY:-sk-REPLACE-WITH-YOUR-KEY}"

cat <<EOF
# ==================== Cognitive Core production .env ====================
# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Place this file at .env in project root on server.
# Permissions: chmod 600 .env

# === STORAGES ===
DATABASE_URL=postgresql://cognitive:${PG_PASS}@postgres:5432/cognitive_core
POSTGRES_PASSWORD=${PG_PASS}

REDIS_URL=redis://redis:6379

S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=${S3_ACCESS}
S3_SECRET_KEY=${S3_SECRET}
S3_BUCKET=l4-snapshots
S3_SECURE=false

# === LLM (replace DEEPSEEK_API_KEY!) ===
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_API_KEY=${DEEPSEEK_KEY}
LLM_DAILY_ANALYZER=deepseek-chat
LLM_WEEKLY_CONSOLIDATOR=deepseek-chat
LLM_CURATOR_FILTER=deepseek-chat
LLM_CURATOR_QUALITY=deepseek-chat
LLM_CURATOR_AUDIT=deepseek-chat
LLM_CURATOR_ARBITRATION=deepseek-chat
LLM_EMBEDDING=deepseek-embedding
SYSTEM_LANGUAGE=ru

# Fallback (optional)
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
LLM_FALLBACK=gpt-4o-mini

# A/B (optional)
LLM_DAILY_ANALYZER_B=
LLM_AB_TRAFFIC_PERCENT=0

# Local AI (optional)
LOCAL_AI_ENABLED=false
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_CURATOR_MODEL=qwen3:14b
OLLAMA_FAST_MODEL=llama4:8b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text

# === SECURITY ===
# Each agent gets its own key. Save these — they're how clients authenticate!
AGENT_API_KEYS={"agent_designer":"${AGENT_DESIGNER}","agent_developer":"${AGENT_DEVELOPER}","agent_default":"${AGENT_DEFAULT}"}

MAX_PAYLOAD_SIZE=262144
RATE_LIMIT_PER_AGENT=100
MAX_PAYLOAD_DEPTH=10
MAX_PAYLOAD_KEYS=500

# === TLS (optional, prefer nginx termination) ===
SSL_CERT_PATH=
SSL_KEY_PATH=

# === CURATOR ===
CURATOR_TEMPERATURE=0.1
MIN_EVENTS_FOR_DAILY=3
MIN_CONFIDENCE_FOR_L3=0.6
MIN_L2_REPETITIONS_FOR_L3=2
L4_FULL_SNAPSHOT_INTERVAL_WEEKS=4
L4_MIN_CHANGE_PERCENT=5
L3_STALENESS_DAYS=90
TOOL_UNUSED_DAYS=60

# === CYCLES ===
RETENTION_DAYS=14
DAILY_HOURS=24
WEEKLY_DAYS=7

# ==================== End of generated .env ====================
EOF

cat >&2 <<'INFO'

==================================================
  Production .env generated to stdout.
==================================================

Save it:
  bash scripts/gen-secrets.sh > .env.production
  scp .env.production user@server:/opt/cognitive-core/.env
  ssh user@server "chmod 600 /opt/cognitive-core/.env"

IMPORTANT:
  1. Replace DEEPSEEK_API_KEY with your real key (from platform.deepseek.com)
  2. Save AGENT_API_KEYS values — clients use them as X-API-Key
  3. Never commit .env to git
INFO
