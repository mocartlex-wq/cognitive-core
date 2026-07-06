#!/bin/bash
# Nightly health suite — runs every 4 hours via cogcore-nightly.timer.
# Checks: production health, orchestrator alive, all provider URLs reachable,
# media pipeline smoke test, cert expiry warnings.
# Outputs to /var/log/cogcore/nightly.log + L1 event domain=nightly_alerts if FAIL.
#
# Exit codes:
#   0 — all PASS or only WARNs (non-blocking issues)
#   1 — at least one FAIL (something needs attention)
#
# Created: 2026-05-24 (quality audit night-pass)

set -u
LOGDIR=/var/log/cogcore
LOGFILE=$LOGDIR/nightly.log
sudo mkdir -p "$LOGDIR"
sudo chmod 755 "$LOGDIR"
[ -f "$LOGFILE" ] || sudo touch "$LOGFILE"
sudo chmod 644 "$LOGFILE"

PASS=0
WARN=0
FAIL=0
FAIL_DETAILS=""
WARN_DETAILS=""

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() {
  local line="[$(ts)] $*"
  echo "$line" | sudo tee -a "$LOGFILE" >/dev/null
  echo "$line"
}
pass() { PASS=$((PASS+1)); log "PASS  $1: $2"; }
warn() { WARN=$((WARN+1)); WARN_DETAILS="$WARN_DETAILS\n$1: $2"; log "WARN  $1: $2"; }
fail() { FAIL=$((FAIL+1)); FAIL_DETAILS="$FAIL_DETAILS\n$1: $2"; log "FAIL  $1: $2"; }
# info() — informational, not counted as pass/warn/fail. Используется для
# may-blocked провайдеров (РФ VPS не доходит, но tenants outside РФ работают).
info() { log "INFO  $1: $2"; }

log "════════ nightly-health-suite start ════════"

# ─── T1: /health HTTP 200 + healthy=true ───────────────────────
BODY=$(curl -s --max-time 10 https://mcp.me-ai.ru/health 2>/dev/null)
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://mcp.me-ai.ru/health 2>/dev/null)
if [ "$CODE" = "200" ] && echo "$BODY" | grep -q '"healthy":true'; then
  VER=$(echo "$BODY" | python3 -c "import sys,json;print(json.load(sys.stdin).get('version','?'))" 2>/dev/null)
  pass T1 "/health 200, version=$VER"
else
  fail T1 "/health http=$CODE body=${BODY:0:120}"
fi

# ─── T2: orchestrator daemon running ───────────────────────────
if sudo systemctl is-active cogcore-orchestrator >/dev/null 2>&1; then
  pass T2 "cogcore-orchestrator.service active"
else
  STATUS=$(sudo systemctl is-active cogcore-orchestrator 2>&1)
  fail T2 "orchestrator daemon status=$STATUS"
fi

# ─── T3: provider URLs reachable (WARN not FAIL — vendors throttle HEAD) ────
# Категоризируем: MUST-pass (доступны из РФ напрямую) vs MAY-BLOCKED (часто
# таймаутят с РФ VPS egress из-за региональных блокировок или CloudFlare).
# MAY-BLOCKED → log как INFO, не WARN — иначе nightly постоянно alerts впустую.
declare -A PROVIDERS_DIRECT=(
  ["openrouter"]="https://openrouter.ai/"
  ["openai"]="https://platform.openai.com/"
  ["gemini"]="https://aistudio.google.com/"
)
declare -A PROVIDERS_MAY_BLOCKED=(
  ["minimax"]="https://platform.minimax.io/"
  ["sber"]="https://developers.sber.ru/studio"
  ["claude"]="https://platform.claude.com/"
)
T3_DIRECT_DOWN=0
T3_BLOCKED_DOWN=0
for name in "${!PROVIDERS_DIRECT[@]}"; do
  url="${PROVIDERS_DIRECT[$name]}"
  code=$(curl -s -L -o /dev/null --max-time 8 -A "Mozilla/5.0 (cogcore-nightly)" \
    -w "%{http_code}" "$url" 2>/dev/null)
  case "$code" in
    200|301|302|307|308|403) ;;
    *) T3_DIRECT_DOWN=$((T3_DIRECT_DOWN+1)); warn T3 "$name (must-pass) unreachable (HTTP $code at $url)" ;;
  esac
