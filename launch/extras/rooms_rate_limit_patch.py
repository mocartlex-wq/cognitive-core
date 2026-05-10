"""
Lightweight in-process rate limiter for the rooms service.

Drop-in: import and use as a FastAPI dependency. Keeps a sliding window per
(api_key, ip) tuple in Redis (cheap, atomic) — no external dependency beyond
the existing redis container.

Why not just nginx?
  * `make up` (no edge profile) skips nginx — but rooms is still exposed.
  * Some attackers control the X-Room-Key (legitimate compromised key) —
    nginx limit per-key still helps but adding per-IP at the app layer gives
    defence-in-depth without operator config.

Usage in cognitive-rooms.py
---------------------------
    from rooms_rate_limit_patch import RateLimiter
    rl = RateLimiter(redis_url="redis://redis:6379", per_minute=120, burst=40)

    @app.middleware("http")
    async def rate_limit_mw(request, call_next):
        try:
            await rl.check(request)
        except RateLimitExceeded as e:
            return JSONResponse(
                {"error": "rate_limit", "retry_after": e.retry_after},
                status_code=429,
                headers={"Retry-After": str(e.retry_after)},
            )
        return await call_next(request)

ENV
---
    ROOMS_RL_PER_MINUTE   default 120  (sustained req/min per key)
    ROOMS_RL_BURST        default  40  (instantaneous burst tolerance)
    ROOMS_RL_KEY_HEADER   default X-Room-Key
    ROOMS_RL_BYPASS_PATHS default /health,/ui,/ui/...
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import redis  # type: ignore


@dataclass
class RateLimitExceeded(Exception):
    retry_after: int


class RateLimiter:
    """Sliding-window rate limiter backed by Redis sorted sets."""

    def __init__(
        self,
        redis_url: str = "redis://redis:6379",
        per_minute: int | None = None,
        burst: int | None = None,
        key_header: str | None = None,
        bypass_paths: tuple[str, ...] = ("/health", "/ui"),
    ) -> None:
        self.r = redis.from_url(redis_url, decode_responses=True)
        self.per_minute = int(per_minute or os.environ.get("ROOMS_RL_PER_MINUTE", "120"))
        self.burst = int(burst or os.environ.get("ROOMS_RL_BURST", "40"))
        self.key_header = key_header or os.environ.get("ROOMS_RL_KEY_HEADER", "X-Room-Key")
        bypass_env = os.environ.get("ROOMS_RL_BYPASS_PATHS", "")
        if bypass_env:
            bypass_paths = tuple(p.strip() for p in bypass_env.split(",") if p.strip())
        self.bypass_paths = bypass_paths

    def _key_id(self, request) -> str:
        h = request.headers.get(self.key_header) or ""
        ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "anon")
        )
        # Bucket by api_key+ip — compromised key can't blow past per-IP cap either.
        return f"rl:rooms:{h[:16] or 'anon'}:{ip}"

    async def check(self, request) -> None:
        path = request.url.path
        for prefix in self.bypass_paths:
            if path == prefix or path.startswith(prefix + "/"):
                return

        bucket = self._key_id(request)
        now = time.time()
        window = 60.0  # 1 min sliding

        pipe = self.r.pipeline()
        pipe.zadd(bucket, {f"{now}:{os.urandom(4).hex()}": now})
        pipe.zremrangebyscore(bucket, 0, now - window)
        pipe.zcard(bucket)
        pipe.expire(bucket, int(window) + 5)
        _, _, count, _ = pipe.execute()

        # Burst: if last second saw too many, also reject.
        recent = self.r.zcount(bucket, now - 1, now)
        if recent > self.burst:
            raise RateLimitExceeded(retry_after=2)
        if count > self.per_minute:
            raise RateLimitExceeded(retry_after=int(60 - (now - self.r.zrange(bucket, 0, 0, withscores=True)[0][1])))
