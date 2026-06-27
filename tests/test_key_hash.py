"""HMAC-based lookup hashing для agent_keys (новая infra, по умолчанию off)."""
from __future__ import annotations

import pytest

from app.security import key_hash


@pytest.fixture
def env_secret(monkeypatch):
    """Включает hashing с заданным секретом, чистит после теста."""
    def _set(value: str):
        monkeypatch.setenv("COGCORE_KEY_LOOKUP_SECRET", value)
    yield _set


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COGCORE_KEY_LOOKUP_SECRET", raising=False)
    assert key_hash.is_key_hashing_enabled() is False
    assert key_hash.compute_key_hmac("any-key") is None
    assert key_hash.get_key_lookup_secret() is None


def test_enabled_when_secret_set(env_secret):
    env_secret("super-secret-32-bytes-or-more!!")
    assert key_hash.is_key_hashing_enabled() is True
    h = key_hash.compute_key_hmac("rk_abc")
    assert h is not None
    assert len(h) == 64  # hex digest of SHA-256


def test_deterministic_for_same_secret(env_secret):
    env_secret("secret-A")
    h1 = key_hash.compute_key_hmac("rk_abc")
    h2 = key_hash.compute_key_hmac("rk_abc")
    assert h1 == h2


def test_different_secrets_yield_different_hashes(env_secret, monkeypatch):
    env_secret("secret-A")
    h_a = key_hash.compute_key_hmac("rk_abc")
    monkeypatch.setenv("COGCORE_KEY_LOOKUP_SECRET", "secret-B")
    h_b = key_hash.compute_key_hmac("rk_abc")
    assert h_a != h_b


def test_different_keys_yield_different_hashes(env_secret):
    env_secret("S")
    assert key_hash.compute_key_hmac("rk_abc") != key_hash.compute_key_hmac("rk_def")


def test_whitespace_secret_treated_as_unset(monkeypatch):
    monkeypatch.setenv("COGCORE_KEY_LOOKUP_SECRET", "   ")
    assert key_hash.is_key_hashing_enabled() is False
    assert key_hash.compute_key_hmac("rk_abc") is None


def test_verify_key_against_hmac_constant_time(env_secret):
    env_secret("S")
    h = key_hash.compute_key_hmac("rk_abc")
    assert key_hash.verify_key_against_hmac("rk_abc", h) is True
    assert key_hash.verify_key_against_hmac("rk_def", h) is False


def test_verify_returns_false_when_no_secret_or_no_hmac(monkeypatch):
    monkeypatch.delenv("COGCORE_KEY_LOOKUP_SECRET", raising=False)
    assert key_hash.verify_key_against_hmac("rk_abc", "deadbeef") is False
    assert key_hash.verify_key_against_hmac("rk_abc", None) is False
    assert key_hash.verify_key_against_hmac("rk_abc", "") is False


def test_empty_key_returns_none(env_secret):
    env_secret("S")
    assert key_hash.compute_key_hmac("") is None