done
for name in "${!PROVIDERS_MAY_BLOCKED[@]}"; do
  url="${PROVIDERS_MAY_BLOCKED[$name]}"
  code=$(curl -s -L -o /dev/null --max-time 8 -A "Mozilla/5.0 (cogcore-nightly)" \
    -w "%{http_code}" "$url" 2>/dev/null)
  case "$code" in
    200|301|302|307|308|403) ;;
    *) T3_BLOCKED_DOWN=$((T3_BLOCKED_DOWN+1)); info T3 "$name unreachable from RU VPS (HTTP $code) — provider may still work for tenants outside RU" ;;
  esac
done
if [ "$T3_DIRECT_DOWN" -eq 0 ]; then
  pass T3 "all direct provider URLs reachable ($T3_BLOCKED_DOWN may-blocked info-only)"
fi

# ─── T4: MinIO media-frames bucket alive ───────────────────────
BUCKET_CHECK=$(sudo docker exec cognitive_api python -c "
from app.db.s3 import get_s3
try:
    c = get_s3()
    for b in c.list_buckets():
        if b.name == 'media-frames':
            print('ok')
            break
    else:
        print('missing')
except Exception as e:
    print('err:'+str(e)[:80])
" 2>&1 | tail -1)
if [ "$BUCKET_CHECK" = "ok" ]; then
  pass T4 "MinIO media-frames bucket present"
else
  fail T4 "media-frames bucket check: $BUCKET_CHECK"
fi

# ─── T5: cert expiry > 7 days ──────────────────────────────────
T5_BAD=0
for host in mcp.me-ai.ru git.me-ai.ru mcp.xn----8sbwawqx4fza.xn--p1ai; do
  NOT_AFTER=$(echo | openssl s_client -servername "$host" -connect "$host:443" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  if [ -z "$NOT_AFTER" ]; then
    warn T5 "cannot read cert for $host"
    T5_BAD=$((T5_BAD+1)); continue
  fi
  END_EPOCH=$(date -d "$NOT_AFTER" +%s 2>/dev/null)
  NOW_EPOCH=$(date +%s)
  DAYS_LEFT=$(( (END_EPOCH - NOW_EPOCH) / 86400 ))
  if [ "$DAYS_LEFT" -lt 7 ]; then
    fail T5 "$host cert expires in $DAYS_LEFT days"
    T5_BAD=$((T5_BAD+1))
  elif [ "$DAYS_LEFT" -lt 30 ]; then
    warn T5 "$host cert expires in $DAYS_LEFT days (renewal due)"
    T5_BAD=$((T5_BAD+1))
  fi
done
if [ "$T5_BAD" -eq 0 ]; then
  pass T5 "all certs >30 days remaining"
fi

# ─── T6: disk usage threshold ──────────────────────────────────
T6_BAD=0
for mount in / /mnt/cold-storage; do
  USE=$(df -h "$mount" 2>/dev/null | awk 'NR==2{gsub("%","",$5);print $5}')
  if [ -z "$USE" ]; then
    warn T6 "cannot read disk for $mount"; T6_BAD=$((T6_BAD+1)); continue
  fi
  if [ "$USE" -ge 90 ]; then
    fail T6 "$mount $USE% used (critical)"
    T6_BAD=$((T6_BAD+1))
  elif [ "$USE" -ge 85 ]; then
    warn T6 "$mount $USE% used (warning)"
    T6_BAD=$((T6_BAD+1))
  fi
done
if [ "$T6_BAD" -eq 0 ]; then
  pass T6 "disk usage under 85%"
fi

# ─── T7: orchestrator processed messages in last 4h ────────────
COUNT=$(sudo docker exec cognitive_postgres psql -U cognitive -d cognitive_core -tA -c \
  "SELECT COUNT(*) FROM l1_raw_events WHERE domain='agent_inbox' AND raw_payload->>'from'='orchestrator' AND timestamp > NOW() - INTERVAL '4 hours'" \
  2>/dev/null | tr -d '[:space:]')
if [ -z "$COUNT" ]; then
  warn T7 "could not query L1 (postgres down?)"
elif [ "$COUNT" = "0" ]; then
  warn T7 "orchestrator sent 0 messages in last 4h (idle or stuck?)"
else
  pass T7 "orchestrator processed $COUNT messages in last 4h"
fi

# ─── Consolidation trigger (RELIABILITY FIX 2026-07-06) ──────────
# The in-app scheduler (app/worker.py scheduler_loop) fires daily/weekly
# consolidation only ONCE per api restart, then never on its 02:00 schedule
# (proven: daily_consolidate audit entries cluster only at restart times, with
# multi-day gaps — e.g. 2026-06-14 → 06-27 had zero runs). That silently starved
# L1→L2 consolidation. This rock-solid 4h systemd timer now drives it instead.
# In-container invocation carries full app context; the consolidator's advisory
# lock makes it safe against the in-app scheduler / concurrent runs. Non-blocking:
# a consolidation hiccup must not fail the health suite.
DAILY_OUT=$(sudo docker exec cognitive_api python -c \
  "import asyncio,json;from app.worker import run_daily_cycle;print(json.dumps(asyncio.run(run_daily_cycle()))[:200])" 2>&1) \
  && pass T8 "daily consolidation triggered: ${DAILY_OUT:0:120}" \
  || warn T8 "daily consolidation trigger failed: ${DAILY_OUT:0:160}"

# Weekly L2→L3 synthesis — heavier (LLM per domain), so once per week: Monday,
# only in the first 4h slot (00:00–03:59 UTC) to avoid 6× runs across the day.
if [ "$(date -u +%u)" = "1" ] && [ "$(date -u +%H)" -lt 4 ]; then
  WEEKLY_OUT=$(sudo docker exec cognitive_api python -c \
    "import asyncio,json;from app.worker import run_weekly_cycle;print(json.dumps(asyncio.run(run_weekly_cycle()))[:200])" 2>&1) \
    && pass T9 "weekly consolidation triggered: ${WEEKLY_OUT:0:120}" \
    || warn T9 "weekly consolidation trigger failed: ${WEEKLY_OUT:0:160}"
fi

# ─── Summary ────────────────────────────────────────────────────
log "════════ nightly suite done: PASS=$PASS WARN=$WARN FAIL=$FAIL ════════"

# ─── Alert sink: L1 event + Telegram (if configured + reachable) ──
if [ "$FAIL" -gt 0 ] || [ "$WARN" -gt 0 ]; then
  PAYLOAD=$(python3 -c "
import json,sys
print(json.dumps({
  'severity': 'fail' if $FAIL > 0 else 'warn',
  'fail_count': $FAIL,
  'warn_count': $WARN,
  'pass_count': $PASS,
  'fail_details': '''$FAIL_DETAILS'''.strip(),
  'warn_details': '''$WARN_DETAILS'''.strip(),
  'ts': '$(ts)',
}))
")
  # Write to L1 (best-effort, swallow errors)
  sudo docker exec -i cognitive_postgres psql -U cognitive -d cognitive_core >/dev/null 2>&1 <<SQL || true
INSERT INTO l1_raw_events (agent_id, domain, raw_payload, timestamp)
VALUES ('nightly-suite', 'nightly_alerts', '${PAYLOAD//\'/\'\'}'::jsonb, NOW());
SQL

  # Telegram (best-effort — known to be RKN-blocked from RU)
  TG_TOKEN=$(sudo grep '^TELEGRAM_BOT_TOKEN=' /etc/cognitive-deploy.env 2>/dev/null | cut -d= -f2)
  TG_CHAT=$(sudo grep '^TELEGRAM_CHAT_ID=' /etc/cognitive-deploy.env 2>/dev/null | cut -d= -f2)
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    MSG="🌙 nightly suite: FAIL=$FAIL WARN=$WARN PASS=$PASS"
    if [ "$FAIL" -gt 0 ]; then
      MSG="$MSG\n\nFAIL:$FAIL_DETAILS"
    fi
    curl -s --max-time 5 -X POST "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
      -d "chat_id=$TG_CHAT" -d "text=$MSG" -d "parse_mode=Markdown" >/dev/null 2>&1 || true
  fi
fi

[ "$FAIL" -gt 0 ] && exit 1
exit 0
