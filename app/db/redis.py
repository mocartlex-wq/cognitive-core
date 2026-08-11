import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

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
    """Создаёт индекс RediSearch при старте — но НЕ уничтожает документы.

    Раньше здесь безусловно выполнялся `FT.DROPINDEX … DD`. Флаг DD удаляет не
    только определение индекса, но и сами документы: каждый рестарт и каждый
    деплой обнулял векторный кеш целиком. С учётом того, что деплой идёт по
    коммиту, KNN-путь регулярно оказывался пустым, и поиск молча деградировал
    до pgvector.

    Теперь индекс пересоздаётся, только если он отсутствует или его размерность
    разошлась с текущей EMBEDDING_DIM (смена модели). В остальных случаях
    существующий индекс и документы остаются на месте.
    """
    from app.services.embedder import EMBEDDING_DIM  # локальный импорт: см. _build_operative_index

    r = await get_redis_raw()

    # Старое имя — удаляем без сожалений, оно давно не используется.
    try:
        await r.execute_command("FT.DROPINDEX", "idx:operative_vector", "DD")
    except redis.ResponseError:
        pass

    needs_recreate = True
    try:
        info = await r.execute_command("FT.INFO", "idx:operative")
        # Ответ — плоский список пар; ищем размерность векторного поля.
        flat = [x.decode() if isinstance(x, bytes) else str(x) for x in info]
        blob = " ".join(flat)
        needs_recreate = f"{EMBEDDING_DIM}" not in blob
        if needs_recreate:
            logger.warning(
                "RediSearch: размерность индекса разошлась с EMBEDDING_DIM=%s — пересоздаю",
                EMBEDDING_DIM,
            )
    except redis.ResponseError:
        needs_recreate = True  # индекса нет

    if not needs_recreate:
        return

    try:
        await r.execute_command("FT.DROPINDEX", "idx:operative", "DD")
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
