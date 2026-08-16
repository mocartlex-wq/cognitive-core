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

REASON = ("Обход pre-commit запрещён правилом проекта. Гейт ловит ruff, "
          "mypy, миграции и утечки ключей. Почини то, на что он ругается, "
          "а не отключай проверку.")


def deny(extra: str = "") -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON + extra}}, ensure_ascii=False))
    sys.exit(0)


# Git принимает однозначные сокращения длинных флагов, поэтому «--no-verify»
# литералом мало: «git commit --no-veri» проходит гейт точно так же. Проверено
# на репозитории с падающим pre-commit — коммит прошёл, гейт не запускался.
# Граница именно «veri»: «--no-ver» неоднозначен (verbose/verify) и git его
# отвергает, а «--no-verbose» — законный флаг, его блокировать нельзя.
BYPASS = re.compile(r"(^|\s)(--no-veri\w*|--no-gpg-sign\s+--no-veri\w*)(\s|$)")

if BYPASS.search(cmd) and re.search(r"\bgit\s+(commit|push)\b", cmd):
    deny()

# Подмена каталога хуков — обход мимо флагов вообще: гейт просто не запускается.
if re.search(r"\bcore\.hooksPath\b", cmd) and re.search(r"\bgit\b", cmd):
    deny(" Подмена core.hooksPath отключает гейт целиком.")

# Короткий флаг: у commit «-n» это --no-verify (у push — --dry-run, не трогаем).
# Кавычки вырезаем, чтобы «-n» в тексте сообщения не давал ложных срабатываний.
# Важно допустить буквы ПОСЛЕ n: «git commit -nm x» — валидная склейка, и без
# этого она проходит мимо хука. Проверено на репозитории с падающим pre-commit:
# «-nm» коммитил, пока условие требовало, чтобы n была последней буквой.
if re.search(r"\bgit\s+commit\b", cmd):
    unquoted = re.sub(r'"[^"]*"|\'[^\']*\'', "", cmd)
    if re.search(r"\s-[a-z]*n[a-z]*\b", unquoted):
        deny(" Короткий флаг -n у commit — тот же --no-verify.")

sys.exit(0)
