"""Display active, owner-scoped registry records to the cabinet and MCP clients."""
import re
from uuid import UUID

from app.db.postgres import get_pool
from app.services.operative import _parse_jsonb

CARD_COLUMNS = "id, domain, tool_name, tool_type, description, usage_patterns"
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
        return "Значение скрыто: обнаружены признаки учётных данных."
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
        card["instructions"] = _display(usage.get("instructions") or usage.get("command")
                                        or usage.get("example"))
        card["notes"] = {key: _display(usage[key]) for key in
                         ("recipients", "author_check", "independent_use") if usage.get(key)}
    return card


async def read_shared_tool(tool_id: UUID, owner_user_id: str) -> dict | None:
    if not owner_user_id:
        return None
    pool = await get_pool()
    row = await pool.fetchrow(f"""SELECT {CARD_COLUMNS} FROM l3_tools_registry
                                  WHERE id=$1 AND owner_user_id=$2::uuid AND effective_to IS NULL""",
                              tool_id, owner_user_id)
    return resource_card(row, full=True) if row else None
