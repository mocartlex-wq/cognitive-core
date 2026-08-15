#!/usr/bin/env python3
"""Оставляет от прогона тестов только упавшее.

Не про правила, а про расход: по замерам 95% трат уходит в контекст,
а гейт гоняет ~496 тестов. Полный вывод — это тысячи токенов, которые
остаются в истории навсегда. Нужны только падения.

Переменная SHOW_FULL_TESTS=1 отключает фильтр, когда нужен полный вывод.
"""
import json, os, re, sys

# Windows: стандартный вывод по умолчанию в кодировке системы (cp1251), и любой
# символ вне её роняет хук с UnicodeEncodeError. Хук падает МОЛЧА — харнесс
# видит ненулевой код возврата и просто не применяет решение, то есть защита
# выглядит установленной, но не работает. Форсируем UTF-8 до первой печати.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


if os.environ.get("SHOW_FULL_TESTS"):
    sys.exit(0)

data = json.load(sys.stdin)
ti = data.get("tool_input") or {}
cmd = ti.get("command", "")

RUNNER = re.compile(r"^\s*(.*\b)?(pytest|python -m pytest|npm test|npm run test|vitest|jest)\b")

# уже отфильтровано человеком — не трогаем
if not RUNNER.search(cmd) or "|" in cmd or ">" in cmd:
    sys.exit(0)

filtered = (
    f"{cmd} 2>&1 | grep -E "
    "'(FAILED|ERROR|error:|✕|✗|assert|Traceback|^E |passed|failed|no tests)' "
    "| head -80"
)

new_input = dict(ti)
new_input["command"] = filtered
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "updatedInput": new_input}}, ensure_ascii=False))
sys.exit(0)
