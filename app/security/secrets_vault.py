"""Fernet-based encryption для секретов хранящихся в БД.

Используется для per-tenant external AI provider keys (user_external_keys),
а в будущем — для любых других секретов которые нельзя держать в plaintext.

Дизайн:
  - Master key из env COGCORE_SECRETS_MASTER_KEY (32-byte base64).
  - Если env не задан — генерируем ephemeral key на старте + warning в логи.
    Это OK для dev/test, но опасно в проде: при рестарте ключ меняется и
    все ранее зашифрованные значения становятся unreadable. Owner ОБЯЗАТЕЛЬНО
    должен задать COGCORE_SECRETS_MASTER_KEY в .env на production-сервере.
  - encrypt(plaintext: str) -> bytes      — Fernet token (raw bytes для BYTEA).
  - decrypt(ciphertext: bytes) -> str     — обратная операция.
  - mask(plaintext: str) -> str           — для UI display (показать только
                                            первые 3 + последние 4 символа).

Security guarantees:
  - НЕ логируем plaintext или ciphertext (даже в exception messages).
  - Ошибки decrypt() поднимают SecretsVaultError с generic msg.
  - Master key генерируется через cryptography.fernet.Fernet.generate_key()
    (urlsafe base64 32-byte from os.urandom).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class SecretsVaultError(Exception):
    """Generic vault error — намеренно без деталей чтобы не светить crypto state."""
    pass


_FERNET: Optional[Fernet] = None
_KEY_SOURCE: str = "unset"   # "env" | "generated" | "unset" — для health-check


def _load_master_key() -> bytes:
    """Загрузить master key из env или сгенерировать ephemeral для dev."""
    global _KEY_SOURCE
    env_key = os.environ.get("COGCORE_SECRETS_MASTER_KEY", "").strip()
    if env_key:
        try:
            # Fernet принимает urlsafe-base64 32-byte ключ. Validate.
            key_bytes = env_key.encode("ascii")
            Fernet(key_bytes)  # raises ValueError если invalid
            _KEY_SOURCE = "env"
            logger.info("secrets_vault: master key loaded from env")
            return key_bytes
        except (ValueError, TypeError):
            logger.error(
                "secrets_vault: COGCORE_SECRETS_MASTER_KEY невалиден "
                "(должен быть urlsafe-base64 32 байта). "
                "Сгенерируйте: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
            raise SecretsVaultError("master key invalid — see logs")

    # Ephemeral key для dev — генерируем на старте.
    key = Fernet.generate_key()
    _KEY_SOURCE = "generated"
    logger.warning(
        "secrets_vault: COGCORE_SECRETS_MASTER_KEY не задан — "
        "сгенерирован эфемерный ключ. ВНИМАНИЕ: при рестарте все "
        "зашифрованные значения в БД станут unreadable. "
        "Установите COGCORE_SECRETS_MASTER_KEY в .env на production."
    )
    return key


def _get_fernet() -> Fernet:
    """Lazy singleton. Не пытаемся загружать ключ при import — только при первом use."""
    global _FERNET
    if _FERNET is None:
        _FERNET = Fernet(_load_master_key())
    return _FERNET


def encrypt(plaintext: str) -> bytes:
    """Зашифровать строку → Fernet token (bytes для BYTEA-колонки).

    Никогда не логируем plaintext или результат.
    """
    if not isinstance(plaintext, str):
        raise SecretsVaultError("plaintext must be str")
    if not plaintext:
        raise SecretsVaultError("plaintext must be non-empty")
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Расшифровать Fernet token → plaintext.

    Raises SecretsVaultError с generic msg если token corrupted / ключ не тот.
    Никогда не логируем ciphertext или partial-plaintext.
    """
    if not isinstance(ciphertext, (bytes, bytearray, memoryview)):
        raise SecretsVaultError("ciphertext must be bytes-like")
    f = _get_fernet()
    try:
        return f.decrypt(bytes(ciphertext)).decode("utf-8")
    except (InvalidToken, ValueError):
        # InvalidToken: wrong key / corrupted ciphertext. Не светим причину.
        raise SecretsVaultError("decryption failed — see operator logs")
    except UnicodeDecodeError:
        raise SecretsVaultError("decrypted data is not valid UTF-8")


def mask(plaintext: str) -> str:
    """Замаскировать ключ для UI display.

    Показываем первые 3 + последние 4 символа, остальное звёздочками.
    Если строка короткая (<= 8 символов) — полностью звёздочки.

    Примеры:
      "sk-abcdef1234567890XYZ" → "sk-a***XYZ"
      "abc12345"               → "********"
      ""                        → ""
    """
    if not plaintext:
        return ""
    s = str(plaintext)
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}...{s[-4:]}"


def key_source() -> str:
    """Возвращает 'env' / 'generated' / 'unset' — для health-check."""
    # Force-load на первом обращении чтобы _KEY_SOURCE был корректен.
    _get_fernet()
    return _KEY_SOURCE


def is_production_ready() -> bool:
    """True если master key пришёл из env (не ephemeral)."""
    return key_source() == "env"
