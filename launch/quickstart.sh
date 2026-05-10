#!/usr/bin/env bash
# Cognitive Core — one-liner quickstart installer (post-merge layout).
#
# curl -fsSL https://raw.githubusercontent.com/mocartlex-wq/cognitive-core/main/launch/quickstart.sh | bash
#
# Or:
#   curl -fsSL ... | INSTALL_DIR=$HOME/cogcore bash

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/cognitive-core}"
REPO_URL="${REPO_URL:-https://github.com/mocartlex-wq/cognitive-core}"
BRANCH="${BRANCH:-main}"
SKIP_SMOKE="${SKIP_SMOKE:-0}"

cyan()  { printf "\033[1;36m%s\033[0m\n" "$*"; }
green() { printf "\033[1;32m%s\033[0m\n" "$*"; }
red()   { printf "\033[1;31m%s\033[0m\n" "$*" >&2; }
warn()  { printf "\033[1;33m%s\033[0m\n" "$*"; }

cyan "══════════════════════════════════════════════════"
cyan "  Cognitive Core — quickstart"
cyan "  install_dir: $INSTALL_DIR"
cyan "══════════════════════════════════════════════════"

# 1. Preflight
need() { command -v "$1" >/dev/null 2>&1 || { red "❌ missing: $1"; exit 1; }; }
need docker; need openssl; need git
docker compose version >/dev/null 2>&1 || { red "❌ docker compose plugin missing"; exit 1; }
command -v make >/dev/null 2>&1 || warn "⚠  make not found — convenience targets disabled"

# 2. Sparse-clone only the launch/ folder
if [ -d "$INSTALL_DIR/.git" ]; then
  cyan "▶ updating existing checkout in $INSTALL_DIR ..."
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  cyan "▶ sparse-cloning launch/ from $REPO_URL ..."
  git clone --depth 1 --filter=blob:none --sparse --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  git -C "$INSTALL_DIR" sparse-checkout set launch
fi
cd "$INSTALL_DIR/launch"

# 3. .env
if [ -f .env ]; then
  warn "▶ .env already exists — preserving."
else
  cyan "▶ generating .env with random secrets ..."
  cp .env.example .env
  PG_PWD=$(openssl rand -hex 24); S3_KEY=$(openssl rand -hex 12); S3_SEC=$(openssl rand -hex 24)
  ALICE=$(openssl rand -hex 16); BOB=$(openssl rand -hex 16)
  sed -i.bak \
    -e "s|CHANGE_ME_postgres_password|$PG_PWD|" \
    -e "s|CHANGE_ME_minio_access_key|$S3_KEY|" \
    -e "s|CHANGE_ME_minio_secret_key|$S3_SEC|" \
    -e "s|key-alice-CHANGE|$ALICE|" \
    -e "s|key-bob-CHANGE|$BOB|" .env
  rm -f .env.bak
  green "✓ .env generated."
  warn  "⚠  set DEEPSEEK_API_KEY in $INSTALL_DIR/launch/.env (https://platform.deepseek.com/api_keys)."
fi

# 4. Pull + up
cyan "▶ pulling images ..."
docker compose -f docker-compose.public.yml pull
cyan "▶ building extras image (rooms + pg-to-nats) ..."
docker compose -f docker-compose.public.yml build
cyan "▶ bringing stack up ..."
docker compose -f docker-compose.public.yml up -d
sleep 15
docker compose -f docker-compose.public.yml ps --format "table {{.Service}}\t{{.Status}}"

# 5. Smoke
if [ "$SKIP_SMOKE" != "1" ] && [ -x ./scripts/smoke-test.sh ]; then
  cyan "▶ running smoke test ..."
  bash ./scripts/smoke-test.sh || warn "⚠  smoke test failed — see output above. Stack is up; investigate."
fi

green ""
green "══════════════════════════════════════════════════"
green "  ✅ Cognitive Core is up at $INSTALL_DIR/launch"
green "══════════════════════════════════════════════════"
green "  API docs:    http://localhost:9001/docs"
green "  Rooms API:   http://localhost:9098/"
green "  Rooms UI:    http://localhost:9098/ui"
green "  MinIO:       http://localhost:9002"
green ""
green "  Manage:  cd $INSTALL_DIR/launch  &&  make ps | make logs | make backup | make down"
green "══════════════════════════════════════════════════"
