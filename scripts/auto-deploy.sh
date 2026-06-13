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
HEALTH_CMD="${COGNITIVE_HEALTH_CMD:-docker exec cognitive_api python -c \"import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://localhost:8000/health',timeout=5).read().decode())\"}"
SMOKE_ATTEMPTS="${COGNITIVE_SMOKE_ATTEMPTS:-6}"
SMOKE_INTERVAL="${COGNITIVE_SMOKE_INTERVAL:-5}"
SMOKE_MIN_OK="${COGNITIVE_SMOKE_MIN_OK:-5}"

# PR #24: source env-файл чтобы получить GITHUB_PAT для git fetch через HTTPS.
# Раньше auto-deploy полагался на /root/.ssh/github_cognitive_deploy который
# не существует → git fetch падал → ничего не деплоилось. Теперь fetch
# идёт через PAT, git remote остаётся SSH (для operator git push).
if [ -f /etc/cognitive-deploy.env ]; then
    # shellcheck source=/dev/null
    set -a; . /etc/cognitive-deploy.env; set +a
fi
REPO_HTTPS_URL="${COGNITIVE_GIT_HTTPS_URL:-}"
if [ -z "$REPO_HTTPS_URL" ] && [ -n "${GITHUB_PAT:-}" ]; then
    REPO_HTTPS_URL="https://${GITHUB_PAT}@github.com/mocartlex-wq/cognitive-core.git"
fi

log() { echo "[$(date -Iseconds)] $*"; }

# Telegram-alert helper: silent if TELEGRAM_BOT_TOKEN/CHAT_ID не заданы.
# Set in /etc/cognitive-deploy.env or systemd unit Environment= directives.
notify() {
    local msg="$1"
    log "ALERT: $msg"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        # send as single message; trim to Telegram's 4096 char limit
        local body
        body=$(printf '🚨 cognitive-core deploy\n\n%s' "$msg" | head -c 4000)
        curl -sS --max-time 6 \
            -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${body}" \
            >/dev/null 2>&1 \
            && log "telegram notified" \
            || log "telegram notify failed (non-fatal)"
    fi
}

cd "$REPO_DIR"

# Diverge guard + self-heal (DS+ai-crm-deploy peer-review 2026-05-08, upgraded
# 2026-05-26):
#
# Если working tree модифицирован вручную (sed/cp/edit-on-server), git pull --ff-only
# падает. Раньше — слепой abort. Теперь:
#   1. Fetch origin first (нужен актуальный origin/$BRANCH для сравнения)
#   2. Если working tree dirty НО `git diff origin/$BRANCH` пусто — это safe-state:
#      контент уже совпадает с тем что будет после pull, только index расходится.
#      Лечится `git checkout -- .` + `git clean -fd` для untracked которые
#      присутствуют в origin (но git их видит как ?? потому что HEAD старый).
#   3. Если diff vs origin/$BRANCH НЕ пустой — реальная дивергенция, аборт + alert.
#
# Это разблокирует случай когда раньше owner делал runtime-edit, потом то же
# самое попало в PR через GitHub. Без self-heal — server stuck forever.

# Pre-fetch чтобы знать origin/$BRANCH
if [ -n "$REPO_HTTPS_URL" ]; then
    git fetch --quiet "$REPO_HTTPS_URL" "$BRANCH" 2>&1 \
        | sed -E "s|https://[^@]+@|https://***@|g" || true
    git update-ref "refs/remotes/origin/$BRANCH" FETCH_HEAD 2>/dev/null || true
else
    git fetch --quiet origin "$BRANCH" 2>&1 || true
fi

# ai-crm.conf is a foreign nginx config continuously rewritten by an external tool
# (davsync/dynup); it is NOT part of cognitive-core and diverges from origin permanently.
# Exclude it from the dirty check so this guard stops false-aborting every deploy tick.
if ! git diff-index --quiet HEAD -- "." ":(exclude)nginx/conf.d/ai-crm.conf" 2>/dev/null; then
    # Working tree dirty. Check if content matches origin/$BRANCH (safe self-heal case)
    if git diff --quiet "origin/$BRANCH" -- . 2>/dev/null; then
        log "dirty index but content matches origin/$BRANCH — self-healing via checkout + clean"
        git checkout --quiet -- . 2>/dev/null || true
        # Удаляем untracked которые есть в origin/$BRANCH (старые runtime-installed файлы)
        # `git clean -fd` уберёт всё untracked — но только в файлах под git control,
        # не в /var/log или внешних путях. Safe для git repo.
        git clean -fd 2>/dev/null || true
        if git diff-index --quiet HEAD 2>/dev/null; then
            log "self-healed: working tree clean now"
            rm -f /var/run/cognitive/deploy-dirty.alerted 2>/dev/null
        else
            log "WARNING: self-heal не полностью сработал, продолжаем но pull может упасть"
        fi
    else
        DIRTY_FILES=$(git status --short 2>/dev/null | head -5 | tr '\n' '|')
        log "ABORT: working tree dirty AND diverged from origin/$BRANCH"
        log "dirty files: $DIRTY_FILES"
        log "to investigate: git diff origin/$BRANCH"
        log "to force-fix:   sudo git reset --hard origin/$BRANCH && sudo git clean -fd"
        SENTINEL=/var/run/cognitive/deploy-dirty.alerted
        if [ ! -f "$SENTINEL" ] || [ $(( $(date +%s) - $(stat -c %Y "$SENTINEL" 2>/dev/null || echo 0) )) -gt 3600 ]; then
            /usr/local/bin/cognitive-notify.sh "auto-deploy: server tree DIRTY+DIVERGED, manual fix needed. Files: $DIRTY_FILES" 2>/dev/null
            touch "$SENTINEL"
        fi
        exit 0
    fi
