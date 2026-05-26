"""Unit tests для app/security/secrets_vault.py.

Покрывает:
  - encrypt/decrypt round-trip (UTF-8 ASCII + Cyrillic + emoji)
  - mask() для разных длин
  - decrypt с corrupted ciphertext → SecretsVaultError
  - decrypt с wrong-key ciphertext → SecretsVaultError
  - encrypt с пустой строкой → SecretsVaultError
"""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def reset_vault_singleton(monkeypatch):
    """Сбросить singleton перед каждым тестом — иначе ключи перетекают."""
    # Подсовываем deterministic master key для test-сессии
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("COGCORE_SECRETS_MASTER_KEY", key)

    # Reset module-level кеш
    import app.security.secrets_vault as v
    v._FERNET = None
    v._KEY_SOURCE = "unset"
    yield


def test_encrypt_decrypt_ascii():
    from app.security.secrets_vault import decrypt, encrypt
    plain = "sk-abcdef1234567890XYZ"
    ct = encrypt(plain)
    assert isinstance(ct, bytes)
    assert plain.encode() not in ct  # не plaintext в bytes
    assert decrypt(ct) == plain


def test_encrypt_decrypt_cyrillic():
    from app.security.secrets_vault import decrypt, encrypt
    plain = "ключ-кириллица-абв-эфир"
    ct = encrypt(plain)
    assert decrypt(ct) == plain


def test_encrypt_decrypt_emoji():
    from app.security.secrets_vault import decrypt, encrypt
    plain = "key 🐲 with 🔑 emoji"
    ct = encrypt(plain)
    assert decrypt(ct) == plain


def test_encrypt_empty_string_raises():
    from app.security.secrets_vault import SecretsVaultError, encrypt
    with pytest.raises(SecretsVaultError):
        encrypt("")


def test_encrypt_non_string_raises():
    from app.security.secrets_vault import SecretsVaultError, encrypt
    with pytest.raises(SecretsVaultError):
        encrypt(None)  # type: ignore
    with pytest.raises(SecretsVaultError):
        encrypt(123)  # type: ignore


def test_decrypt_corrupted_raises():
    from app.security.secrets_vault import SecretsVaultError, decrypt
    with pytest.raises(SecretsVaultError):
        decrypt(b"not-a-real-fernet-token-corrupted")


def test_decrypt_wrong_key(monkeypatch):
    """Encrypt одним ключом → reset singleton → decrypt с другим ключом → fail."""
    from app.security import secrets_vault as v
    from app.security.secrets_vault import SecretsVaultError, decrypt, encrypt
    # Encrypt с первым ключом
    plain = "sensitive-data"
    ct = encrypt(plain)
    # Подменить ключ + сбросить singleton
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("COGCORE_SECRETS_MASTER_KEY", new_key)
    v._FERNET = None
    v._KEY_SOURCE = "unset"
    with pytest.raises(SecretsVaultError):
        decrypt(ct)


def test_decrypt_non_bytes_raises():
    from app.security.secrets_vault import SecretsVaultError, decrypt
    with pytest.raises(SecretsVaultError):
        decrypt("not-bytes")  # type: ignore


def test_mask_short_string():
    from app.security.secrets_vault import mask
    assert mask("abc") == "***"
    assert mask("12345678") == "********"  # ровно 8


def test_mask_long_string():
    from app.security.secrets_vault import mask
    masked = mask("sk-abcdef1234567890XYZ")
    # Первые 4 + ... + последние 4
    assert masked.startswith("sk-a")
    assert masked.endswith("0XYZ")
    assert "..." in masked
    # Не содержит middle plaintext
    assert "bcdef123" not in masked


def test_mask_empty_string():
    from app.security.secrets_vault import mask
    assert mask("") == ""
    assert mask(None) == ""  # type: ignore


def test_key_source_env():
    from app.security.secrets_vault import is_production_ready, key_source
    # autouse fixture устанавливает env-key
    assert key_source() == "env"
    assert is_production_ready() is True


def test_key_source_generated_when_no_env(monkeypatch):
    """Без env-key — singleton генерирует ephemeral + key_source = 'generated'."""
    from app.security import secrets_vault as v
    monkeypatch.delenv("COGCORE_SECRETS_MASTER_KEY", raising=False)
    v._FERNET = None
    v._KEY_SOURCE = "unset"
    # Trigger lazy init
    from app.security.secrets_vault import (
        decrypt,
        encrypt,
        is_production_ready,
        key_source,
    )
    assert key_source() == "generated"
    assert is_production_ready() is False
    # Even with generated key, round-trip works в рамках одного процесса
    plain = "test-ephemeral"
    assert decrypt(encrypt(plain)) == plain


def test_key_source_invalid_env_raises(monkeypatch):
    """Невалидный env-key (не base64 32 bytes) → SecretsVaultError на load."""
    from app.security import secrets_vault as v
    from app.security.secrets_vault import SecretsVaultError, encrypt
    monkeypatch.setenv("COGCORE_SECRETS_MASTER_KEY", "not-a-valid-fernet-key")
    v._FERNET = None
    v._KEY_SOURCE = "unset"
    with pytest.raises(SecretsVaultError):
        encrypt("anything")


def test_decrypt_invalid_utf8(monkeypatch):
    """Если cipher-result не UTF-8 (намеренно corruption через manual Fernet) → SecretsVaultError."""
    from cryptography.fernet import Fernet

    from app.security.secrets_vault import SecretsVaultError, decrypt
    # Encrypt invalid UTF-8 напрямую через Fernet используя текущий ключ
    key = os.environ["COGCORE_SECRETS_MASTER_KEY"].encode()
    f = Fernet(key)
    bad_bytes_token = f.encrypt(b"\xff\xfe\xfd not valid utf-8 \x80\x81")
    with pytest.raises(SecretsVaultError):
        decrypt(bad_bytes_token)
