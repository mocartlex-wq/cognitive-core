from fastapi import HTTPException, Request

from app.config import settings
from app.db.postgres import get_pool
from app.db.redis import get_redis
from app.security.key_hash import compute_key_hmac, is_key_hashing_enabled


async def verify_api_key(request: Request) -> str:
    """Validate X-API-Key, return agent_id. 401 if invalid.

    Two-tier lookup:
      1. agent_keys table (per-agent issued via POST /agents/register)
      2. Legacy settings.agent_api_keys env (backwards compat for old clients)

    When COGCORE_KEY_LOOKUP_SECRET is configured (see app/security/key_hash.py),
    Tier-1 lookup checks BOTH the new api_key_hmac column and the legacy
    plaintext column. This lets a deployment roll out hashed lookups gradually
    — backfill populates the new column; un-backfilled rows still match via
    the plaintext path until the owner removes that column in a later migration.
    """
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")

    # Tier 1: per-agent keys in DB (sprint v0.5.0-prod #3)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await _lookup_agent_key(conn, api_key)
            if row:
                # Update last_used_at (fire-and-forget — no need to await separately)
                await conn.execute(
                    "UPDATE agent_keys SET last_used_at = NOW() WHERE api_key = $1",
                    api_key,
                )
                request.state.agent_id = row["agent_id"]
                return row["agent_id"]
    except HTTPException:
        raise
    except Exception:
        # If DB unavailable, fall through to legacy env-keys (don't block during outage)
        pass

    # Tier 2: legacy env-defined keys (kept for ai-crm handoff and existing clients)
    agent_keys = settings.get_agent_keys()
    for aid, key in agent_keys.items():
        if key == api_key:
            request.state.agent_id = aid
            return aid

    raise HTTPException(status_code=401, detail="Invalid API key")


async def _lookup_agent_key(conn, api_key: str):
    """Look up an agent_keys row by either HMAC hash (when configured) or
    the plaintext column. Returns the row dict-like or None.

    The SQL guards against a missing api_key_hmac column on stock schemas
    that haven't run the (future) migration yet — falls back to a
    plaintext-only query in that case."""
    if is_key_hashing_enabled():
        key_hmac = compute_key_hmac(api_key)
        try:
            return await conn.fetchrow(
                """
                SELECT agent_id FROM agent_keys
                WHERE revoked_at IS NULL
                  AND (api_key_hmac = $2 OR api_key = $1)
                LIMIT 1
                """,
                api_key, key_hmac,
            )
        except Exception:
            # Column not yet present on this deployment — fall through.
            pass
    return await conn.fetchrow(
        """
        SELECT agent_id FROM agent_keys
        WHERE api_key = $1 AND revoked_at IS NULL
        """,
        api_key,
    )


async def check_rate_limit(agent_id: str) -> bool:
    """Возвращает True если лимит не превышен. 429 если превышен."""
    r = await get_redis()
    key = f"rate:{agent_id}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 1)
    if count > settings.rate_limit_per_agent:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    return True
