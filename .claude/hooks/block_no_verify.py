#!/usr/bin/env python3
"""Блокирует обход pre-commit хуков.

В CLAUDE.md есть правило «не пропускать pre-commit хуки (--no-verify)».
Прозой оно необязательное — модель может решить иначе под давлением.
Здесь оно становится гарантией.
"""
import json, re, sys

# Windows: стандартный вывод по умолчанию в кодировке системы (cp1251), и любой
# символ вне её роняет хук с UnicodeEncodeError. Хук падает МОЛЧА — харнесс
# видит ненулевой код возврата и просто не применяет решение, то есть защита
# выглядит установленной, но не работает. Форсируем UTF-8 до первой печати.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


data = json.load(sys.stdin)
cmd = (data.get("tool_input") or {}).get("command", "")

# --no-verify и -n у commit/push; -n только там, где он значит именно это
BYPASS = re.compile(r"(^|\s)(--no-verify|--no-gpg-sign\s+--no-verify)(\s|$)")

if BYPASS.search(cmd) and re.search(r"\bgit\s+(commit|push)\b", cmd):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "Обход pre-commit запрещён правилом проекта. Гейт ловит ruff, "
            "mypy, миграции и утечки ключей. Почини то, на что он ругается, "
            "а не отключай проверку."
        )}}, ensure_ascii=False))
sys.exit(0)
