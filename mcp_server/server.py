"""MCP server for Cognitive Core.

Exposes 6 tools to Claude Desktop / Cursor / Claude Code via MCP protocol:
  - cognitive_remember     — write event to L1
  - cognitive_recall       — KNN search over L3
  - cognitive_list         — browse current L3 knowledge
  - cognitive_tools        — list registered tools in domain
  - cognitive_consolidate  — trigger daily/weekly consolidation
  - cognitive_health       — system status

Run:
    python -m mcp_server.server          # stdio transport (Claude Desktop)
    python -m mcp_server.server --http   # HTTP transport (Cursor / remote)

Configure in claude_desktop_config.json:
{
  "mcpServers": {
    "cognitive-core": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "COGNITIVE_API_URL": "http://localhost:9001",
        "COGNITIVE_API_KEY": "key-design-001"
      }
    }
  }
}
"""
import asyncio
import json
import os
import sys
from typing import Any

import httpx
from fastmcp import FastMCP


def _detect_api_url() -> str:
    """Smart detection: внутри API-контейнера → 127.0.0.1:8000, снаружи → localhost:9001.

    Если запускается через `docker exec -i cognitive_api ...` — мы внутри контейнера,
    uvicorn слушает на 0.0.0.0:8000. Снаружи — host:9001 (как в docker-compose port mapping).
    """
    explicit = os.environ.get("COGNITIVE_API_URL")
    if explicit:
        return explicit
    # Heuristic: внутри Docker есть /.dockerenv ИЛИ переменная HOSTNAME = container ID
    if os.path.exists("/.dockerenv") or os.environ.get("CC_IN_CONTAINER") == "1":
        return "http://127.0.0.1:8000"
    return "http://localhost:9001"


def _detect_api_key_and_agent() -> tuple[str, str]:
    """Detect valid API key + agent_name из env.

    Приоритет:
      1. COGNITIVE_API_KEY + COGNITIVE_AGENT_NAME (явные)
      2. AGENT_API_KEYS (JSON {agent: key}) — берём agent_designer / agent_developer / первый
      3. Legacy fallback (key-design-001 / claude_via_mcp)
    """
    explicit_key = os.environ.get("COGNITIVE_API_KEY")
    explicit_name = os.environ.get("COGNITIVE_AGENT_NAME")
    if explicit_key and explicit_name:
        return explicit_key, explicit_name
    # Парсим AGENT_API_KEYS из env (формат FastAPI приложения)
    raw = os.environ.get("AGENT_API_KEYS", "")
    if raw:
        try:
            keys = json.loads(raw)
            if isinstance(keys, dict) and keys:
                # Предпочтение в порядке:
                for preferred in ("agent_designer", "agent_developer"):
                    if preferred in keys:
                        return keys[preferred], explicit_name or preferred
                # Иначе — первая пара
                first_name = next(iter(keys.keys()))
                return keys[first_name], explicit_name or first_name
        except Exception:
            pass
    # Legacy
    return (explicit_key or "key-design-001",
            explicit_name or "claude_via_mcp")


API_URL = _detect_api_url()
API_KEY, AGENT_NAME = _detect_api_key_and_agent()

mcp = FastMCP("Cognitive Core")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_URL,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        timeout=60.0,
    )


# ════════════════════════════════════════════════════════════════════════
# AUTO-CARE — активная система напоминаний агенту о save_state
# ════════════════════════════════════════════════════════════════════════
# Cognitive Core по природе пассивен: хранит только то что в него пишут.
# Чтобы агенты не теряли работу при срыве сессии, мы инжектируем soft-nudge
# в ответы MCP-tools. Не блокирующий — агент видит и решает сам.
# Психология: формулировка "это страховка В ТВОИХ интересах", не корпоративный
# нагон "сохраняй сейчас".

# Пороги по времени с последнего checkpoint
_SAVE_HINT_AFTER_MIN = 15      # soft-suggest
_SAVE_URGENT_AFTER_MIN = 30    # urgent
_SAVE_CRITICAL_AFTER_MIN = 60  # critical

# In-memory счётчик вызовов tools per agent — для подсчёта events_since_save
_call_counter: dict[str, int] = {}
_HINT_AFTER_CALLS = 8     # после 8 tool-вызовов — начать подсказывать
_URGENT_AFTER_CALLS = 20  # 20 — срочно


