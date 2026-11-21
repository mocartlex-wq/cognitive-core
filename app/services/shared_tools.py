"""Display active, owner-scoped registry records to the cabinet and MCP clients."""
import json
import re
from uuid import UUID

from app.db.postgres import get_pool
from app.services.operative import _parse_jsonb

CARD_COLUMNS = "id, domain, tool_name, tool_type, description, usage_patterns"
REGISTRY_COLUMNS = CARD_COLUMNS + ", config_schema, version, created_at"
_REDACTED = "Значение скрыто: обнаружены признаки учётных данных."
_SECRET_KEY = re.compile(r"(?:^|[_-])(?:password|passwd|secret|token|authorization|"
                         r"api[_-]?key|room[_-]?key|private[_-]?key|credential)$", re.I)
CREDENTIAL_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:rk_|sk-(?:proj-)?|ghp_|github_pat_|AKIA)[a-zA-Z0-9_-]{12,}|"
    r"\bBearer\s+[a-zA-Z0-9._~+/=-]{16,}|"
    r"(?:[?&]|\b)(?:api[_-]?key|access[_-]?token|password|client_secret)\s*[=:]\s*"
    r"['\"]?[a-zA-Z0-9._~+/=-]{8,}", re.I,
)


def card_usage(row) -> dict:
    value = _parse_jsonb(row["usage_patterns"])
    return value if isinstance(value, dict) else {}


def _display(value) -> str:
    if not isinstance(value, str):
        return ""
    if CREDENTIAL_PATTERN.search(value):
        return _REDACTED
    return value


def resource_card(row, full=False):
    usage = card_usage(row)
    location = usage.get("location") or usage.get("local_path") or usage.get("url") or ""
    kind = "skill" if usage.get("resource_kind") == "skill" else row["tool_type"]
    scope = usage.get("scope")
    if scope not in ("local", "portable", "service"):
        scope = "local" if usage.get("local_path") else None
    card = {
        "id": str(row["id"]), "name": _display(row["tool_name"]), "kind": kind,
        "domain": _display(row["domain"]), "description": _display(row["description"]), "scope": scope,
    }
    if full:
        card["location"] = _display(location)
        instructions = usage.get("instructions") or usage.get("command") or usage.get("example")
        if not instructions and usage.get("pattern") != row["description"]:
            instructions = usage.get("pattern")
        card["instructions"] = _display(instructions)
        card["notes"] = {key: _display(usage[key]) for key in
                         ("recipients", "author_check", "independent_use") if usage.get(key)}
    return card


def _safe_json(value, schema=False):
    if isinstance(value, list):
        return [_safe_json(item, schema) for item in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if schema and key == "properties" and isinstance(item, dict):
                # A schema may describe a credential without storing its value.
                properties = {}
                for name, definition in item.items():
                    if _SECRET_KEY.search(name) and isinstance(definition, dict):
                        definition = {k: _REDACTED if k in ("default", "const", "enum", "examples", "example") else v
                                      for k, v in definition.items()}
                    elif _SECRET_KEY.search(name):
                        definition = _REDACTED
                    properties[_display(name)] = _safe_json(definition, schema=True)
                cleaned[key] = properties
            else:
                cleaned[_display(key)] = _REDACTED if _SECRET_KEY.search(key) else _safe_json(item, schema)
        return cleaned
    return _display(value) if isinstance(value, str) else value


def agent_registry_record(row) -> dict:
    """Return the registration fields with the catalog's existing JSON wire types."""
    record = dict(row)
    record["id"] = str(record["id"])
    for field in ("tool_name", "domain", "description"):
        if isinstance(record[field], str):
            record[field] = _display(record[field])
    for field in ("config_schema", "usage_patterns"):
        raw = record[field]
        parsed = _parse_jsonb(raw)
        safe = _safe_json(parsed, schema=field == "config_schema")
        if safe != parsed:
            record[field] = json.dumps(safe, ensure_ascii=False) if isinstance(raw, str) else safe
    record["read_tool"] = "cognitive_read_tool"
    return record


async def read_shared_tool(tool_id: UUID, owner_user_id: str, *, include_registration=False) -> dict | None:
    if not owner_user_id:
        return None
    pool = await get_pool()
    columns = REGISTRY_COLUMNS if include_registration else CARD_COLUMNS
    row = await pool.fetchrow(f"""SELECT {columns} FROM l3_tools_registry
                                  WHERE id=$1 AND owner_user_id=$2::uuid AND effective_to IS NULL""",
                              tool_id, owner_user_id)
    if not row:
        return None
    if include_registration:
        record = agent_registry_record(row)
        return {**resource_card(record, full=True), **record}
    return resource_card(row, full=True)
