"""Shared env-var helpers.

NB: os.environ.get(NAME, DEFAULT) применяет DEFAULT только если ключа НЕТ.
docker-compose.prod.yml использует ${VAR:-} → переменная всегда есть,
но пустая строка. _env возвращает default и для отсутствующего ключа,
и для пустой строки. Дубликат паттерна из app/services/vision_analyzer.py
(PR #63) — вынесен сюда ради переиспользования.
"""
from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default
