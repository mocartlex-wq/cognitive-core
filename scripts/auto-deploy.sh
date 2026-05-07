#!/usr/bin/env bash
# Cognitive Core — auto-deploy poller with smoke-test + auto-rollback.
# Запускается systemd-timer-ом каждые 60 сек на сервере.
#
# Алгоритм:
#   1. git fetch origin/main; если HEAD не сменился — exit 0 (silent)
#   2. Сохранить prev-sha; git pull --ff-only
#   3. Запустить conditional_reload.sh — оно решит что перезагружать/пересобирать
#   4. SMOKE-TEST: проверить /health 6 раз с интервалом 5 сек (всего ~30 сек window)
#      — если 5/6 успешных HTTP 200 с healthy=true → deploy ok
#      — иначе → ROLLBACK к prev-sha + повторный conditional_reload + alert
#
# Идемпотентен. Logs through systemd journal (`journalctl -u cognitive-deploy -f`).

set -euo pipefail

REPO_DIR="${COGNITIVE_REPO_DIR:-/opt/cognitive-core}"
BRANCH="${COGNITIVE_DEPLOY_BRANCH:-main}"
HEALTH_URL="${COGNITIVE_HEALTH_URL:-http://localhost:9001/health}"
SMOKE_ATTEMPTS="${COGNITIVE_SMOKE_ATTEMPTS:-6}"
SMOKE_INTERVAL="${COGNITIVE_SMOKE_INTERVAL:-5}"
SMOKE_MIN_OK="${COGNITIVE_SMOKE_MIN_OK:-5}"

log() { echo "[$(date -Iseconds)] $*"; }

cd "$REPO_DIR"

# Не паникуем если git fetch упал по сети — попробуем в следующий тик
if ! git fetch --quiet origin "$BRANCH" 2>&1; then
    log "git fetch failed, will retry next tick" >&2
    exit 0
fi

PREV=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")

if [ "$PREV" = "$NEW" ]; then
    exit 0
fi

log "new commits detected: ${PREV:0:7} -> ${NEW:0:7}"

# Fast-forward only — отказываемся deploy-ить если local diverged
if ! git merge-base --is-ancestor "$PREV" "$NEW"; then
    log "ERROR: local HEAD ${PREV:0:7} is not ancestor of origin/${BRANCH} ${NEW:0:7} — manual intervention required" >&2
    exit 1
fi

git pull --ff-only --quiet origin "$BRANCH"

# Применяем изменения через conditional_reload (forward direction PREV → NEW)
"$REPO_DIR/scripts/conditional_reload.sh" "$PREV" "$NEW"

# ─── SMOKE-TEST ─────────────────────────────────────────────────────────────
# Проверяем что endpoint жив + healthy=true.
# Если non-trivial reload (rebuild api/mcp), даём контейнерам время подняться:
# первый запрос с большим timeout, остальные быстрее.

log "smoke-testing $HEALTH_URL (need ${SMOKE_MIN_OK}/${SMOKE_ATTEMPTS} healthy responses)"

ok_count=0
for i in $(seq 1 "$SMOKE_ATTEMPTS"); do
    timeout=$([ "$i" = 1 ] && echo 25 || echo 8)
    if response=$(curl -sS --max-time "$timeout" -w "\n%{http_code}" "$HEALTH_URL" 2>&1); then
        body=$(echo "$response" | head -n -1)
        code=$(echo "$response" | tail -1)
        if [ "$code" = "200" ] && echo "$body" | grep -q '"healthy":true'; then
            ok_count=$((ok_count + 1))
            log "smoke #${i}/${SMOKE_ATTEMPTS}: ok (${ok_count}/${SMOKE_MIN_OK})"
        else
            log "smoke #${i}/${SMOKE_ATTEMPTS}: bad (code=${code})"
        fi
    else
        log "smoke #${i}/${SMOKE_ATTEMPTS}: connect failed"
    fi

    # Раннее завершение если уже набрали нужное количество
    if [ "$ok_count" -ge "$SMOKE_MIN_OK" ]; then
        break
    fi

    # Между попытками — пауза, кроме последней
    if [ "$i" -lt "$SMOKE_ATTEMPTS" ]; then
        sleep "$SMOKE_INTERVAL"
    fi
done

if [ "$ok_count" -ge "$SMOKE_MIN_OK" ]; then
    log "deploy complete: $NEW (smoke ${ok_count}/${SMOKE_ATTEMPTS} ok)"
    exit 0
fi

# ─── ROLLBACK ────────────────────────────────────────────────────────────────
log "SMOKE FAILED (only ${ok_count}/${SMOKE_ATTEMPTS} healthy). Rolling back ${NEW:0:7} -> ${PREV:0:7}"

if ! git reset --hard --quiet "$PREV" 2>&1; then
    log "FATAL: git reset to $PREV failed — manual recovery required" >&2
    exit 2
fi

# Reverse-direction conditional reload: применяем то же что бы поменялось
# при движении NEW → PREV (сейчас файлы уже как в PREV-state, нужно
# rebuild контейнеров если они менялись). conditional_reload.sh принимает
# (from, to) — тот же diff в обратной направленности тригерит те же
# rebuild-actions для откаченных файлов.
if ! "$REPO_DIR/scripts/conditional_reload.sh" "$NEW" "$PREV"; then
    log "ERROR: rollback conditional_reload failed — service may be in degraded state" >&2
fi

# Финальная проверка после rollback
log "post-rollback smoke-check"
post_ok=0
for i in 1 2 3; do
    if curl -sS --max-time 8 "$HEALTH_URL" 2>&1 | grep -q '"healthy":true'; then
        post_ok=$((post_ok + 1))
    fi
    [ "$i" -lt 3 ] && sleep 5
done

if [ "$post_ok" -ge 2 ]; then
    log "ROLLED BACK successfully to ${PREV:0:7} (post-smoke ${post_ok}/3 ok)"
    # Notification stub — будет заменён на Telegram webhook в задаче 5 sprint
    log "ALERT: deploy ${PREV:0:7}->${NEW:0:7} failed smoke-test, rolled back"
    exit 1
else
    log "FATAL: rollback to ${PREV:0:7} also unhealthy — production in degraded state" >&2
    log "ALERT: full deploy failure, manual intervention required"
    exit 2
fi
