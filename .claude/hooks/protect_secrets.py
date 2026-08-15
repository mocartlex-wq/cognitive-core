#!/usr/bin/env python3
"""Запрещает запись в файлы с секретами.

CLAUDE.md: «Никогда не коммитить .env — в нём реальные ключи API и пароли».
Правило прозой не мешает записать в файл; это — мешает.
Читать не запрещаем, только писать: агенту иногда нужно свериться со схемой.
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
path = (data.get("tool_input") or {}).get("file_path", "") or ""
norm = path.replace("\\", "/")

PROTECTED = re.compile(
    r"(^|/)\.env($|\.[^/]*$)"          # .env, .env.local, .env.production
    r"|\.(pem|key|p12|pfx|jks)$"       # ключи и хранилища сертификатов
    r"|(^|/)id_(rsa|ed25519|ecdsa)$"   # приватные SSH-ключи
    r"|(^|/)\.htpasswd$",
    re.I)

# .env.example — шаблон без секретов, его править можно
if PROTECTED.search(norm) and not norm.endswith(".example"):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Запись в {path} запрещена: файл содержит секреты. "
            "Новые переменные добавляй в .env.example (шаблон без значений), "
            "а реальные значения владелец вписывает сам."
        )}}, ensure_ascii=False))
sys.exit(0)
