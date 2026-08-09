#!/bin/bash
# Backfill L1→L2: догоняет хвост событий, выпавших из суточного окна.
#
# Живёт ОТДЕЛЬНО от nightly-health-suite намеренно. Сначала backfill был шагом
# T8b внутри health-suite, и это его убивало: у suite TimeoutStartSec=300, а
# одна только daily-консолидация занимает 4+ минуты LLM — backfill стартовал за
# 40 секунд до SIGTERM и не отработал НИ РАЗУ (2026-08-09). Снаружи выглядело
# исправным: T8 успевал записаться в лог.
#
# Здоровье-проверки должны быть быстрыми, LLM-работа — иметь свой бюджет
# времени. Отсюда отдельный юнит с TimeoutStartSec=1500.
#
# Расписание — каждые 4 часа со сдвигом на 2 часа от nightly, чтобы два
# LLM-тяжёлых прогона не спорили за advisory lock и за квоту провайдера.

set -u
LOGDIR=/var/log/cogcore
LOGFILE=$LOGDIR/backfill.log
mkdir -p "$LOGDIR"
[ -f "$LOGFILE" ] || touch "$LOGFILE"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*" | tee -a "$LOGFILE"; }

# Порции держим небольшими: очередь тает ступенями за несколько прогонов, зато
# один прогон не выжигает LLM-бюджет и укладывается в таймаут.
MAX_DOMAINS="${BACKFILL_MAX_DOMAINS:-3}"
MAX_EVENTS="${BACKFILL_MAX_EVENTS:-40}"

log "backfill start (max_domains=$MAX_DOMAINS max_events_per_domain=$MAX_EVENTS)"

OUT=$(docker exec cognitive_api python -c "
import asyncio, json
from app.services.consolidator import daily_consolidate
r = asyncio.run(daily_consolidate(backfill=True, max_domains=$MAX_DOMAINS, max_events_per_domain=$MAX_EVENTS))
print(json.dumps(r, ensure_ascii=False)[:600])
" 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
  log "FAIL rc=$RC: ${OUT:0:400}"
  exit 1
fi
log "result: ${OUT:0:400}"

# Остаток очереди — видно, тает ли она от прогона к прогону.
LEFT=$(docker exec cognitive_postgres psql -U cognitive -d cognitive_core -tA \
  -c "SELECT COUNT(*) FROM l1_raw_events WHERE processed_to_l2 = FALSE" 2>/dev/null | tr -d '[:space:]')
log "backlog_remaining=${LEFT:-unknown}"
exit 0
