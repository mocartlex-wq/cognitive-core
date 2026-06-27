"""Deterministic HMAC-based lookup hash for API keys.

Why HMAC and not Argon2 for the lookup column?
  Lookup queries need: `WHERE api_key_hmac = $1`. That requires a
  deterministic mapping from plaintext key → fixed bytes — every row
  hashed the same way for a given input. Argon2 includes a per-row
  salt by design, so you'd have to SELECT all rows and verify one by
  one (O(n) per request). HMAC-SHA256 with a server-side secret gives
  a fixed mapping, blocks pre-computation by attackers who don't have
  the secret, and lets the DB index the column.

Threat model:
  - Database dump or SQL-injection-read of agent_keys leaks HMACs;
    they're useless without COGCORE_KEY_LOOKUP_SECRET.
  - Server compromise that leaks the secret degrades to plaintext-
    equivalence — no worse than today.
  - Plaintext column stays during transition for backwards compat
    with rows that have not been backfilled yet.

Deployment plan (NOT executed here; owner runs separately):
  1. Set COGCORE_KEY_LOOKUP_SECRET in /opt/cognitive-core/.env
     (random ≥ 32 bytes; once set, never rotate without a full key
     re-issue).
  2. Apply alembic migration that adds api_key_hmac VARCHAR(64).
  3. Run backfill: UPDATE agent_keys SET api_key_hmac =
     compute_key_hmac(api_key) WHERE api_key_hmac IS NULL.
  4. Verify lookups still work (both code paths active by design).
  5. After confidence period, deprecate plaintext column in a
     follow-up migration.

Until step 1 is done, this module returns None and callers must fall
back to the plaintext path. Nothing else in the system changes.
"""
from __future__ import annotations

import hashlib
import hmac
import os


_SECRET_ENV = "COGCORE_KEY_LOOKUP_SECRET"


def get_key_lookup_secret() -> bytes | None:
    """Return the server-side HMAC secret, or None if not configured."""
    raw = os.environ.get(_SECRET_ENV, "").strip()
    if not raw:
        return None
    return raw.encode("utf-8")


def is_key_hashing_enabled() -> bool:
    """True iff the deployment has configured a lookup secret."""
    return get_key_lookup_secret() is not None


def compute_key_hmac(api_key: str) -> str | None:
    """HMAC-SHA256 of `api_key` keyed by COGCORE_KEY_LOOKUP_SECRET.

    Returns hex digest (64 chars). Returns None if the secret env var
    is not set — callers must then fall back to plaintext lookup.

    Stable across processes given the same secret; identical input →
    identical output, so the column can be indexed for direct lookup.
    """
    if not api_key:
        return None
    secret = get_key_lookup_secret()
    if secret is None:
        return None
    return hmac.new(secret, api_key.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_key_against_hmac(api_key: str, stored_hmac: str | None) -> bool:
    """Constant-time check that `api_key` matches a stored HMAC."""
    if not stored_hmac:
        return False
    computed = compute_key_hmac(api_key)
    if computed is None:
        return False
    return hmac.compare_digest(computed, stored_hmac)
