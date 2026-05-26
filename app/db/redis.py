import redis.asyncio as redis

from app.config import settings

_client: redis.Redis | None = None

def _build_operative_index() -> list[str]:
    """Индекс с размерностью из embedder.EMBEDDING_DIM (один источник правды)."""
    from app.services.embedder import EMBEDDING_DIM
    return [
        "FT.CREATE", "idx:operative", "ON", "HASH", "PREFIX", "1", "op:",
        "SCHEMA",
        "domain", "TAG",
        "record_type", "TAG",
        "content_summary", "TEXT",
        "embedding", "VECTOR", "FLAT", "6",
        "DIM", str(EMBEDDING_DIM),
        "TYPE", "FLOAT32",
        "DISTANCE_METRIC", "COSINE",
    ]


_client: redis.Redis | None = None
_raw_client: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def get_redis_raw() -> redis.Redis:
    """Клиент БЕЗ decode_responses — для бинарных операций (векторы)."""
    global _raw_client
    if _raw_client is None:
        _raw_client = redis.from_url(settings.redis_url, decode_responses=False)
    return _raw_client


async def init_redis() -> None:
    """Создаёт индекс RediSearch при старте.
    Drop + recreate чтобы размерность всегда соответствовала текущей EMBEDDING_DIM."""
    r = await get_redis_raw()
    for old_idx in ("idx:operative", "idx:operative_vector"):
        try:
            await r.execute_command("FT.DROPINDEX", old_idx, "DD")
        except redis.ResponseError:
            pass
    await r.execute_command(*_build_operative_index())


async def close_redis() -> None:
    global _client, _raw_client
    if _client:
        await _client.close()
        _client = None
    if _raw_client:
        await _raw_client.close()
        _raw_client = None