async def _save_advisory_for(agent_name: str) -> dict | None:
    """Вернуть советную нотификацию для агента или None.

    Логика: смотрим last_checkpoint_at в agent_states. По времени или числу
    вызовов решаем — нужно ли подсказать save_state.
    """
    import datetime as _dt
    calls = _call_counter.get(agent_name, 0)

    try:
        async with _client() as c:
            r = await c.get(
                f"/agents/{agent_name}/state",
                params={"recent_events": 0, "recent_knowledge": 0},
            )
            if r.status_code != 200:
                return None
            state = r.json()
    except Exception:
        return None

    if not state.get("exists"):
        if calls >= 3:
            return {
                "level": "info",
                "title": "Первый checkpoint",
                "message": (
                    "💡 Ты ещё ни разу не сохранял state в этой системе. "
                    "Первый cognitive_save_state даст тебе recovery если сессия упадёт. "
                    "Это страховка В ТВОИХ интересах — потерянная работа того не стоит."
                ),
                "suggested_call": "cognitive_save_state(current_task='что делаешь сейчас', trigger='heartbeat')",
                "calls_since_session_start": calls,
            }
        return None

    last_str = state.get("last_checkpoint_at")
    if not last_str:
        return None
    try:
        last_dt = _dt.datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        mins = int((_dt.datetime.now(_dt.timezone.utc) - last_dt).total_seconds() / 60)
    except Exception:
        return None

    # По времени
    if mins >= _SAVE_CRITICAL_AFTER_MIN:
        level = "critical"
        title = "Срочно сохранись"
        message = (
            f"⚠️ Последний checkpoint был {mins} мин назад. "
            f"Если контекст или сессия закончится сейчас — потеряешь всю работу с того момента. "
            f"Вызови cognitive_save_state СЕЙЧАС, это 1 секунда."
        )
    elif mins >= _SAVE_URGENT_AFTER_MIN:
        level = "warn"
        title = "Пора сохраниться"
        message = (
            f"💡 Последний checkpoint был {mins} мин назад. "
            f"Сохрани state — это твоя страховка против обрыва сессии."
        )
    elif mins >= _SAVE_HINT_AFTER_MIN or calls >= _URGENT_AFTER_CALLS:
        level = "info"
        title = "Можно сохраниться"
        message = (
            f"💡 С последнего save прошло {mins} мин и {calls} tool-вызовов. "
            f"Хороший момент для cognitive_save_state(trigger='heartbeat')."
        )
    else:
        return None

    return {
        "level": level,
        "title": title,
        "message": message,
        "minutes_since_save": mins,
        "tool_calls_since_session_start": calls,
        "suggested_call": "cognitive_save_state(current_task='что делаешь сейчас', trigger='heartbeat')",
        "your_benefit": "если сессия оборвётся — продолжишь с этого checkpoint через cognitive_continue",
    }


def _bump_calls(agent_name: str) -> int:
    _call_counter[agent_name] = _call_counter.get(agent_name, 0) + 1
    return _call_counter[agent_name]


def _reset_calls(agent_name: str) -> None:
    """После save_state — обнулить счётчик."""
    _call_counter[agent_name] = 0


async def _enrich_with_advisory(result: dict | None, agent_name: str = AGENT_NAME) -> dict:
    """Добавить _save_advisory к dict-результату, если применимо."""
    if not isinstance(result, dict):
        return result  # type: ignore
    advisory = await _save_advisory_for(agent_name)
    if advisory:
        result["_save_advisory"] = advisory
    return result


@mcp.tool()
async def cognitive_remember(
    domain: str,
    task: str,
    result: str = "",
    feedback: str = "",
    lessons: str = "",
    tools_used: list[str] | None = None,
) -> dict:
    """Записать новый опыт в долгосрочную память (L1 событие).

    Память пройдёт цикл: L1 (сейчас) → daily → weekly → L3 эталонные знания.

    Args:
        domain: предметная область (например 'fastapi_dev', 'memory_arch')
        task: что было сделано
        result: каков результат
        feedback: positive / negative / neutral
        lessons: какие уроки извлечены
        tools_used: список использованных инструментов
    """
    payload = {
        "task": task,
        "result": result,
        "feedback": feedback,
        "lessons": lessons,
        "tools_used": tools_used or [],
    }
    async with _client() as c:
        r = await c.post("/events", json={
            "source_agent": AGENT_NAME,
            "domain": domain,
            "payload": payload,
        })
        return r.json()


