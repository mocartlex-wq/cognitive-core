import json
import struct
from uuid import UUID, uuid4
from datetime import datetime, timezone
import numpy as np
from app.db.postgres import get_pool
from app.db.redis import get_redis, get_redis_raw
from app.services.embedder import embed_text, EMBEDDING_DIM, get_model_version
from app.services.ingestor import save_raw_event
from app.security.audit import log_audit

SESSION_TTL = 86400  # 24 часа


async def _index_l3_vector(item_id: str, domain: str, record_type: str, content_str: str, vector: list[float]) -> bool:
    """Индексирует вектор в Redis для RediSearch KNN. True если успешно.
    Сохраняет version модели для hot-reload — старые векторы помечаются stale."""
    try:
        r = await get_redis_raw()
        key = f"op:{item_id}"
        vec_bytes = struct.pack(f"{len(vector)}f", *vector)
        await r.hset(key, mapping={
            "id": item_id,
            "domain": domain,
            "record_type": record_type,
            "content_summary": content_str[:500],
            "embedding": vec_bytes,
            "mv": get_model_version(),  # version модели — для hot-reload
        })
        await r.expire(key, SESSION_TTL)
        return True
    except Exception:
        return False


async def cleanup_stale_vectors() -> dict:
    """Удаляет из Redis векторы с устаревшей model_version.
    Вызывается при смене эмбеддинг-модели (hot-reload)."""
    current_mv = get_model_version()
    r = await get_redis_raw()
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match=b"op:*", count=200)
        for key in keys:
            try:
                mv_bytes = await r.hget(key, "mv")
                if mv_bytes is None:
                    # Старый формат без mv → считаем stale
                    await r.delete(key)
                    deleted += 1
                    continue
                mv = mv_bytes.decode() if isinstance(mv_bytes, bytes) else mv_bytes
                if mv != current_mv:
                    await r.delete(key)
                    deleted += 1
            except Exception:
                continue
        if cursor == 0:
            break
    return {"deleted": deleted, "current_model": current_mv}


async def _remove_indexed_vectors(item_ids: list[str]) -> int:
    """Удаляет векторы из Redis по списку ID. Возвращает количество удалённых."""
    if not item_ids:
        return 0
    try:
        r = await get_redis_raw()
        keys = [f"op:{iid}" for iid in item_ids]
        return await r.delete(*keys)
    except Exception:
        return 0


def _vec_to_pg(vec: list[float]) -> str:
    """Конвертирует list[float] в строку для pgvector: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


async def index_domain_vectors(domain: str) -> dict:
    """Индексирует ВСЕ активные L3 знания и инструменты домена.
    Пишет одновременно в pgvector (постоянное хранилище, ACID) и Redis (быстрый KNN с TAG-фильтром).
    Вызывается после weekly consolidation и monthly audit."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        knowledge_rows = await conn.fetch(
            """
            SELECT id, domain, knowledge_type, content
            FROM l3_master_knowledge
            WHERE domain = $1 AND effective_to IS NULL
            """,
            domain,
        )
        tool_rows = await conn.fetch(
            """
            SELECT id, domain, tool_name, tool_type, description,
                   config_schema, usage_patterns
            FROM l3_tools_registry
            WHERE domain = $1 AND effective_to IS NULL
            """,
            domain,
        )

        knowledge_indexed = 0
        for k in knowledge_rows:
            kdict = dict(k)
            raw = _parse_jsonb(kdict.get("content", {}))
            content_str = json.dumps(raw, ensure_ascii=False)
            vec = await embed_text(content_str)
            # 1. Persistent: pgvector в Postgres
            await conn.execute(
                "UPDATE l3_master_knowledge SET embedding = $1::vector WHERE id = $2",
                _vec_to_pg(vec), kdict["id"],
            )
            # 2. Fast: Redis для KNN с domain TAG
            ok = await _index_l3_vector(
                str(kdict["id"]), domain, "knowledge", content_str, vec
            )
            if ok:
                knowledge_indexed += 1

        tools_indexed = 0
        for t in tool_rows:
            tdict = dict(t)
            desc = tdict.get("description", "")
            usage = _parse_jsonb(tdict.get("usage_patterns", {}))
            tool_str = json.dumps({
                "name": tdict.get("tool_name", ""),
                "type": tdict.get("tool_type", ""),
                "description": desc,
                "usage": usage,
            }, ensure_ascii=False)
            vec = await embed_text(tool_str)
            await conn.execute(
                "UPDATE l3_tools_registry SET embedding = $1::vector WHERE id = $2",
                _vec_to_pg(vec), tdict["id"],
            )
            ok = await _index_l3_vector(
                str(tdict["id"]), domain, "tool", tool_str, vec
            )
            if ok:
                tools_indexed += 1

    return {
        "domain": domain,
        "knowledge_indexed": knowledge_indexed,
        "tools_indexed": tools_indexed,
        "total": knowledge_indexed + tools_indexed,
    }


