import json

from app.security.validator import validate_llm_response
from app.services.llm_client import get_llm_client
from app.services.prompts import (
    get_daily_prompt,
    get_user_daily,
    get_user_retry,
    get_user_weekly,
    get_weekly_prompt,
    lang,
)


async def analyze_daily_events(events: list[dict], domain: str) -> dict:
    """Анализирует L1-события за день → возвращает patterns/mistakes/lessons."""
    if not events:
        return {"patterns": [], "mistakes": [], "lessons": [], "confidence": 0.0}

    system = get_daily_prompt(lang()).format(domain=domain)
    events_json = json.dumps(
        [{"id": str(e["id"]), "payload": e["raw_payload"]} for e in events],
        ensure_ascii=False,
        default=str,
    )
    l = lang()
    user = get_user_daily(l).format(events_json=events_json)

    client = get_llm_client("daily_analyzer")
    max_attempts = 3
    for attempt in range(max_attempts):
        raw = await client._try_call(client.primary_config, client.primary_model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        if raw is None:
            continue
        try:
            return validate_llm_response(json.dumps(raw), schema="daily")
        except ValueError:
            if attempt < max_attempts - 1:
                user = f"{user}\n\n{get_user_retry(l)}"
            else:
                raise

    return {"patterns": [], "mistakes": [], "lessons": [], "confidence": 0.0}


async def analyze_weekly(
    domain: str,
    current_l3: list[dict],
    current_tools: list[dict],
    weekly_buffers: list[dict],
) -> dict:
    """Обобщает недельные L2-буферы → new_or_updated + deprecated + tools."""
    l = lang()
    system = get_weekly_prompt(l).format(
        domain=domain,
        current_l3=json.dumps(current_l3, ensure_ascii=False, default=str),
        current_tools=json.dumps(current_tools, ensure_ascii=False, default=str),
        weekly_buffers=json.dumps(weekly_buffers, ensure_ascii=False, default=str),
    )

    client = get_llm_client("weekly_consolidator")
    max_attempts = 3
    for attempt in range(max_attempts):
        raw = await client._try_call(client.primary_config, client.primary_model, [
            {"role": "system", "content": system},
            {"role": "user", "content": get_user_weekly(l)},
        ])
        if raw is None:
            continue
        try:
            return validate_llm_response(json.dumps(raw), schema="weekly")
        except ValueError:
            if attempt >= max_attempts - 1:
                raise

    return {"new_or_updated": [], "deprecated_l3_ids": [], "tools": []}