fi
rm -f /var/run/cognitive/deploy-dirty.alerted 2>/dev/null

# Net-safe: убеждаемся что fetch выше реально успел. Если нет — exit и retry.
# (Pre-fetch для dirty-self-heal уже произошёл; если упал — origin/$BRANCH ref
# мог остаться stale, тогда PREV != NEW даст fake-diff. Safer: re-verify.)
if ! git rev-parse "origin/$BRANCH" >/dev/null 2>&1; then
    log "origin/$BRANCH ref missing после fetch — пропускаем тик, retry next" >&2
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

# git pull --ff-only --quiet может пытаться SSH-fetch — используем
# уже скачанный FETCH_HEAD через merge --ff-only.
git merge --ff-only --quiet "$NEW" 2>/dev/null || git pull --ff-only --quiet origin "$BRANCH"

# [skip-deploy] аварийный рычаг: если subject HEAD-коммита содержит "[skip-deploy]",
# код приземляется в серверный checkout (ff-merge выше), но conditional_reload +
# smoke пропускаем — то есть в runtime НЕ выкатываем. Следующий обычный коммит
# выкатит всё накопленное. Использовать для паузы выкатки конкретного изменения.
if git log -1 --format=%s "$NEW" | grep -qF '[skip-deploy]'; then
    log "[skip-deploy] marker on ${NEW:0:7} — merged into checkout, skipping reload + smoke"
    exit 0
fi

# Применяем изменения через conditional_reload (forward direction PREV → NEW)
"$REPO_DIR/scripts/conditional_reload.sh" "$PREV" "$NEW"

# Smoke-test нужен только если поменялся application код или infra (compose).
# Изменения в scripts/auto-deploy*, conditional_reload*, deploy/*, *.md, docs/*
# не влияют на runtime — smoke-тест бесполезен и рискует ложным rollback'ом
# (если сам skoke-скрипт буггован, он откатит свой же fix, рекурсивный лок).
APP_CHANGED=$(git diff --name-only "$PREV" "$NEW" | grep -vE '^(scripts/(auto-deploy|conditional_reload)\.sh$|deploy/|.*\.md$|docs/|CHANGELOG|README|\.gitattributes|\.gitignore)' || true)

if [ -z "$APP_CHANGED" ]; then
    log "deploy-infra/docs only — skipping smoke-test"
    log "deploy complete: $NEW (no smoke needed)"
    exit 0
fi

log "app/infra files changed: $(echo "$APP_CHANGED" | head -3 | tr '\n' ' ')..."

# ─── SMOKE-TEST ─────────────────────────────────────────────────────────────
# Проверяем что endpoint жив + healthy=true.
# Если non-trivial reload (rebuild api/mcp), даём контейнерам время подняться:
# первый запрос с большим timeout, остальные быстрее.

log "smoke-testing via [$HEALTH_CMD] (need ${SMOKE_MIN_OK}/${SMOKE_ATTEMPTS} healthy responses)"

ok_count=0
# При первой попытке ждём чуть дольше — даём контейнеру время после rebuild
[ "$SMOKE_ATTEMPTS" -gt 0 ] && sleep 3

for i in $(seq 1 "$SMOKE_ATTEMPTS"); do
    if body=$(eval "$HEALTH_CMD" 2>/dev/null); then
        if echo "$body" | grep -q '"healthy":true'; then
            ok_count=$((ok_count + 1))
            log "smoke #${i}/${SMOKE_ATTEMPTS}: ok (${ok_count}/${SMOKE_MIN_OK})"
        else
            log "smoke #${i}/${SMOKE_ATTEMPTS}: bad response"
        fi
    else
        log "smoke #${i}/${SMOKE_ATTEMPTS}: probe failed"
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
    if eval "$HEALTH_CMD" 2>/dev/null | grep -q '"healthy":true'; then
        post_ok=$((post_ok + 1))
    fi
    [ "$i" -lt 3 ] && sleep 5
done

if [ "$post_ok" -ge 2 ]; then
    log "ROLLED BACK successfully to ${PREV:0:7} (post-smoke ${post_ok}/3 ok)"
    notify "Deploy ${NEW:0:7} failed smoke-test, auto-rolled back to ${PREV:0:7}. Service is healthy on previous version."
    exit 1
else
    log "FATAL: rollback to ${PREV:0:7} also unhealthy — production in degraded state" >&2
    notify "FULL DEPLOY FAILURE: ${PREV:0:7}->${NEW:0:7} broken AND rollback to ${PREV:0:7} also unhealthy. Manual intervention required."
    exit 2
fi
