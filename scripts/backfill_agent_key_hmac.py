#!/usr/bin/env python3
"""One-shot backfill: populate agent_keys.api_key_hmac for existing rows.

Prerequisites:
  1. Alembic 0018 applied (column + index exist).
  2. COGCORE_KEY_LOOKUP_SECRET set in the runtime env / .env
     (≥ 32 random bytes; do NOT rotate later without a full key re-issue).

Idempotent: re-running on a partially-backfilled table only touches rows
whose api_key_hmac is still NULL. Safe to run while traffic is live —
verify_api_key already accepts BOTH lookup paths.

Usage:
  COGCORE_KEY_LOOKUP_SECRET=... DATABASE_URL=postgresql://... \\
    python scripts/backfill_agent_key_hmac.py [--dry-run]

Returns a summary dict on stdout and a non-zero exit code on hard failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg

# Make `app` importable so we reuse the production HMAC helper. Mounting the
# repo root onto sys.path keeps the script self-contained — no PYTHONPATH gym.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.security.key_hash import compute_key_hmac, is_key_hashing_enabled


async def backfill(dry_run: bool = False) -> dict:
    if not is_key_hashing_enabled():
        return {
            "status": "skipped",
            "reason": "COGCORE_KEY_LOOKUP_SECRET not set in env",
        }

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("COGCORE_DATABASE_URL")
    if not dsn:
        return {
            "status": "skipped",
            "reason": "DATABASE_URL not set in env",
        }

    started = datetime.now(timezone.utc)
    conn = await asyncpg.connect(dsn)
    try:
        # Берём только то, что ещё не размечено и не отозвано (отозванные
        # ключи не нужно индексировать — они никогда не лукапятся).
        rows = await conn.fetch(
            """
            SELECT api_key
            FROM agent_keys
            WHERE api_key_hmac IS NULL AND revoked_at IS NULL
            """
        )
        total = len(rows)
        if total == 0:
            return {
                "status": "ok",
                "rows_total": 0,
                "rows_updated": 0,
                "dry_run": dry_run,
                "duration_s": 0.0,
            }

        updated = 0
        if not dry_run:
            async with conn.transaction():
                for r in rows:
                    api_key = r["api_key"]
                    h = compute_key_hmac(api_key)
                    if h is None:
                        # Secret пропал между check и use — прерываем без частичного
                        # коммита (транзакция откатится).
                        raise RuntimeError("compute_key_hmac returned None mid-flight")
                    await conn.execute(
                        "UPDATE agent_keys SET api_key_hmac = $1 WHERE api_key = $2",
                        h, api_key,
                    )
                    updated += 1

        duration_s = (datetime.now(timezone.utc) - started).total_seconds()
        return {
            "status": "ok",
            "rows_total": total,
            "rows_updated": updated,
            "dry_run": dry_run,
            "duration_s": round(duration_s, 2),
        }
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Не применять UPDATE, только посчитать сколько строк затронуто.")
    args = ap.parse_args()
    result = asyncio.run(backfill(dry_run=args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
