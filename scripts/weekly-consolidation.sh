#!/usr/bin/env bash
# Cognitive Core — L2→L3 weekly из systemd (cogcore-weekly.timer, ежедневно 05:00 UTC).
#
# Зачем, если weekly уже есть в app/worker.py scheduler_loop:
#   1. scheduler_loop живёт в процессе api и ждёт понедельника 03:00 UTC; рестарт
#      контейнера в неудачный час — и неделя пропущена (та же история, что с daily
#      до 2026-07-06, см. nightly T8).
#   2. 02.11.2026 куратор (DeepSeek) лежал весь понедельничный прогон: все домены
#      упали, retry-очередь осталась пустой, следующий шанс — через неделю.
#
# Два шага, оба идемпотентны:
#   - run_weekly_cycle_if_due: полный прогон, ТОЛЬКО если последний полный
#     weekly_consolidate в аудите старше 5 дней (иначе буферы ушли бы в LLM дважды);
#   - run_weekly_retry_cycle: добрать домены из retry-очереди, если она не пуста.
set -u
LOGDIR=/var/log/cogcore
LOGFILE=$LOGDIR/weekly.log
mkdir -p "$LOGDIR"
[ -f "$LOGFILE" ] || touch "$LOGFILE"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" | tee -a "$LOGFILE"; }

MAX_AGE_DAYS="${WEEKLY_MAX_AGE_DAYS:-5}"
log "weekly start (max_age_days=$MAX_AGE_DAYS)"

OUT=$(docker exec cognitive_api python -c "
import asyncio, json
from app.worker import run_weekly_cycle_if_due, run_weekly_retry_cycle
async def main():
    full = await run_weekly_cycle_if_due(max_age_days=$MAX_AGE_DAYS)
    retry = await run_weekly_retry_cycle()
    failed = [r['domain'] for r in retry if 'error' in r]
    return {'full': full, 'retry_domains': len(retry), 'retry_failed': failed}
print(json.dumps(asyncio.run(main()), ensure_ascii=False)[:600])
" 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
  log "FAIL rc=$RC: ${OUT:0:400}"
  exit 1
fi
log "result: ${OUT:0:600}"
exit 0
