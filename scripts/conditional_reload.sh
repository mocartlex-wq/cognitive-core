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
            reload_nginx=1; worth_logging=1
            # Versioned snapshot: keep last 10 working nginx.confs in history/
            # for emergency rollback (config-only) without rolling back code.
            HIST="$REPO_DIR/nginx/history"
            mkdir -p "$HIST"
            if [ -f "$REPO_DIR/nginx/nginx.conf" ]; then
                cp "$REPO_DIR/nginx/nginx.conf" "$HIST/nginx.conf.${PREV:0:7}" 2>/dev/null || true
            fi
            # LRU: trim to last 10
            # shellcheck disable=SC2012
            ls -t "$HIST"/nginx.conf.* 2>/dev/null | tail -n +11 | xargs -r rm -f
            ;;
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
        scripts/cognitive-rooms.py)
            # Live-файл лежит в /usr/local/lib/cognitive-rooms.py (запуск через
            # systemd unit cognitive-rooms.service). git import 2026-05-21 —
            # теперь scripts/cognitive-rooms.py = source of truth, sync атомарный.
            echo "[$(date -Iseconds)] rooms-server changed — sync + restart"
            sudo install -m 0755 "$REPO_DIR/scripts/cognitive-rooms.py" /usr/local/lib/cognitive-rooms.py
            sudo systemctl restart cognitive-rooms || true
            worth_logging=1 ;;
        scripts/cognitive-agent-runtime.py)
            # Демон заместителей — такой же systemd-сервис на хосте, но до
            # 2026-08-11 его здесь не было вовсе: правки доезжали в git-checkout
            # и НЕ применялись. Отсюда системный дрейф репозиторий↔сервер и
            # процедура ручного diff в docs/sre-runbook.md.
            #
            # Перезапуск рвёт незавершённые ответы (агент может писать в комнату
            # прямо сейчас), поэтому:
            #   1. проверяем синтаксис ДО подмены — сломанный файл не должен
            #      останавливать работающий демон;
            #   2. сверяем содержимое: git-коммит мог задеть файл, не изменив
            #      его (переносы строк, cherry-pick) — рестарт впустую не нужен.
            NEW_FILE="$REPO_DIR/scripts/cognitive-agent-runtime.py"
            LIVE_FILE=/usr/local/lib/cognitive-agent-runtime.py
            if ! python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$NEW_FILE"; then
                echo "[$(date -Iseconds)] agent-runtime: синтаксическая ошибка — подмена ОТМЕНЕНА, демон работает на прежней версии"
            elif cmp -s "$NEW_FILE" "$LIVE_FILE"; then
                echo "[$(date -Iseconds)] agent-runtime: файл не изменился — рестарт не нужен"
            else
                echo "[$(date -Iseconds)] agent-runtime changed — sync + restart"
                sudo install -m 0755 "$NEW_FILE" "$LIVE_FILE"
                sudo systemctl restart cognitive-agent-runtime || true
            fi
            worth_logging=1 ;;
        *)
            : ;;  # docs / scripts / .md / прочее — игнор
    esac
done <<< "$CHANGED"

if [ "$worth_logging" = "0" ]; then
    echo "[$(date -Iseconds)] nothing relevant changed in ${PREV:0:7}..${NEW:0:7} — skipping reload"
    exit 0
fi

# IPv6 build workaround (2026-05-28): buildkit резолвит registry-1.docker.io
# по IPv6 → unreachable на РФ VPS → build падает. docker CLI pull use IPv4
# fallback → primes local cache → build берёт base из cache. Idempotent.
_prepull_base_images() {
    # Extract FROM base images из Dockerfile + pull через CLI (IPv4 fallback)
    local bases
    bases=$(grep -hiE '^FROM ' "$REPO_DIR"/Dockerfile 2>/dev/null | awk '{print $2}' | grep -v '^scratch$' | sort -u)
    for img in $bases; do
        echo "[$(date -Iseconds)] pre-pull base $img (IPv6 build workaround)"
        docker pull "$img" 2>&1 | tail -1 || echo "  pre-pull $img failed (build may retry)"
    done
}