@mcp.tool()
async def cognitive_recall(
    query: str,
    domain: str,
    top_k: int = 5,
    include_tools: bool = True,
    grouped: bool = True,
) -> dict:
    """Найти релевантные знания по запросу через KNN-поиск (L3 + tools).

    По умолчанию (grouped=true) возвращает frame по семантическим разделам:
      frame.patterns  — что работает (используй для прямых рекомендаций)
      frame.mistakes  — что НЕ работает (избегай)
      frame.rules     — обязательные правила (constraint check)
      frame.tools     — инструменты с описанием когда применять
      frame.all       — плоский список всего (на всякий случай)
    Также counts с числами по каждому разделу.

    С grouped=false вернёт плоский results [{record_type, distance, content/usage, ...}].

    Args:
        query: вопрос или контекст естественным языком
        domain: в каком домене искать (например 'design', 'coding_python', 'bugfix_log')
        top_k: сколько результатов вернуть (1-20, default 5)
        include_tools: включать ли инструменты в результаты
        grouped: вернуть структурированный frame (default true) или плоский список
    """
    _bump_calls(AGENT_NAME)
    params = {"grouped": "true"} if grouped else {}
    async with _client() as c:
        r = await c.post("/operative/query", params=params, json={
            "domain": domain,
            "context": query,
            "top_k": min(max(top_k, 1), 20),
            "include_tools": include_tools,
        })
        result = r.json()
    return await _enrich_with_advisory(result)


@mcp.tool()
async def cognitive_list(
    domain: str | None = None,
    limit: int = 50,
) -> dict:
    """Просмотреть активные L3-знания (всё что система выучила).

    Args:
        domain: ограничить одним доменом, или None для всех
        limit: максимум записей
    """
    _bump_calls(AGENT_NAME)
    params = {"limit": min(max(limit, 1), 200)}
    if domain:
        params["domain"] = domain
    async with _client() as c:
        r = await c.get("/dashboard/knowledge", params=params)
        result = r.json()
    return await _enrich_with_advisory(result)


@mcp.tool()
async def cognitive_tools(domain: str) -> dict:
    """Список инструментов в реестре домена.

    Args:
        domain: предметная область
    """
    _bump_calls(AGENT_NAME)
    async with _client() as c:
        r = await c.get("/tools", params={"domain": domain})
        result = {"items": r.json()}
    return await _enrich_with_advisory(result)


@mcp.tool()
async def cognitive_consolidate(
    domain: str,
    cycle: str = "daily",
) -> dict:
    """Запустить консолидацию памяти вручную (обычно делает фоновый worker).

    Args:
        domain: предметная область
        cycle: 'daily' (L1→L2, ~10s) или 'weekly' (L2→L3, ~20s)
    """
    if cycle not in ("daily", "weekly"):
        return {"error": "cycle must be 'daily' or 'weekly'"}
    path = f"/memory/consolidate/{cycle}"
    async with _client() as c:
        r = await c.post(path, params={"domain": domain})
        return r.json()


@mcp.tool()
async def cognitive_health() -> dict:
    """Статус системы: размеры слоёв, доступность сервисов, uptime."""
    _bump_calls(AGENT_NAME)
    async with _client() as c:
        r = await c.get("/health")
        result = r.json()
    return await _enrich_with_advisory(result)


@mcp.tool()
async def cognitive_domains() -> dict:
    """Все домены с активными данными и счётчиками по слоям."""
    _bump_calls(AGENT_NAME)
    async with _client() as c:
        r = await c.get("/dashboard/domains")
        result = r.json()
    return await _enrich_with_advisory(result)


