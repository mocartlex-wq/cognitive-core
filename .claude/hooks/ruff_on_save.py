#!/usr/bin/env python3
"""Прогоняет ruff по изменённому Python-файлу сразу после правки.

Ошибку видно на месте, а не через двадцать минут на гейте перед пушем.
Ничего не блокирует — только сообщает.
"""
import json
import os
import shutil
import subprocess
import sys

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

if not path.endswith(".py") or not os.path.exists(path):
    sys.exit(0)

ruff = shutil.which("ruff")
if not ruff:
    sys.exit(0)                      # ruff не поставлен — молчим

try:
    r = subprocess.run([ruff, "check", "--quiet", path],
                       capture_output=True, text=True, timeout=20)
except Exception:
    sys.exit(0)

if r.returncode != 0 and r.stdout.strip():
    print(f"ruff по {os.path.basename(path)}:\n{r.stdout.strip()[:1500]}", file=sys.stderr)
sys.exit(0)