# Миграции схемы (2026-09-05). До этого деплой НИКОГДА не запускал alembic:
# файлы миграций приезжали в образ, а схема прода отставала (0016→0018 в июле
# докатывали руками; 2026-09-05 две ветки взяли один номер 0022). Теперь после
# каждого подъёма api — `alembic upgrade head`: идемпотентно, лечит отставание
# любого происхождения. Провал миграции = провал деплоя (auto-deploy откатит
# код). ⚠️ Схему при откате НЕ понижаем: миграции обязаны быть обратно
# совместимыми (add-only), иначе откат кода на старую схему — отдельная беда.
_migrate() {
    # При откате (auto-deploy зовёт нас с COGNITIVE_MIGRATE=0) старый образ не
    # знает ревизию, которую только что применил новый → alembic упал бы на
    # «Can't locate revision». Схему не трогаем, код откатываем.
    if [ "${COGNITIVE_MIGRATE:-1}" != "1" ]; then
        echo "[$(date -Iseconds)] alembic: пропуск (COGNITIVE_MIGRATE=${COGNITIVE_MIGRATE}, откат кода без понижения схемы)"
        return 0
    fi
    local attempt
    for attempt in 1 2 3; do
        if docker compose $COMPOSE_FILES exec -T api alembic upgrade head; then
            echo "[$(date -Iseconds)] alembic upgrade head: ok"
            return 0
        fi
        echo "[$(date -Iseconds)] alembic upgrade head: попытка $attempt не удалась (api/БД ещё поднимаются?)"
        sleep 5
    done
    echo "[$(date -Iseconds)] ERROR: alembic upgrade head провалился 3 раза — деплой считаем неудачным" >&2
    return 1
}

if [ "$restart_full" = "1" ]; then
    echo "[$(date -Iseconds)] full restart (compose-file or env changed)"
    _prepull_base_images
    docker compose $COMPOSE_FILES up -d --build
    _migrate
    exit 0
fi

# Helper: rebuild только тех сервисов которые ДЕЙСТВИТЕЛЬНО есть в compose.
# В 2026-05 mcp-контейнер был removed (FastMCP native в cognitive_api), и
# попытка `docker compose up mcp` падает с "no such service: mcp" — что
# заваливает auto-deploy в loop. Phase A 2026-05-21: фильтруем по
# `docker compose config --services`.
COMPOSE_SERVICES=$(docker compose $COMPOSE_FILES config --services 2>/dev/null | tr '\n' ' ')
services_to_build=""
if [ "$rebuild_api" = "1" ] && echo " $COMPOSE_SERVICES " | grep -q " api "; then
    services_to_build="$services_to_build api"
fi
if [ "$rebuild_mcp" = "1" ] && echo " $COMPOSE_SERVICES " | grep -q " mcp "; then
    services_to_build="$services_to_build mcp"
fi

if [ -n "$services_to_build" ]; then
    echo "[$(date -Iseconds)] rebuilding:$services_to_build"
    _prepull_base_images
    docker compose $COMPOSE_FILES up -d --build $services_to_build
    if echo " $services_to_build " | grep -q " api "; then
        _migrate
    fi
elif [ "$rebuild_api" = "1" ] || [ "$rebuild_mcp" = "1" ]; then
    echo "[$(date -Iseconds)] api/mcp rebuild requested but no such services in compose ($COMPOSE_SERVICES) — skipping"
fi

if [ "$reload_nginx" = "1" ]; then
    # Bind-mount stale issue (документировано в памяти 2026-05):
    # git pull --ff-only делает atomic rename → docker bind mount держит
    # старый inode → `nginx -s reload` читает СТАРЫЙ файл. Решение —
    # docker restart, тогда новый inode правильно подхватывается.
    # nginx -t всё равно валидируем ДО restart чтобы не сломать прод.
    echo "[$(date -Iseconds)] validating nginx config + restart (bind-mount fresh-inode)"
    if docker exec cognitive_nginx nginx -t -c /tmp/.fresh-test 2>/dev/null; then
        :  # noop — пройдём ниже
    fi
    # Копируем host-файл в /tmp в контейнере + валидируем — обход stale bind
    if docker cp "$REPO_DIR/nginx/nginx.conf" cognitive_nginx:/tmp/.fresh-test.conf 2>/dev/null && \
       docker exec cognitive_nginx nginx -t -c /tmp/.fresh-test.conf >/dev/null 2>&1; then
        docker restart cognitive_nginx >/dev/null
        sleep 2
        if ! docker exec cognitive_nginx nginx -t >/dev/null 2>&1; then
            echo "[$(date -Iseconds)] ERROR: nginx config invalid after restart — производство сломано" >&2
            exit 1
        fi
        echo "[$(date -Iseconds)] nginx restarted (fresh inode)"
    else
        echo "[$(date -Iseconds)] ERROR: nginx -t failed на новом config — НЕ restart, fix nginx.conf" >&2
        docker cp "$REPO_DIR/nginx/nginx.conf" cognitive_nginx:/tmp/.fresh-test.conf 2>/dev/null
        docker exec cognitive_nginx nginx -t -c /tmp/.fresh-test.conf 2>&1 | head -10 || true
        exit 1
    fi
fi