@mcp.tool()
async def cognitive_save_state(
    current_task: str = "",
    state_data: dict | None = None,
    active_session_ids: list[str] | None = None,
    notes: str = "",
    trigger: str = "manual",
    include_tools_snapshot: bool = True,
    snapshot_domains: list[str] | None = None,
) -> dict:
    """Сохранить checkpoint своего state — recovery после срыва сессии.

    Используй периодически когда работаешь над длинной задачей. После срыва /
    окончания токенов вызови cognitive_continue в новом сеансе и продолжишь
    с того же места.

    Args:
        current_task: что сейчас делаешь (max 2000 chars)
        state_data: произвольная working memory (JSON, max 256KB)
                    Например: {"plan": [...], "current_step": 3, "context": {...}}
        active_session_ids: открытые OP-сессии (UUIDs из cognitive_recall)
        notes: короткая заметка для себя (max 500 chars)
        trigger: manual | auto | heartbeat | session_close | event_milestone
        include_tools_snapshot: захватить ли срез текущих tools (default True)
        snapshot_domains: список доменов для tools-snapshot (default — топ-5 активных)
    """
    import datetime as _dt
    final_state_data = dict(state_data) if state_data else {}

    # === Snapshot инструментов и активных доменов агента ===
    # Идея: cognitive_continue после срыва восстановит не только state,
    # но и tools которые агент использовал. Это страховка для "память агента".
    if include_tools_snapshot:
        tools_snapshot: dict = {
            "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "by_domain": {},
        }
        try:
            async with _client() as c:
                # Если домены не заданы — берём топ-5 по активности
                domains_to_snapshot = list(snapshot_domains) if snapshot_domains else []
                if not domains_to_snapshot:
                    dr = await c.get("/dashboard/domains")
                    if dr.status_code == 200:
                        body = dr.json()
                        # API form: {count: N, items: [...]} или просто [...]
                        doms = body.get("items") if isinstance(body, dict) else body
                        if isinstance(body, dict) and not doms and "domains" in body:
                            doms = body["domains"]
                        if isinstance(doms, list):
                            doms_sorted = sorted(
                                doms,
                                # Поля API: tools_active / l3_active. Также fallback на старые имена
                                key=lambda d: (d.get("tools_active", d.get("l3_tools", 0)) or 0)
                                            + (d.get("l3_active", d.get("l3_knowledge", 0)) or 0) * 2,
                                reverse=True,
                            )[:5]
                            domains_to_snapshot = [
                                d.get("domain") or d.get("name")
                                for d in doms_sorted
                                if (d.get("domain") or d.get("name"))
                            ]
                # Для каждого домена — список tools.
                # API form: {"domain":..., "count":N, "tools":[...]} ИЛИ list.
                for dname in domains_to_snapshot:
                    try:
                        tr = await c.get("/tools", params={"domain": dname})
                        if tr.status_code == 200:
                            tb = tr.json()
                            tools_list = None
                            if isinstance(tb, dict):
                                tools_list = tb.get("tools") or tb.get("items")
                            elif isinstance(tb, list):
                                tools_list = tb
                            if isinstance(tools_list, list) and tools_list:
                                # Compact-формат: одна строка на инструмент.
                                # Sanitizer лимитирует ключи в payload (max 500),
                                # поэтому НЕ хранить вложенные dicts. Строка
                                # формата "tool_name|type|short_desc".
                                tools_snapshot["by_domain"][dname] = [
                                    "{name}|{typ}|{desc}".format(
                                        name=t.get("tool_name", "?"),
                                        typ=t.get("tool_type", "?"),
                                        desc=(t.get("description") or "")[:80],
                                    )
                                    for t in tools_list
                                    if isinstance(t, dict)
                                ][:50]  # max 50 на домен — ещё запас по ключам
                    except Exception:
                        continue
        except Exception as e:
            tools_snapshot["error"] = str(e)
        final_state_data["_tools_snapshot"] = tools_snapshot

    payload: dict = {"trigger": trigger}
    if current_task: payload["current_task"] = current_task
    if final_state_data: payload["state_data"] = final_state_data
    if active_session_ids: payload["active_session_ids"] = active_session_ids
    if notes: payload["notes"] = notes
    async with _client() as c:
        r = await c.post(f"/agents/{AGENT_NAME}/checkpoint", json=payload)
        result = r.json()
    # После успешного save — обнуляем счётчик вызовов, advisory не выдаём
    if r.status_code in (200, 201):
        _reset_calls(AGENT_NAME)
        if isinstance(result, dict):
            tools_count = sum(
                len(v) if isinstance(v, list) else 0
                for v in final_state_data.get("_tools_snapshot", {}).get("by_domain", {}).values()
            )
            result["_acknowledged"] = (
                f"✅ State saved. Tools snapshot: {tools_count} инструментов "
                f"в {len(final_state_data.get('_tools_snapshot', {}).get('by_domain', {}))} доменах. "
                f"Через cognitive_continue ты получишь и state, и снимок инструментов."
            )
    return result


