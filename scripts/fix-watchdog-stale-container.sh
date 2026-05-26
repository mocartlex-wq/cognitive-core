#!/usr/bin/env bash
# fix-watchdog-stale-container.sh — удалить из /usr/local/bin/cognitive-watchdog.sh
# проверку контейнера cognitive_mcp, которого нет в production с давнего рефактора.
#
# Симптом до фикса: каждые 5 минут в /var/log/cognitive-alerts.log пишется
# "container cognitive_mcp failed to start". Алерт false-positive, но засоряет
# канал и скрывает реальные алерты (вроде HTTP 500 на /user/connect/claim).
#
# Сделано в discovery-сессии 2026-05-26.
#
# Запуск (на сервере, требует sudo):
#   sudo bash scripts/fix-watchdog-stale-container.sh
#
# Идемпотент: повторный запуск ничего не сделает (sed уже срабоавал).

set -euo pipefail

WATCHDOG="/usr/local/bin/cognitive-watchdog.sh"

if [[ ! -f "$WATCHDOG" ]]; then
    echo "ERROR: $WATCHDOG не найден. Скрипт должен быть установлен в /usr/local/bin/."
    exit 1
fi

# Backup
BACKUP="$WATCHDOG.bak.$(date +%Y%m%d_%H%M%S)"
cp "$WATCHDOG" "$BACKUP"
echo "Backup created: $BACKUP"

# Show current CONTAINERS line
echo ""
echo "BEFORE:"
grep -E "^CONTAINERS=" "$WATCHDOG" || { echo "ERROR: CONTAINERS= line not found, abort"; exit 1; }

# Remove cognitive_mcp from list (с пробелом перед и/или после, чтобы не сломать соседей)
sed -i -E 's/(^CONTAINERS=[^"]*")([^"]*) cognitive_mcp /\1\2 /; s/(^CONTAINERS=[^"]*")([^"]*) cognitive_mcp"/\1\2"/' "$WATCHDOG"

echo ""
echo "AFTER:"
grep -E "^CONTAINERS=" "$WATCHDOG"

# Verify no `cognitive_mcp` reference left in CONTAINERS line
if grep -E "^CONTAINERS=.*cognitive_mcp" "$WATCHDOG" > /dev/null; then
    echo ""
    echo "WARNING: cognitive_mcp всё ещё в CONTAINERS line. Что-то пошло не так."
    echo "Restore: sudo cp $BACKUP $WATCHDOG"
    exit 1
fi

echo ""
echo "OK. Следующий tick watchdog (через 5 мин) уже не будет писать"
echo "  'container cognitive_mcp failed to start'"
echo ""
echo "Проверить через 10 мин:"
echo "  sudo tail -20 /var/log/cognitive-alerts.log"
echo "  (новых записей про cognitive_mcp быть не должно)"
