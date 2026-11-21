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
# systemd already appends stdout to the log. Mirror it only for manual runs.
if [[ ! /dev/stdout -ef "$LOGFILE" ]]; then
  exec > >(tee -a "$LOGFILE") 2>&1
fi

MAX_AGE_DAYS="${WEEKLY_MAX_AGE_DAYS:-5}"
echo "[$(ts)] weekly start (max_age_days=$MAX_AGE_DAYS)"

docker exec -i cognitive_api python - "$MAX_AGE_DAYS" <<'PY'
import asyncio
import json
import sys

from app.worker import weekly_backstop_report

report = asyncio.run(weekly_backstop_report(int(sys.argv[1])))
print(json.dumps(report, ensure_ascii=False))
sys.exit(1 if report["unresolved"] else 0)
PY