async def restore_redis_from_pg(domain: str | None = None) -> dict:
    """Cold-start: загружает векторы из pgvector в Redis.
    Используется после рестарта Redis — без вызовов LLM."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if domain:
            knowledge_rows = await conn.fetch(
                "SELECT id, domain, content, embedding::text AS emb FROM l3_master_knowledge "
                "WHERE domain = $1 AND effective_to IS NULL AND embedding IS NOT NULL",
                domain,
            )
            tool_rows = await conn.fetch(
                "SELECT id, domain, tool_name, tool_type, description, usage_patterns, embedding::text AS emb "
                "FROM l3_tools_registry WHERE domain = $1 AND effective_to IS NULL AND embedding IS NOT NULL",
                domain,
            )
        else:
            knowledge_rows = await conn.fetch(
                "SELECT id, domain, content, embedding::text AS emb FROM l3_master_knowledge "
                "WHERE effective_to IS NULL AND embedding IS NOT NULL",
            )
            tool_rows = await conn.fetch(
                "SELECT id, domain, tool_name, tool_type, description, usage_patterns, embedding::text AS emb "
                "FROM l3_tools_registry WHERE effective_to IS NULL AND embedding IS NOT NULL",
            )

    restored = 0
    for r in knowledge_rows:
        emb_str = r["emb"]
        if not emb_str:
            continue
        vec = [float(x) for x in emb_str.strip("[]").split(",")]
        content_str = json.dumps(_parse_jsonb(r["content"]), ensure_ascii=False)
        if await _index_l3_vector(str(r["id"]), r["domain"], "knowledge", content_str, vec):
            restored += 1

    for r in tool_rows:
        emb_str = r["emb"]
        if not emb_str:
            continue
        vec = [float(x) for x in emb_str.strip("[]").split(",")]
        tool_str = json.dumps({
            "name": r.get("tool_name", ""),
            "type": r.get("tool_type", ""),
            "description": r.get("description", ""),
            "usage": _parse_jsonb(r.get("usage_patterns", {})),
        }, ensure_ascii=False)
        if await _index_l3_vector(str(r["id"]), r["domain"], "tool", tool_str, vec):
            restored += 1

    return {"restored": restored, "from": "pgvector"}


async def search_pgvector(query_vec: list[float], domain: str, top_k: int = 5) -> list[dict]:
    """KNN-поиск через pgvector (fallback когда Redis пустой)."""
    pool = await get_pool()
    qvec = _vec_to_pg(query_vec)
    async with pool.acquire() as conn:
        knowledge = await conn.fetch(
            f"""
            SELECT id, domain, knowledge_type, content,
                   (embedding <=> $1::vector) AS distance
            FROM l3_master_knowledge
            WHERE domain = $2 AND effective_to IS NULL AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            qvec, domain, top_k,
        )
        tools = await conn.fetch(
            f"""
            SELECT id, domain, tool_name, tool_type, description, config_schema, usage_patterns,
                   (embedding <=> $1::vector) AS distance
            FROM l3_tools_registry
            WHERE domain = $2 AND effective_to IS NULL AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            qvec, domain, max(2, top_k // 2),
        )

    results = []
    for k in knowledge:
        kd = dict(k)
        parsed = _parse_jsonb(kd["content"])
        results.append({
            "id": str(kd["id"]),
            "record_type": "knowledge",
            "domain": kd["domain"],
            "content": parsed,
            "knowledge_type": kd.get("knowledge_type", ""),
            "confidence": _extract_confidence(parsed),
            "distance": float(kd["distance"]),
        })
    for t in tools:
        td = dict(t)
        results.append({
            "id": str(td["id"]),
            "record_type": "tool",
            "domain": td["domain"],
            "tool_name": td.get("tool_name", ""),
            "tool_type": td.get("tool_type", ""),
            "config_schema": _parse_jsonb(td.get("config_schema")),
            "usage": _parse_jsonb(td.get("usage_patterns")),
            "distance": float(td["distance"]),
        })
    results.sort(key=lambda x: x["distance"])
    return results


async def _search_redis_vector(query_vec: list[float], domain: str, top_k: int = 5) -> list[dict]:
    """Поиск через RediSearch KNN. Возвращает список результатов."""
    try:
        r = await get_redis_raw()
        vec_bytes = struct.pack(f"{len(query_vec)}f", *query_vec)
        results = await r.execute_command(
            "FT.SEARCH", "idx:operative",
            f"@domain:{{{domain}}}=>[KNN {top_k} @embedding $vec]",
            "SORTBY", "__embedding_score",
            "PARAMS", "2", "vec", vec_bytes,
            "DIALECT", "2",
            "LIMIT", "0", str(top_k),
            "RETURN", "3", "id", "record_type", "content_summary",
        )
        items = []
        for i in range(2, len(results), 2):
            field_list = results[i]
            fields = {}
            for j in range(0, len(field_list), 2):
                key = field_list[j]
                val = field_list[j + 1]
                if isinstance(key, bytes):
                    key = key.decode()
                if isinstance(val, bytes):
                    val = val.decode()
                fields[key] = val
            items.append({"id": fields.get("id", ""), "record_type": fields.get("record_type", "knowledge")})
        return items
    except Exception:
        return []


async def build_operative(query: str, domain: str, top_k: int = 5, include_tools: bool = True) -> list[dict]:
    """KNN-поиск по L3: RediSearch → pgvector → Python fallback."""
    query_vec = await embed_text(query)

    # Попытка 1: RediSearch векторный поиск (быстрее всего, с TAG-фильтром по domain)
    redis_hits = await _search_redis_vector(query_vec, domain, top_k)
    if redis_hits:
        pool = await get_pool()
        async with pool.acquire() as conn:
            results = []
            for hit in redis_hits:
                rid = hit.get("id", "")
                rtype = hit.get("record_type", "knowledge")
                if rtype == "knowledge":
                    row = await conn.fetchrow(
                        "SELECT id, domain, knowledge_type, content FROM l3_master_knowledge WHERE id = $1 AND effective_to IS NULL",
                        UUID(rid),
                    )
                    if row:
                        rd = dict(row)
                        parsed = _parse_jsonb(rd.get("content", {}))
                        results.append({
                            "id": str(rd["id"]),
                            "record_type": "knowledge",
                            "domain": rd["domain"],
                            "content": parsed,
                            "knowledge_type": rd.get("knowledge_type", ""),
                            "confidence": _extract_confidence(parsed),
                            "distance": 0.0,
                        })
                elif rtype == "tool":
                    row = await conn.fetchrow(
                        "SELECT id, domain, tool_name, tool_type, description, config_schema, usage_patterns FROM l3_tools_registry WHERE id = $1 AND effective_to IS NULL",
                        UUID(rid),
                    )
                    if row:
                        rt = dict(row)
                        results.append({
                            "id": str(rt["id"]),
                            "record_type": "tool",
                            "domain": rt["domain"],
                            "tool_name": rt.get("tool_name", ""),
                            "tool_type": rt.get("tool_type", ""),
                            "config_schema": _parse_jsonb(rt.get("config_schema")),
                            "usage": _parse_jsonb(rt.get("usage_patterns")),
                            "distance": 0.0,
                        })
            if results:
                return results

    # Попытка 2: pgvector KNN (если Redis пуст — например после рестарта)
    pg_results = await search_pgvector(query_vec, domain, top_k)
    if pg_results:
        # Параллельно восстанавливаем Redis для следующих запросов
        try:
            await restore_redis_from_pg(domain)
        except Exception:
            pass
        if not include_tools:
            pg_results = [r for r in pg_results if r["record_type"] != "tool"]
        return pg_results[:top_k + (top_k // 2 if include_tools else 0)]

    # Попытка 3: Python KNN (последний fallback — для свежесозданных без эмбеддингов)
    pool = await get_pool()
    async with pool.acquire() as conn:
        knowledge_rows = await conn.fetch(
            """
            SELECT id, domain, knowledge_type, content, version,
                   derived_from_l2_ids, related_tool_ids, effective_from
            FROM l3_master_knowledge
            WHERE domain = $1 AND effective_to IS NULL
            """,
            domain,
        )
        tool_rows = []
        if include_tools:
            tool_rows = await conn.fetch(
                """
                SELECT id, domain, tool_name, tool_type, description,
                       config_schema, usage_patterns, version
                FROM l3_tools_registry
                WHERE domain = $1 AND effective_to IS NULL
                """,
                domain,
            )

    knowledge_with_distance = []
    for k in knowledge_rows:
        kdict = dict(k)
        raw_content = _parse_jsonb(kdict.get("content", {}))
        content_str = json.dumps(raw_content, ensure_ascii=False)
        kvec = await embed_text(content_str)
        dist = float(_cosine_similarity(query_vec, kvec))
        kdict["_parsed_content"] = raw_content
        kdict["distance"] = dist
        knowledge_with_distance.append(kdict)

    knowledge_with_distance.sort(key=lambda x: x["distance"], reverse=True)

    tools_with_distance = []
    if include_tools:
        for t in tool_rows:
            tdict = dict(t)
            usage = _parse_jsonb(tdict.get("usage_patterns", {}))
            desc = tdict.get("description", "")
            tool_str = json.dumps({
                "name": tdict.get("tool_name", ""),
                "type": tdict.get("tool_type", ""),
                "description": desc,
                "usage": usage,
            }, ensure_ascii=False)
            tvec = await embed_text(tool_str)
            dist = float(_cosine_similarity(query_vec, tvec))
            tdict["_parsed_usage"] = usage
            tdict["distance"] = dist
            tools_with_distance.append(tdict)
        tools_with_distance.sort(key=lambda x: x["distance"], reverse=True)

    results = []
    for k in knowledge_with_distance[:top_k]:
        parsed = k.get("_parsed_content", k.get("content", {}))
        results.append({
            "id": str(k["id"]),
            "record_type": "knowledge",
            "domain": k["domain"],
            "content": parsed,
            "knowledge_type": k.get("knowledge_type", ""),
            "confidence": _extract_confidence(parsed),
            "distance": k["distance"],
        })

    if include_tools:
        for t in tools_with_distance[:max(2, top_k // 2)]:
            results.append({
                "id": str(t["id"]),
                "record_type": "tool",
                "domain": t["domain"],
                "tool_name": t.get("tool_name", ""),
                "tool_type": t.get("tool_type", ""),
                "config_schema": _parse_jsonb(t.get("config_schema")),
                "usage": _parse_jsonb(t.get("usage_patterns")),
                "distance": t["distance"],
            })

    results.sort(key=lambda x: x["distance"], reverse=True)

    # Индексируем для будущих поисков
    for r in results[:top_k]:
        content_str = json.dumps(r.get("content") or r.get("usage") or {}, ensure_ascii=False)
        item_vec = await embed_text(content_str)
        await _index_l3_vector(r["id"], domain, r["record_type"], content_str, item_vec)

    return results


async def create_session(domain: str, results: list[dict]) -> dict:
    """Создаёт OP-сессию в Redis."""
    session_id = uuid4()
    r = await get_redis()

    session_key = f"session:{session_id}"
    session_data = {
        "session_id": str(session_id),
        "domain": domain,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "results": json.dumps(results, ensure_ascii=False),
    }
    await r.hset(session_key, mapping=session_data)
    await r.expire(session_key, SESSION_TTL)

    # Индексируем результаты для быстрого поиска
    for item in results:
        op_key = f"op:{item['id']}"
        op_data = {
            "id": item["id"],
            "domain": domain,
            "record_type": item["record_type"],
            "content_summary": json.dumps(item.get("content") or item.get("usage") or {}, ensure_ascii=False)[:500],
        }
        await r.hset(op_key, mapping=op_data)
        await r.expire(op_key, SESSION_TTL)

    return {
        "session_id": str(session_id),
        "domain": domain,
        "results": results,
        "expires_in": SESSION_TTL,
    }


async def close_session(session_id: UUID, keep_results: bool = False,
                        results_summary: dict | None = None,
                        source_agent: str = "user") -> dict:
    """Закрывает OP-сессию. Если keep — обратная связь в L1."""
    r = await get_redis()
    session_key = f"session:{session_id}"
    session_data = await r.hgetall(session_key)

    if not session_data:
        return {"status": "not_found", "session_id": str(session_id)}

    domain = session_data.get("domain", "default")

    if keep_results and results_summary:
        # Обратная связь: результаты работы → L1
        event_id = await save_raw_event(
            agent_id=source_agent,
            domain=domain,
            payload={
                "source": "operative_session",
                "session_id": str(session_id),
                "results": results_summary,
                "original_query": session_data.get("results", "{}"),
            },
        )
        await log_audit(
            agent_id=source_agent,
            action="feedback",
            target_table="l1_raw_events",
            target_id=event_id,
            details={"session_id": str(session_id)},
            success=True,
        )

    # Удаляем сессию и связанные ключи
    results_raw = session_data.get("results", "[]")
    try:
        results = json.loads(results_raw)
        for item in results:
            await r.delete(f"op:{item['id']}")
    except (json.JSONDecodeError, KeyError):
        pass

    await r.delete(session_key)

    return {
        "status": "closed",
        "session_id": str(session_id),
        "kept_results": keep_results,
    }


async def feedback_record(session_id: UUID, record_id: UUID, record_type: str, useful: bool) -> dict:
    """Обратная связь по конкретной записи в OP."""
    r = await get_redis()
    session_key = f"session:{session_id}"
    exists = await r.exists(session_key)
    if not exists:
        return {"status": "session_not_found"}

    feedback_key = f"feedback:{session_id}:{record_id}"
    await r.hset(feedback_key, mapping={
        "record_id": str(record_id),
        "record_type": record_type,
        "useful": "1" if useful else "0",
    })
    await r.expire(feedback_key, SESSION_TTL)

    return {"status": "recorded", "record_id": str(record_id), "useful": useful}


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Косинусное сходство двух векторов."""
    a_arr = np.array(a, dtype=np.float32)
    b_arr = np.array(b, dtype=np.float32)
    dot = np.dot(a_arr, b_arr)
    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _parse_jsonb(val):
    """Распаковывает JSONB-строку в dict/list. Возвращает как есть если не строка."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def _extract_confidence(content) -> float:
    """Извлекает confidence из контента L3-записи."""
    content = _parse_jsonb(content)
    if not content or not isinstance(content, dict):
        return 0.5
    for key in ("confidence", "avg_confidence", "confidence_score"):
        if key in content:
            try:
                return float(content[key])
            except (ValueError, TypeError):
                pass
    for key in ("patterns", "mistakes", "lessons", "new_or_updated"):
        items = content.get(key, [])
        if isinstance(items, list) and items:
            confs = []
            for item in items:
                if isinstance(item, dict) and "confidence" in item:
                    try:
                        confs.append(float(item["confidence"]))
                    except (ValueError, TypeError):
                        pass
            if confs:
                return sum(confs) / len(confs)
    return 0.5
