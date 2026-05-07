from fastapi import Request, HTTPException
from app.config import settings
from app.db.redis import get_redis


async def verify_api_key(request: Request) -> str:
    """Проверяет X-API-Key и возвращает agent_id. 401 если невалиден."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")

    agent_keys = settings.get_agent_keys()
    agent_id = None
    for aid, key in agent_keys.items():
        if key == api_key:
            agent_id = aid
            break

    if agent_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    request.state.agent_id = agent_id
    return agent_id


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
