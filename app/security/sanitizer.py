import json
import re

from app.config import settings

# Паттерны для обнаружения атак
HTML_PATTERN = re.compile(
    r"<script[^>]*>|<iframe[^>]*>|<object[^>]*>|<embed[^>]*>|"
    r"<img[^>]*onerror|<svg[^>]*onload|javascript:|<style[^>]*>",
    re.IGNORECASE,
)
SQL_PATTERN = re.compile(
    r"\b(EXEC|EXECUTE|DROP\s+TABLE|DROP\s+DATABASE|ALTER\s+TABLE|"
    r"INSERT\s+INTO|UPDATE\b|DELETE\s+FROM|UNION\s+SELECT|"
    r"SELECT\s+.*\s+FROM|--[^\n]*$|'.*OR\s+'1'='1)",
    re.IGNORECASE,
)
SHELL_PATTERN = re.compile(
    r"\b(rm\s+-rf|curl\s+|wget\s+|/bin/bash|/bin/sh|"
    r"&&\s*|`[^`]+`|\$\([^)]+\)|eval\s+|exec\s*\()",
    re.IGNORECASE,
)
JS_PATTERN = re.compile(
    r"\b(eval\s*\(|Function\s*\(|setTimeout\s*\(|setInterval\s*\(|"
    r"document\.cookie|window\.location|XMLHttpRequest|fetch\s*\()",
    re.IGNORECASE,
)


class SanitizeResult:
    def __init__(self, payload: dict, warnings: list[str]):
        self.payload = payload
        self.warnings = warnings


def sanitize_payload(payload: dict) -> SanitizeResult:
    """Проверяет и очищает payload события. Выбрасывает ValueError при нарушении."""
    warnings = []

    # Проверка размера
    raw = json.dumps(payload, ensure_ascii=False)
    size = len(raw.encode("utf-8"))
    if size > settings.max_payload_size:
        raise ValueError(f"Payload size {size} exceeds max {settings.max_payload_size}")

    # Проверка глубины вложенности
    depth = _get_depth(payload)
    if depth > settings.max_payload_depth:
        raise ValueError(f"Payload depth {depth} exceeds max {settings.max_payload_depth}")

    # Проверка количества ключей
    key_count = _count_keys(payload)
    if key_count > settings.max_payload_keys:
        raise ValueError(f"Payload keys {key_count} exceeds max {settings.max_payload_keys}")

    # Сканирование и санитизация строк
    cleaned = _sanitize_dict(payload, warnings)

    return SanitizeResult(payload=cleaned, warnings=warnings)


def _get_depth(obj, current=0):
    if isinstance(obj, dict):
        if not obj:
            return current
        return max(_get_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current
        return max(_get_depth(v, current) for v in obj)
    return current


def _count_keys(obj) -> int:
    if isinstance(obj, dict):
        return len(obj) + sum(_count_keys(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_keys(v) for v in obj)
    return 0


def _sanitize_dict(obj, warnings, parent_key=""):
    if isinstance(obj, dict):
        return {k: _sanitize_dict(v, warnings, f"{parent_key}.{k}" if parent_key else k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_dict(v, warnings, parent_key) for v in obj]
    if isinstance(obj, str):
        return _clean_string(obj, warnings, parent_key)
    return obj


def _clean_string(value: str, warnings: list, key_path: str) -> str:
    # SQL_PATTERN check removed 2026-05-26 per ewewew feedback:
    # все queries в app/ параметризованные (asyncpg $1/$2) — injection невозможна.
    # Прошлый filter блокировал валидные em-dash, double-hyphens, shell args
    # (e.g. "pytest -- -k", lessons про SQL injection).
    if JS_PATTERN.search(value):
        raise ValueError(f"JavaScript injection detected in field '{key_path}'")
    if SHELL_PATTERN.search(value):
        warnings.append(f"Shell command escaped in '{key_path}'")
        value = re.sub(r"[;&|`$()]", lambda m: f"\\{m.group(0)}", value)
    if HTML_PATTERN.search(value):
        warnings.append(f"HTML tags escaped in '{key_path}'")
        value = value.replace("<", "&lt;").replace(">", "&gt;")
    return value
