import json
from app.services.llm_client import get_llm_client
from app.security.validator import validate_llm_response
from app.config import settings
from app.services.prompts import get_filter_prompt, get_quality_prompt, get_audit_prompt, lang


async def pre_daily_filter(events: list[dict], domain: str) -> dict:
    """Куратор фильтрует шум перед daily consolidation."""
    if len(events) < settings.min_events_for_daily:
        return {"skip": True, "filtered_event_ids": [], "noise_event_ids": [], "reason": "Not enough events"}

    system = get_filter_prompt(lang()).format(domain=domain, min_events=settings.min_events_for_daily)
    events_json = json.dumps([{"id": str(e["id"]), "payload": e.get("raw_payload", e.get("payload", {}))} for e in events], ensure_ascii=False, default=str)

    client = get_llm_client("curator_filter")
    try:
        raw = await client._try_call(client.primary_config, client.primary_model, [
            {"role": "system", "content": system},
            {"role": "user", "content": events_json},
        ])
        if raw:
            return validate_llm_response(json.dumps(raw), schema="curator_filter")
    except Exception:
        pass

    all_ids = [str(e["id"]) for e in events]
    return {"skip": False, "filtered_event_ids": all_ids, "noise_event_ids": [], "reason": "Skipped curator (all passed)"}


async def pre_weekly_check(
    domain: str,
    current_l3: list[dict],
    l2_buffers: list[dict],
) -> dict:
    """Куратор проверяет качество перед weekly consolidation."""
    if not l2_buffers:
        return {"ready_for_l3": [], "not_ready_for_l3": [], "deprecated_l3": [], "conflicts": [], "deduplicated_to_existing": []}

    system = get_quality_prompt(lang()).format(
        domain=domain,
        min_repetitions=settings.min_l2_repetitions_for_l3,
        min_confidence=settings.min_confidence_for_l3,
    )
    user = json.dumps({
        "current_l3": [{"id": str(k.get("id", "")), "content": k.get("content", {}), "knowledge_type": k.get("knowledge_type", "")} for k in current_l3],
        "l2_buffers": [{"id": str(b.get("id", "")), "summary": b.get("summary", {})} for b in l2_buffers],
    }, ensure_ascii=False, default=str)

    client = get_llm_client("curator_quality")
    try:
        raw = await client._try_call(client.primary_config, client.primary_model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        if raw:
            return validate_llm_response(json.dumps(raw), schema="curator_quality")
    except Exception:
        pass

    return {"ready_for_l3": [], "not_ready_for_l3": [], "deprecated_l3": [], "conflicts": [], "deduplicated_to_existing": []}


async def monthly_audit(domain: str, l3_knowledge: list[dict], l3_tools: list[dict]) -> dict:
    """Ежемесячная ревизия L3."""
    from datetime import datetime as dt, timezone as tz
    now_iso = dt.now(tz.utc).strftime("%Y-%m-%d")
    system = get_audit_prompt(lang()).format(
        domain=domain,
        now=now_iso,
        staleness_days=settings.l3_staleness_days,
        unused_days=settings.tool_unused_days,
    )
    user = json.dumps({
        "current_date": now_iso,
        "staleness_threshold_days": settings.l3_staleness_days,
        "unused_threshold_days": settings.tool_unused_days,
        "knowledge": [{"id": str(k.get("id", "")), "content": k.get("content", {}), "knowledge_type": k.get("knowledge_type", ""), "created_at": str(k.get("created_at", "")), "effective_to": str(k.get("effective_to", ""))} for k in l3_knowledge],
        "tools": [{"id": str(t.get("id", "")), "tool_name": t.get("tool_name", ""), "tool_type": t.get("tool_type", ""), "created_at": str(t.get("created_at", "")), "effective_to": str(t.get("effective_to", ""))} for t in l3_tools],
    }, ensure_ascii=False, default=str)

    client = get_llm_client("curator_audit")
    try:
        raw = await client._try_call(client.primary_config, client.primary_model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        if raw:
            return validate_llm_response(json.dumps(raw), schema="curator_audit")
    except Exception:
        pass

    return {"stale_knowledge_ids": [], "internal_conflicts": [], "dead_tool_ids": [], "duplicate_pairs": [], "health_score": 1.0, "recommendations": "Audit skipped (error)"}
