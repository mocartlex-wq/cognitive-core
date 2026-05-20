# Patch: mcp_protocol.py — _resolve_agent → tuple (agent_id, user_id)

**Файл:** `/opt/cognitive-core/app/api/mcp_protocol.py`
**Назначение:** дать MCP-инструментам понимание «кто владелец вызывающего агента»
для фильтрации в `cognitive_resume`, `my_events`, `room_*` и др.
**Обратная совместимость:** `agent_id` остаётся первым в tuple — вызовы вида
`agent_id = await _resolve_agent(...)` ломаются, но их легко найти и обновить.

---

## Шаг 1. Обновить сигнатуру `_resolve_agent()`

**Найти** (примерно в районе строки 447):

```python
async def _resolve_agent(request) -> str:
    """Резолвит agent_id из X-API-Key.

    Если ключ в env (agent_api_keys JSON) — возвращает имя из словаря.
    Иначе ищет в БД agent_keys.api_key и возвращает agent_id.
    Бросает HTTPException(401) если ключ не найден.
    """
    api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required")

    # 1. env-based keys (legacy shared keys)
    env_keys = settings.get_agent_keys()
    for agent_id, key in env_keys.items():
        if key == api_key:
            return agent_id

    # 2. БД per-agent keys
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_id FROM agent_keys "
            "WHERE api_key = $1 AND revoked_at IS NULL",
            api_key,
        )
        if row:
            await conn.execute(
                "UPDATE agent_keys SET last_used_at = NOW() WHERE api_key = $1",
                api_key,
            )
            return row["agent_id"]

    raise HTTPException(status_code=401, detail="Invalid API key")
```

**Заменить на:**

```python
async def _resolve_agent(request) -> tuple[str, str | None]:
    """Резолвит (agent_id, owner_user_id) из X-API-Key.

    Возвращает:
        (agent_id, user_id) — user_id может быть None для legacy агентов,
                              не привязанных к аккаунту.

    Если ключ в env (agent_api_keys JSON) — owner=None.
    Если в БД agent_keys — owner берётся из agent_states.owner_user_id.
    """
    api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required")

    # 1. env-based keys (legacy shared keys) — без user привязки
    env_keys = settings.get_agent_keys()
    for agent_id, key in env_keys.items():
        if key == api_key:
            return agent_id, None

    # 2. БД per-agent keys — резолвим вместе с owner
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT k.agent_id,
                   s.owner_user_id::text AS owner_user_id
              FROM agent_keys k
              LEFT JOIN agent_states s ON s.agent_id = k.agent_id
             WHERE k.api_key = $1 AND k.revoked_at IS NULL
            """,
            api_key,
        )
        if row:
            await conn.execute(
                "UPDATE agent_keys SET last_used_at = NOW() WHERE api_key = $1",
                api_key,
            )
            return row["agent_id"], row["owner_user_id"]

    raise HTTPException(status_code=401, detail="Invalid API key")
```

---

## Шаг 2. Обновить все места вызова

Найти все вхождения:

```bash
grep -n "_resolve_agent(" /opt/cognitive-core/app/api/mcp_protocol.py
```

Типичный паттерн вызова раньше:

```python
agent_id = await _resolve_agent(request)
```

Заменить на:

```python
agent_id, user_id = await _resolve_agent(request)
```

Если `user_id` не нужен в конкретном вызове — оставить `_`:

```python
agent_id, _ = await _resolve_agent(request)
```

---

## Шаг 3. Использование user_id в новых tools

Места, которые **выигрывают** от user_id:

| Tool | Что меняется |
|------|--------------|
| `cognitive_resume` | Если user_id есть — отдаём только комнаты+помощники с тем же owner |
| `my_events` | Фильтр по owner_user_id, не показываем чужие события |
| `room_pending` | Только pending для агентов того же owner |
| `cognitive_remember` | Если знание privacy=private → присвоить owner_user_id |

Пример для `cognitive_resume`:

```python
async def _tool_cognitive_resume(args: dict, agent_id: str, user_id: str | None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user_id:
            # Показать только мои комнаты + помощники
            rooms = await conn.fetch(
                "SELECT name FROM rooms WHERE owner_user_id = $1::uuid LIMIT 20",
                user_id,
            )
            agents = await conn.fetch(
                "SELECT agent_id FROM agent_states WHERE owner_user_id = $1::uuid",
                user_id,
            )
        else:
            # Legacy: показываем все где этот агент участник
            rooms = await conn.fetch(
                "SELECT r.name FROM rooms r JOIN room_participants p ON p.room_id = r.id "
                "WHERE p.agent_id = $1 LIMIT 20",
                agent_id,
            )
            agents = []  # legacy не знает про user
    return {"my_rooms": [r["name"] for r in rooms], "my_agents": [a["agent_id"] for a in agents]}
```

---

## Шаг 4. Тест

После применения patch на сервере:

```bash
# 1. Перезапустить cognitive_api
sudo docker compose -f /opt/cognitive-core/docker-compose.yml \
    -f /opt/cognitive-core/docker-compose.override.yml restart cognitive_api

# 2. Дождаться "Cognitive Core ready" в логах
sudo docker logs --tail 20 cognitive_api 2>&1 | grep -i ready

# 3. Тест через старый агент без owner — должен работать
curl -X POST https://mcp.xn----8sbwawqx4fza.xn--p1ai/mcp/messages \
    -H "X-API-Key: $LEGACY_AGENT_KEY" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"tools/list","id":1}' | jq '.result.tools | length'
# Ожидается: 24

# 4. Тест через нового агента с owner — owner-фильтрация работает
curl -X POST https://mcp.xn----8sbwawqx4fza.xn--p1ai/mcp/messages \
    -H "X-API-Key: $NEW_AGENT_KEY" \
    -d '{"jsonrpc":"2.0","method":"tools/call","id":2,
         "params":{"name":"cognitive_resume","arguments":{}}}' | jq '.result.content[0].text'
# Ожидается: JSON с my_rooms содержащим только комнаты owner
```

Если что-то ломается — `git revert` коммита, контейнер откатится через auto-deploy за 60 секунд.
