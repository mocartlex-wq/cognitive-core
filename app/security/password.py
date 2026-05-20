"""Пароли: argon2id-хеширование + проверка.

Используется argon2id (winner PHC 2015, рекомендован OWASP) через библиотеку
argon2-cffi (она уже в requirements.txt с предыдущей итерации).

Параметры по умолчанию у argon2.PasswordHasher() настроены под современное
железо и обновятся со временем (библиотека периодически бампит дефолты).
Если нужны явные параметры — можно создать PasswordHasher(time_cost=3, memory_cost=64MB, parallelism=4).

Проверка пароля:
  • verify_password(plain, stored_hash) → bool
  • не бросает исключений, всегда возвращает True/False
  • если параметры хеша устарели — отдельно вызывается needs_rehash()
"""
from __future__ import annotations

import logging

import argon2
import argon2.exceptions

logger = logging.getLogger(__name__)

# Singleton: создаём один раз, переиспользуем
_ph = argon2.PasswordHasher()

# Минимальная длина пароля. Лучше длиннее чем сложнее (NIST 800-63B).
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 256   # защита от DoS — argon2 от слишком длинного грузится


def validate_password_strength(plain: str) -> str | None:
    """Проверка пароля. Возвращает None если ок, иначе текст ошибки."""
    if not isinstance(plain, str):
        return "Пароль должен быть строкой"
    if len(plain) < MIN_PASSWORD_LEN:
        return f"Пароль слишком короткий — минимум {MIN_PASSWORD_LEN} символов"
    if len(plain) > MAX_PASSWORD_LEN:
        return f"Пароль слишком длинный — максимум {MAX_PASSWORD_LEN} символов"
    # Хочется чтобы был хоть какой-то микс. Но не давим — NIST не рекомендует
    # сложные требования (chars, digits, etc.) — длина важнее.
    if plain.strip() != plain:
        return "Пароль не должен начинаться или заканчиваться пробелами"
    return None


def hash_password(plain: str) -> str:
    """Захешировать пароль argon2id. Возвращает строку для хранения в БД."""
    return _ph.hash(plain)


def verify_password(plain: str, stored_hash: str | None) -> bool:
    """Проверить пароль. Возвращает True/False, не бросает исключений.

    Specifically:
      • False если stored_hash пустой
      • False если verify-mismatch (неверный пароль)
      • False если хеш битый / неверного формата
      • True только при успешной проверке
    """
    if not stored_hash:
        return False
    try:
        _ph.verify(stored_hash, plain)
        return True
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.InvalidHash:
        logger.warning("verify_password: invalid hash format in DB")
        return False
    except Exception as e:  # pragma: no cover
        logger.warning("verify_password: unexpected error: %s", e)
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True если параметры хеша устарели и пора пере-хешировать с новыми
    дефолтами (например, при следующем успешном входе)."""
    try:
        return _ph.check_needs_rehash(stored_hash)
    except Exception:
        return False