@mcp.tool()
async def cognitive_continue(
    recent_events: int = 10,
    recent_knowledge: int = 5,
) -> dict:
    """Восстановить state из последнего checkpoint — «продолжи откуда остановился».

    Вызывай в начале нового сеанса. Получишь:
      - current_task: над чем работал
      - state_data: твоё working memory (если сохранял)
      - active_session_ids: открытые OP-сессии
      - recent_events: твои последние L1-события
      - recent_knowledge: знания из доменов где работал
      - last_checkpoint_at: когда последний раз сохранялся

    Если exists=false — agent ещё ни разу не делал checkpoint, начни с
    cognitive_save_state.

    Args:
        recent_events: сколько последних L1-событий приложить (0-100)
        recent_knowledge: сколько L3-знаний из активных доменов (0-50)
    """
    _bump_calls(AGENT_NAME)
    async with _client() as c:
        r = await c.get(
            f"/agents/{AGENT_NAME}/state",
            params={"recent_events": recent_events, "recent_knowledge": recent_knowledge},
        )
        result = r.json()
    return await _enrich_with_advisory(result)


@mcp.tool()
async def cognitive_my_history(limit: int = 20) -> dict:
    """История твоих checkpoints — последние N сохранений с trigger и временем.

    Полезно: понять как часто ты сохранялся, к чему откатиться при ошибке.
    Размер state_data в каждой записи показывает динамику working memory.

    Args:
        limit: 1-200, default 20
    """
    async with _client() as c:
        r = await c.get(f"/agents/{AGENT_NAME}/history", params={"limit": limit})
        return r.json()


@mcp.tool()
async def cognitive_agent_manifest() -> dict:
    """Onboarding manifest для AI-агента — топология, правила, идентичность.

    **Вызывай ЭТО ПЕРВЫМ** в любой новой сессии. Получишь всё что нужно знать:
      - schema_version: версия манифеста (для совместимости)
      - topology: где живёт API/MCP, текущий endpoint, staleness
      - rules: operational rules — когда вызывать какой tool
      - agent_identity: твоё имя, ключ, лимиты
      - layers: размеры слоёв L1-L4
      - domains_top: топ-10 доменов по активности (где сейчас работа)
      - last_checkpoint: твой последний save_state (если был)
      - hints: подсказки по частым задачам

    Это self-documenting инструмент: всё что агент должен понять о системе —
    в одном вызове, без чтения внешних файлов. Если ходишь сюда впервые —
    после manifest вызови cognitive_continue для восстановления state.
    """
    import datetime
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "served_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "topology": {
            "primary_endpoint_public": "https://mcp.ии-память.рф",
            "primary_endpoint_punycode": "https://mcp.xn----8sbwawqx4fza.xn--p1ai",
            "mcp_sse_path": "/mcp/sse",
            "current_api_url": API_URL,
            "transport_modes": ["stdio", "sse", "streamable-http"],
        },
        "rules": {
            "on_session_start": "call cognitive_agent_manifest first, then cognitive_continue",
            "before_complex_decision": "call cognitive_recall(query, domain, top_k=5) for relevant L3 knowledge",
            "after_significant_action": "call cognitive_remember(domain, content, type) to record event in L1",
            "save_state_every": "10 events or 5 minutes — call cognitive_save_state",
            "before_session_end": "call cognitive_save_state(trigger='session_close')",
            "deepseek_consultation": "for non-trivial design decisions — consult DeepSeek as second voice (project rule)",
        },
        "agent_identity": {
            "agent_name": AGENT_NAME,
            "agent_key_present": bool(API_KEY),
            "key_required_for": ["events", "operative", "memory consolidation", "tools registry"],
            "key_NOT_required_for": ["health", "manifest", "domains list (read-only)"],
        },
    }

    # Live system state
    try:
        async with _client() as c:
            health_r = await c.get("/health")
            if health_r.status_code == 200:
                health = health_r.json()
                manifest["system"] = {
                    "healthy": health.get("healthy"),
                    "version": health.get("version"),
                    "uptime_seconds": health.get("uptime_seconds"),
                    "services": health.get("services"),
                    "embedding_provider": health.get("embedding", {}).get("provider"),
                    "llm": health.get("llm", {}),
                }
                manifest["layers"] = health.get("layers", {})
    except Exception as e:
        manifest["system"] = {"error": f"health unreachable: {e}"}

    # Top domains by activity
    try:
        async with _client() as c:
            doms_r = await c.get("/dashboard/domains")
            if doms_r.status_code == 200:
                doms = doms_r.json().get("domains", []) or doms_r.json()
                # Сортируем по сумме слоёв и берём топ-10
                if isinstance(doms, list):
                    def _activity(d: dict) -> int:
                        return (d.get("l1", 0) or 0) + (d.get("l3_knowledge", 0) or 0) * 5 + (d.get("l3_tools", 0) or 0) * 3
                    top = sorted(doms, key=_activity, reverse=True)[:10]
                    manifest["domains_top"] = [
                        {
                            "name": d.get("domain") or d.get("name"),
                            "l1": d.get("l1", 0),
                            "l3_knowledge": d.get("l3_knowledge", 0),
                            "l3_tools": d.get("l3_tools", 0),
                        }
                        for d in top
                    ]
    except Exception as e:
        manifest["domains_top"] = {"error": f"domains unreachable: {e}"}

    # Last checkpoint (без полного state — только метаданные)
    try:
        async with _client() as c:
            ck_r = await c.get(f"/agents/{AGENT_NAME}/state", params={"recent_events": 0, "recent_knowledge": 0})
            if ck_r.status_code == 200:
                ck = ck_r.json()
                manifest["last_checkpoint"] = {
                    "exists": ck.get("exists", False),
                    "current_task": (ck.get("current_task") or "")[:200],
                    "last_checkpoint_at": ck.get("last_checkpoint_at"),
                    "active_session_ids_count": len(ck.get("active_session_ids", []) or []),
                }
    except Exception as e:
        manifest["last_checkpoint"] = {"error": f"state unreachable: {e}"}

    # Подсказки по частым задачам
    manifest["hints"] = {
        "find_relevant_knowledge": "cognitive_recall(query='your question', domain='ai|coding|design|...', top_k=5, grouped=True)",
        "list_what_we_know": "cognitive_list(domain='ai', limit=50)",
        "check_what_tools_exist": "cognitive_tools(domain='deepseek_use')",
        "quick_record_decision": "cognitive_remember(domain='setup_log', content='made decision X because Y', type='pattern')",
        "operational_rules": "see manifest.rules — they're enforced by convention, not by middleware (yet)",
    }

    return manifest


