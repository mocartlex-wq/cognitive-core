#!/usr/bin/env python3
"""Subscription expiry cron — auto-downgrade tier когда период оплаты прошёл.

Запускается daily 03:30 UTC через systemd-timer (cogcore-subscription-cron.timer).

Логика:
  1. SELECT subscriptions WHERE status='active' AND current_period_end < NOW()
  2. Для каждой expired subscription:
     - UPDATE owner_quotas SET tier='free' + apply TIER_LIMITS['free']
     - UPDATE subscriptions SET status='expired'
  3. Лог в /var/log/cognitive-alerts.log для audit-trail
  4. (опц.) cognitive_remember в L1 domain=billing_lifecycle

ВАЖНО: вебхук subscription.deleted ОТДЕЛЬНО handle event immediately
(stripe_provider.handle_event). Этот cron — backup для cases когда:
  - Stripe webhook не доехал (network issue)
  - Manual SQL update пропустил downgrade step
  - Subscription created before billing scaffold deployed

Usage (manual run):
    sudo python3 /opt/cognitive-core/scripts/cogcore-subscription-cron.py --dry-run
    sudo python3 /opt/cognitive-core/scripts/cogcore-subscription-cron.py --apply

systemd:
    sudo systemctl enable --now cogcore-subscription-cron.timer
    sudo systemctl status cogcore-subscription-cron.timer
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.stderr.write("ERROR: psycopg2 не установлен. pip install psycopg2-binary\n")
    sys.exit(1)

# Inline tier limits (avoid app/* import — cron runs outside container).
# Sync с app/services/billing/__init__.py:TIER_LIMITS
TIER_LIMITS = {
    "free":       {"max_events_per_day": 10000,   "max_storage_mb": 1024,    "max_agents": 10,  "max_recall_per_min": 30},
    "pro":        {"max_events_per_day": 100000,  "max_storage_mb": 10240,   "max_agents": 50,  "max_recall_per_min": 100},
    "enterprise": {"max_events_per_day": 1000000, "max_storage_mb": 1048576, "max_agents": 500, "max_recall_per_min": 500},
}

DSN_TEMPLATE = "host=cognitive_postgres port=5432 user=cognitive dbname=cognitive_core password={pwd}"
ENV_PATH = "/opt/cognitive-core/.env"
ALERT_LOG = "/var/log/cognitive-alerts.log"

# Standalone (cron-friendly) logger
logging.basicConfig(
    format="[%(asctime)s] %(levelname)s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("subscription_cron")


def load_pg_password() -> str:
    """Read POSTGRES_PASSWORD from .env (на server-side).

    Fallback на ENV var POSTGRES_PASSWORD если .env недоступен (для local test).
    """
    try:
        for line in Path(ENV_PATH).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("POSTGRES_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        import os
        env_pwd = os.environ.get("POSTGRES_PASSWORD", "")
        if env_pwd:
            return env_pwd
    log.error(".env not found and POSTGRES_PASSWORD env var not set")
    sys.exit(1)


def append_alert(msg: str) -> None:
    """Best-effort log в /var/log/cognitive-alerts.log (видно admin/owner-у)."""
    try:
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] subscription-cron: {msg}\n")
    except Exception as e:
        log.warning("alert log write failed: %s", type(e).__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Реально downgrade'ить (без флага — только подсчёт + лог)")
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()

    pwd = load_pg_password()
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL") or DSN_TEMPLATE.format(pwd=pwd))
    except psycopg2.Error as e:
        log.error("DB connect failed: %s", e)
        return 1
    cur = conn.cursor()

    # Check that billing_processed_events table exists — proxy для «migration 0014 applied»
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'subscriptions'
        );
    """)
    has_subs_table = cur.fetchone()[0]
    if not has_subs_table:
        log.warning("subscriptions table doesn't exist — migration 0014 not applied yet, exiting")
        return 0

    # Find expired active subscriptions
    cur.execute("""
        SELECT id::text, owner_user_id::text, provider, tier,
               current_period_end, external_subscription_id
          FROM subscriptions
         WHERE status = 'active'
           AND current_period_end IS NOT NULL
           AND current_period_end < NOW();
    """)
    expired = cur.fetchall()

    if not expired:
        log.info("No expired active subscriptions found.")
        cur.close()
        conn.close()
        return 0

    log.info("Found %d expired active subscriptions:", len(expired))
    for sub_id, owner, provider, tier, period_end, ext_id in expired[:20]:
        log.info("  sub=%s owner=%s provider=%s tier=%s expired=%s ext_id=%s",
                 sub_id[:8], owner[:8], provider, tier, period_end, ext_id[:24])

    if not args.apply:
        log.info("[DRY-RUN] Запусти с --apply чтобы downgrade'ить %d tenants до free.", len(expired))
        cur.close()
        conn.close()
        return 0

    # APPLY: downgrade каждого
    free_limits = TIER_LIMITS["free"]
    downgraded_count = 0
    for sub_id, owner, provider, tier_old, period_end, ext_id in expired:
        try:
            cur.execute("BEGIN")
            cur.execute("""
                INSERT INTO owner_quotas
                  (owner_user_id, tier, max_events_per_day, max_storage_mb,
                   max_agents, max_recall_per_min)
                VALUES (%s::uuid, 'free', %s, %s, %s, %s)
                ON CONFLICT (owner_user_id) DO UPDATE
                  SET tier = 'free',
                      max_events_per_day = EXCLUDED.max_events_per_day,
                      max_storage_mb = EXCLUDED.max_storage_mb,
                      max_agents = EXCLUDED.max_agents,
                      max_recall_per_min = EXCLUDED.max_recall_per_min;
            """, (owner, free_limits["max_events_per_day"], free_limits["max_storage_mb"],
                  free_limits["max_agents"], free_limits["max_recall_per_min"]))
            cur.execute("""
                UPDATE subscriptions SET status = 'expired', updated_at = NOW()
                 WHERE id = %s::uuid;
            """, (sub_id,))
            cur.execute("COMMIT")
            downgraded_count += 1
            log.info("downgraded owner=%s from tier=%s → free (sub=%s)", owner[:8], tier_old, sub_id[:8])
            append_alert(f"DOWNGRADE owner={owner[:8]} {tier_old}→free (subscription {sub_id[:8]} expired)")
        except psycopg2.Error as e:
            cur.execute("ROLLBACK")
            log.error("FAIL downgrade owner=%s: %s", owner[:8], e)
            append_alert(f"FAIL downgrade owner={owner[:8]}: {type(e).__name__}")

    cur.close()
    conn.close()

    log.info("Done. Downgraded %d/%d expired subscriptions.", downgraded_count, len(expired))
    if downgraded_count > 0:
        append_alert(f"Cron complete: {downgraded_count}/{len(expired)} subscriptions downgraded to free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
