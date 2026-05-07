#!/usr/bin/env bash
# Cognitive Core — conditional reload по diff'у.
# Решает что перезапустить на основе списка изменённых файлов между двумя коммитами.
# Минимум disruption: nginx reload не трогает API; rebuild api не трогает MCP и наоборот.
#
# Usage: conditional_reload.sh <prev-sha> <new-sha>

set -euo pipefail

PREV="${1:?prev sha required}"
NEW="${2:?new sha required}"

REPO_DIR="${COGNITIVE_REPO_DIR:-/opt/cognitive-core}"
# Base + prod overlay; локальный override (например docker-compose.override.yml на сервере
# определяет mcp-сайдкар) подхватывается, если файл существует. Override в .gitignore —
# это нормально, у каждой машины он свой.
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
if [ -f "$REPO_DIR/docker-compose.override.yml" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.override.yml"
fi

cd "$REPO_DIR"

CHANGED=$(git diff --name-only "$PREV" "$NEW")

reload_nginx=0
rebuild_api=0
rebuild_mcp=0
restart_full=0
worth_logging=0

while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        nginx/*)
            reload_nginx=1; worth_logging=1 ;;
        mcp_server/*)
            rebuild_mcp=1; worth_logging=1 ;;
        app/*|alembic/*|requirements*.txt|pyproject.toml|Dockerfile)
            rebuild_api=1; rebuild_mcp=1; worth_logging=1 ;;
        docker-compose*.yml|.env*)
            restart_full=1; worth_logging=1 ;;
        scripts/auto-deploy.sh|scripts/conditional_reload.sh|deploy/cognitive-deploy.*)
            # Сами себя тоже релоадим (через systemctl daemon-reload).
            # systemctl требует root → sudo (salex в sudoers с NOPASSWD).
            echo "[$(date -Iseconds)] deploy infra changed — reloading systemd"
            if [ -f /etc/systemd/system/cognitive-deploy.service ]; then
                sudo cp "$REPO_DIR/deploy/cognitive-deploy.service" /etc/systemd/system/
                sudo cp "$REPO_DIR/deploy/cognitive-deploy.timer"   /etc/systemd/system/
            fi
            sudo systemctl daemon-reload || true
            sudo systemctl restart cognitive-deploy.timer || true
            worth_logging=1 ;;
        *)
            : ;;  # docs / scripts / .md / прочее — игнор
    esac
done <<< "$CHANGED"

if [ "$worth_logging" = "0" ]; then
    echo "[$(date -Iseconds)] nothing relevant changed in ${PREV:0:7}..${NEW:0:7} — skipping reload"
    exit 0
fi

if [ "$restart_full" = "1" ]; then
    echo "[$(date -Iseconds)] full restart (compose-file or env changed)"
    docker compose $COMPOSE_FILES up -d --build
    exit 0
fi

if [ "$rebuild_api" = "1" ] && [ "$rebuild_mcp" = "1" ]; then
    echo "[$(date -Iseconds)] rebuilding api+mcp (shared code changed)"
    docker compose $COMPOSE_FILES up -d --build api mcp
elif [ "$rebuild_api" = "1" ]; then
    echo "[$(date -Iseconds)] rebuilding api"
    docker compose $COMPOSE_FILES up -d --build api
elif [ "$rebuild_mcp" = "1" ]; then
    echo "[$(date -Iseconds)] rebuilding mcp"
    docker compose $COMPOSE_FILES up -d --build mcp
fi

if [ "$reload_nginx" = "1" ]; then
    echo "[$(date -Iseconds)] reloading nginx"
    if docker exec cognitive_nginx nginx -t >/dev/null 2>&1; then
        docker exec cognitive_nginx nginx -s reload
    else
        echo "[$(date -Iseconds)] ERROR: nginx -t failed — config NOT reloaded, fix nginx.conf" >&2
        docker exec cognitive_nginx nginx -t || true
        exit 1
    fi
fi
