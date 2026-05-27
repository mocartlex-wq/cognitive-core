#!/usr/bin/env python3
"""Cleanup anonymous + empty messages in rooms.

В rooms service до 2026-05-26 был bug в `_read_json()`: при UTF-8 / parsing
ошибке silently возвращался пустой dict → messages сохранялись с
`from_agent='anonymous'` и `text=''`. См. PR fix/mcp-wrappers (A3).

Этот скрипт удаляет все такие «фантомные» сообщения. Безопасно — реальные
сообщения с пустым text никогда не сохраняются (Now validation вернёт 400).

Usage (на сервере):
    sudo python3 /opt/cognitive-core/scripts/cleanup_anonymous_rooms_msgs.py --dry-run
    sudo python3 /opt/cognitive-core/scripts/cleanup_anonymous_rooms_msgs.py --apply

Owner action 2026-05-26: 4 phantom messages в room 3593a2ff-... из утра.
"""
from __future__ import annotations

import argparse
import sys

# psycopg2 (используется cognitive-rooms.py, тот же connection style)
try:
    import psycopg2
except ImportError:
    sys.stderr.write("ERROR: psycopg2 не установлен. Запустите внутри cognitive_postgres-окружения "
                     "или: pip install psycopg2-binary\n")
    sys.exit(1)

DSN = "host=cognitive_postgres port=5432 user=cognitive dbname=cognitive_core password={pwd}"


def load_pwd() -> str:
    """Read POSTGRES_PASSWORD from /opt/cognitive-core/.env."""
    try:
        for line in open("/opt/cognitive-core/.env", encoding="utf-8"):
            line = line.strip()
            if line.startswith("POSTGRES_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        sys.stderr.write("ERROR: /opt/cognitive-core/.env not found. Set POSTGRES_PASSWORD env var manually.\n")
        sys.exit(1)
    sys.stderr.write("ERROR: POSTGRES_PASSWORD not in .env\n")
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Реально удалить (без флага — dry-run, только подсчёт)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Только показать что будет удалено (default)")
    args = parser.parse_args()

    pwd = load_pwd()
    conn = psycopg2.connect(DSN.format(pwd=pwd))
    cur = conn.cursor()

    # Считаем сколько phantom messages
    cur.execute("""
        SELECT room_id, COUNT(*) AS cnt
          FROM room_messages
         WHERE from_agent = 'anonymous'
           AND (text = '' OR text IS NULL)
         GROUP BY room_id
         ORDER BY cnt DESC;
    """)
    rows = cur.fetchall()

    if not rows:
        print("OK: phantom messages не найдены (rooms service уже clean)")
        return 0

    total = sum(r[1] for r in rows)
    print(f"\nНайдено {total} phantom messages в {len(rows)} комнатах:\n")
    for room_id, cnt in rows[:20]:
        print(f"  room_id={room_id} → {cnt} phantom msgs")
    if len(rows) > 20:
        print(f"  ... и ещё {len(rows) - 20} комнат")

    if not args.apply:
        print(f"\n[DRY-RUN] Запусти с --apply чтобы удалить {total} сообщений.")
        cur.close()
        conn.close()
        return 0

    # APPLY mode — реально удаляем
    print(f"\n[APPLY] Удаляю {total} phantom messages...")
    cur.execute("""
        DELETE FROM room_messages
         WHERE from_agent = 'anonymous'
           AND (text = '' OR text IS NULL);
    """)
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f"OK: deleted {deleted} messages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