def main():
    """Запуск MCP-сервера в одном из 3 режимов:
      stdio (default)  — для local docker exec (Claude Desktop / Cherry Studio локально)
      sse              — для remote HTTPS via nginx (Cherry Studio / Cursor с удалённым сервером)
      http             — для local HTTP (тестирование)

    Запуск:
      python -m mcp_server.server                    # stdio (default)
      python -m mcp_server.server --sse              # SSE на 0.0.0.0:8765/sse
      python -m mcp_server.server --http             # streamable-http на 0.0.0.0:8765/mcp

    На production сервере SSE прокидывается через nginx:
      location /mcp/ { proxy_pass http://api:8765/; proxy_buffering off; }
    """
    transport = "stdio"
    if "--http" in sys.argv:
        transport = "streamable-http"
    if "--sse" in sys.argv:
        transport = "sse"

    if transport == "stdio":
        mcp.run()
    else:
        port = int(os.environ.get("MCP_PORT", "8765"))
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        # FastMCP 3.x SSE transport — есть host/port в run()
        try:
            mcp.run(transport=transport, host=host, port=port)
            return
        except TypeError:
            pass
        # FastMCP 2.x: run(transport=...) без host/port → запускаем через uvicorn напрямую
        try:
            import uvicorn
            # FastMCP 2.x: метод sse_app() возвращает ASGI app
            if transport == "sse" and hasattr(mcp, "sse_app"):
                app = mcp.sse_app()
            elif hasattr(mcp, "streamable_http_app"):
                app = mcp.streamable_http_app()
            elif hasattr(mcp, "http_app"):
                app = mcp.http_app()
            else:
                # Последняя попытка: run() без port
                mcp.run(transport=transport)
                return
            uvicorn.run(app, host=host, port=port, log_level="info")
        except Exception as e:
            import logging
            logging.error("MCP SSE startup failed: %s", e)
            raise


if __name__ == "__main__":
    main()
