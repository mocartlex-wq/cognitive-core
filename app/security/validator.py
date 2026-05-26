import json
import re

from app.security.sanitizer import _clean_string

REQUIRED_DAILY_FIELDS = {"patterns", "mistakes", "lessons"}
REQUIRED_ITEM_FIELDS = {"description", "confidence"}


def validate_llm_response(raw_text: str, schema: str = "daily") -> dict:
    """Валидирует JSON-ответ LLM. Выбрасывает ValueError при нарушениях."""
    # Парсинг JSON
    text = raw_text.strip()
    # Убрать возможные маркеры ```json ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM response is not valid JSON: {e}")

    if schema == "daily":
        return _validate_daily(data)
    elif schema == "weekly":
        return _validate_weekly(data)
    elif schema == "curator_filter":
        return _validate_curator_filter(data)
    elif schema == "curator_quality":
        return _validate_curator_quality(data)
    elif schema == "curator_audit":
        return _validate_curator_audit(data)
    raise ValueError(f"Unknown schema: {schema}")


def _validate_daily(data: dict) -> dict:
    missing = REQUIRED_DAILY_FIELDS - set(data.keys())
    if missing:
        raise ValueError(f"Missing fields in daily response: {missing}")

    for field in ["patterns", "mistakes", "lessons"]:
        items = data.get(field, [])
        if not isinstance(items, list):
            raise ValueError(f"'{field}' must be a list")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"'{field}[{i}]' must be an object")
            for req in REQUIRED_ITEM_FIELDS:
                if req not in item:
                    raise ValueError(f"'{field}[{i}]' missing '{req}'")
            conf = item.get("confidence", 0)
            if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
                raise ValueError(f"'{field}[{i}].confidence' must be in [0, 1], got {conf}")
            desc = item.get("description", "")
            desc = _clean_string(desc, [], f"{field}[{i}].description")

    return data


def _validate_weekly(data: dict) -> dict:
    if "new_or_updated" not in data and "deprecated_l3_ids" not in data:
        raise ValueError("Weekly response requires 'new_or_updated' or 'deprecated_l3_ids'")
    if "tools" in data and not isinstance(data["tools"], list):
        raise ValueError("'tools' must be a list")
    return data


def _validate_curator_filter(data: dict) -> dict:
    if "skip" not in data:
        raise ValueError("Curator filter response requires 'skip' field")
    return data


def _validate_curator_quality(data: dict) -> dict:
    for field in ["ready_for_l3", "not_ready_for_l3", "deprecated_l3"]:
        if field in data and not isinstance(data[field], list):
            raise ValueError(f"'{field}' must be a list")
    return data


def _validate_curator_audit(data: dict) -> dict:
    if "health_score" not in data:
        raise ValueError("Curator audit response requires 'health_score'")
    return data
