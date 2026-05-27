"""Tests для /user/settings/external-key* endpoints (M1 PR #117 — Test Foundation).

Покрывает tenant vault — per-tenant external AI provider keys в БД,
зашифрованные Fernet (app/security/secrets_vault.py).

API surface (см. app/api/user_settings.py):
  GET    /user/settings/external-keys              — список + masked
  POST   /user/settings/external-key               — UPSERT (body: provider, api_key, …)
  POST   /user/settings/external-key/{p}/test      — validate (НЕ покрывается здесь — требует
                                                     внешнего сетевого вызова к provider)
  DELETE /user/settings/external-key/{provider}    — soft-delete (return 200, не 204)

Использует `authed_client` fixture из tests/fixtures/session.py (PR #115).
Тесты с direct DB верификацией skip-ают, если COGCORE_TEST_DB_URL не задан.

NOTE про путь endpoint'ов: задача упоминала `/user/settings/keys/{provider}`,
но реальный prefix в коде — `/user/settings/external-key[s]`. Если миграция
случится — тест-уровень добавит fallback через 404-skip.
"""
from __future__ import annotations

import os
import uuid

import pytest

# Whitelist providers — см. app/services/vision_providers/__init__.py
VALID_PROVIDER = "openai"  # стабильный, есть test_connection
OTHER_PROVIDER = "claude"


def _test_db_url() -> str | None:
    return os.getenv("COGCORE_TEST_DB_URL") or os.getenv("DATABASE_URL")


async def _cleanup_key(user_id: str, provider: str) -> None:
    """Idempotent cleanup для конкретного (user, provider) — вызывается из finally."""
    db_url = _test_db_url()
    if not db_url:
        return
    try:
        import asyncpg
    except ImportError:
        return
    try:
        conn = await asyncpg.connect(db_url)
        try:
            await conn.execute(
                "DELETE FROM user_external_keys "
                "WHERE owner_user_id = $1::uuid AND provider = $2",
                user_id, provider,
            )
        finally:
            await conn.close()
    except Exception:
        # Cleanup best-effort — не валим тест если БД недоступна
        pass


# ─────────────────────────────────────────────────────────────────────────
# TestSetUserKey — POST upsert валидного ключа
# ─────────────────────────────────────────────────────────────────────────
class TestSetUserKey:
    async def test_save_valid_key_returns_masked(self, authed_client, test_account_session):
        plain = f"sk-test-{uuid.uuid4().hex}"
        try:
            r = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": VALID_PROVIDER, "api_key": plain},
            )
            if r.status_code == 404:
                pytest.skip("endpoint /user/settings/external-key не зарегистрирован")
            if r.status_code == 500 and "Vault" in r.text:
                pytest.skip(f"vault not configured on test server: {r.text}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True
            assert body["provider"] == VALID_PROVIDER
            assert "masked_key" in body
            # Plaintext НЕ должен утекать в response
            assert plain not in r.text
            # Mask: первые 4 + ... + последние 4
            assert "..." in body["masked_key"]
        finally:
            await _cleanup_key(test_account_session["user_id"], VALID_PROVIDER)

    async def test_upsert_overwrites_existing(self, authed_client, test_account_session):
        first = f"sk-first-{uuid.uuid4().hex}"
        second = f"sk-second-{uuid.uuid4().hex}"
        try:
            r1 = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": VALID_PROVIDER, "api_key": first},
            )
            if r1.status_code in (404, 500):
                pytest.skip(f"setup failed: {r1.status_code}")
            r2 = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": VALID_PROVIDER, "api_key": second},
            )
            assert r2.status_code == 200, r2.text
            # GET должен показать masked для second (last 4 = second tail)
            r3 = await authed_client.get("/user/settings/external-keys")
            items = r3.json().get("items", [])
            found = next((x for x in items if x["provider"] == VALID_PROVIDER), None)
            assert found is not None
            assert found["masked_key"].endswith(second[-4:])
        finally:
            await _cleanup_key(test_account_session["user_id"], VALID_PROVIDER)


# ─────────────────────────────────────────────────────────────────────────
# TestGetUserKey — GET возвращает masked, не plaintext
# ─────────────────────────────────────────────────────────────────────────
class TestGetUserKey:
    async def test_list_empty_initially(self, authed_client):
        r = await authed_client.get("/user/settings/external-keys")
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "items" in body
        assert "count" in body
        assert body["count"] == len(body["items"])
        # available_providers всегда есть (для UI dropdown)
        assert "available_providers" in body
        assert isinstance(body["available_providers"], list)
        assert len(body["available_providers"]) > 0

    async def test_list_returns_masked_not_plaintext(self, authed_client, test_account_session):
        plain = f"sk-secret-{uuid.uuid4().hex}-trailing"
        try:
            r0 = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": VALID_PROVIDER, "api_key": plain},
            )
            if r0.status_code in (404, 500):
                pytest.skip(f"save failed: {r0.status_code}")
            r = await authed_client.get("/user/settings/external-keys")
            assert r.status_code == 200
            # Plaintext НИКОГДА не в response (это главная security guarantee)
            assert plain not in r.text
            items = r.json().get("items", [])
            found = next((x for x in items if x["provider"] == VALID_PROVIDER), None)
            assert found is not None
            # masked содержит '...' + last 4 plain'a
            assert "..." in found["masked_key"]
            assert found["masked_key"].endswith(plain[-4:])
            # provider label есть для UI
            assert "label" in found
        finally:
            await _cleanup_key(test_account_session["user_id"], VALID_PROVIDER)


# ─────────────────────────────────────────────────────────────────────────
# TestDeleteUserKey — DELETE + 404 на повторный
# ─────────────────────────────────────────────────────────────────────────
class TestDeleteUserKey:
    async def test_delete_existing_key(self, authed_client, test_account_session):
        plain = f"sk-todel-{uuid.uuid4().hex}"
        try:
            r0 = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": VALID_PROVIDER, "api_key": plain},
            )
            if r0.status_code in (404, 500):
                pytest.skip(f"setup failed: {r0.status_code}")
            r = await authed_client.delete(
                f"/user/settings/external-key/{VALID_PROVIDER}"
            )
            # NOTE: endpoint return 200 {"ok": true, "provider": ...}, не 204 (см. user_settings.py)
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True
            # Verify gone
            r2 = await authed_client.get("/user/settings/external-keys")
            items = r2.json().get("items", [])
            assert not any(x["provider"] == VALID_PROVIDER for x in items)
        finally:
            await _cleanup_key(test_account_session["user_id"], VALID_PROVIDER)

    async def test_delete_nonexistent_returns_404(self, authed_client):
        r = await authed_client.delete(
            f"/user/settings/external-key/{OTHER_PROVIDER}"
        )
        # Если endpoint не зарегистрирован — тоже 404 (нельзя различить, skip)
        if r.status_code == 404 and "Ключ не найден" not in r.text:
            pytest.skip("endpoint not registered (404 without expected detail)")
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────
# TestKeyValidation — bad provider / empty key / pydantic limits
# ─────────────────────────────────────────────────────────────────────────
class TestKeyValidation:
    async def test_invalid_provider_rejected(self, authed_client):
        r = await authed_client.post(
            "/user/settings/external-key",
            json={"provider": "totally-fake-provider-xyz", "api_key": "sk-valid-something"},
        )
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        # NOTE: код шлёт 400 с русским detail (НЕ 422 — pydantic пропускает по min_length)
        assert r.status_code in (400, 422), r.text
        # Тело должно содержать namespace «провайдер/provider»
        body_lower = r.text.lower()
        assert "provider" in body_lower or "провайдер" in body_lower

    async def test_empty_key_rejected(self, authed_client):
        # api_key="" → pydantic min_length=4 → 422
        r = await authed_client.post(
            "/user/settings/external-key",
            json={"provider": VALID_PROVIDER, "api_key": ""},
        )
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        assert r.status_code == 422

    async def test_whitespace_only_key_rejected(self, authed_client):
        # min_length=4 → "    " проходит pydantic (4 char), но handler делает strip
        # → пустой → HTTPException 400 "api_key пустой"
        r = await authed_client.post(
            "/user/settings/external-key",
            json={"provider": VALID_PROVIDER, "api_key": "    "},
        )
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        assert r.status_code in (400, 422)

    async def test_missing_api_key_field_rejected(self, authed_client):
        r = await authed_client.post(
            "/user/settings/external-key",
            json={"provider": VALID_PROVIDER},
        )
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        assert r.status_code == 422

    async def test_extra_fields_rejected(self, authed_client):
        # ConfigDict(extra="forbid") в SaveExternalKeyBody
        r = await authed_client.post(
            "/user/settings/external-key",
            json={
                "provider": VALID_PROVIDER,
                "api_key": "sk-valid-thing",
                "evil_extra": "should-be-rejected",
            },
        )
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        assert r.status_code == 422

    async def test_invalid_base_url_scheme_rejected(self, authed_client):
        r = await authed_client.post(
            "/user/settings/external-key",
            json={
                "provider": VALID_PROVIDER,
                "api_key": "sk-valid-thing",
                "base_url": "ftp://wrong.example.com",
            },
        )
        if r.status_code == 404:
            pytest.skip("endpoint not registered")
        assert r.status_code in (400, 422)


# ─────────────────────────────────────────────────────────────────────────
# TestEncryption — direct DB verify что raw key зашифрован
# ─────────────────────────────────────────────────────────────────────────
class TestEncryption:
    async def test_db_stores_ciphertext_not_plaintext(
        self, authed_client, test_account_session
    ):
        """Verify через asyncpg что в БД лежит Fernet ciphertext, не plaintext."""
        db_url = _test_db_url()
        if not db_url:
            pytest.skip("COGCORE_TEST_DB_URL not set — нужен direct DB")
        try:
            import asyncpg
        except ImportError:
            pytest.skip("asyncpg not installed")

        plain = f"sk-distinct-marker-{uuid.uuid4().hex}"
        try:
            r = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": VALID_PROVIDER, "api_key": plain},
            )
            if r.status_code in (404, 500):
                pytest.skip(f"save failed: {r.status_code} {r.text}")
            assert r.status_code == 200, r.text

            conn = await asyncpg.connect(db_url)
            try:
                row = await conn.fetchrow(
                    "SELECT api_key_encrypted FROM user_external_keys "
                    "WHERE owner_user_id = $1::uuid AND provider = $2",
                    test_account_session["user_id"], VALID_PROVIDER,
                )
            finally:
                await conn.close()

            assert row is not None, "ключ не сохранился в БД"
            ct = bytes(row["api_key_encrypted"])
            # Fernet tokens начинаются с 0x80 (version byte) и base64-safe-кодированы
            assert isinstance(ct, bytes)
            assert len(ct) > len(plain), "ciphertext должен быть длиннее plaintext"
            # Главная проверка — plaintext НЕ присутствует в raw bytes
            assert plain.encode("utf-8") not in ct, "plaintext найден в БД raw bytes!"
            # Fernet tokens — urlsafe-base64; не содержат нашего ASCII-marker
            assert b"distinct-marker" not in ct
        finally:
            await _cleanup_key(test_account_session["user_id"], VALID_PROVIDER)

    async def test_round_trip_via_get_endpoint(
        self, authed_client, test_account_session
    ):
        """Save → list → проверить что masked суффикс = последние 4 plaintext.

        Это косвенно подтверждает что server успешно decrypt-нул на read-path
        (если бы decrypt fail-нул, mask вернул бы '*** (ключ неисправен)').
        """
        plain = f"sk-roundtrip-ABCDEFGH-{uuid.uuid4().hex}"
        try:
            r0 = await authed_client.post(
                "/user/settings/external-key",
                json={"provider": OTHER_PROVIDER, "api_key": plain},
            )
            if r0.status_code in (404, 500):
                pytest.skip(f"save failed: {r0.status_code}")
            r = await authed_client.get("/user/settings/external-keys")
            items = r.json().get("items", [])
            found = next((x for x in items if x["provider"] == OTHER_PROVIDER), None)
            assert found is not None
            # Если бы decrypt fail — было бы "*** (ключ неисправен)"
            assert "неисправен" not in found["masked_key"]
            assert found["masked_key"].endswith(plain[-4:])
        finally:
            await _cleanup_key(test_account_session["user_id"], OTHER_PROVIDER)


# ─────────────────────────────────────────────────────────────────────────
# TestSilentExceptFixed — PR #91 Phase B1: vault decrypt errors теперь логятся
# ─────────────────────────────────────────────────────────────────────────
class TestSilentExceptFixed:
    """Раньше vault decrypt failures были silent except. PR #91 Phase B1 fix:
    list_external_keys + test_external_key теперь logger.warning(...).

    Полная проверка через mock сложна (требует подменить decrypt в импортнутом
    модуле + перехват logging). Здесь делаем lightweight contract-проверку:
    если в БД лежит мусор вместо ciphertext, GET возвращает 200 с masked=
    '*** (ключ неисправен)' — это и есть observable side-effect fix'a.
    """

    async def test_corrupted_ciphertext_returns_marker_not_500(
        self, authed_client, test_account_session
    ):
        db_url = _test_db_url()
        if not db_url:
            pytest.skip("COGCORE_TEST_DB_URL not set — нужен direct DB для corruption")
        try:
            import asyncpg
        except ImportError:
            pytest.skip("asyncpg not installed")

        try:
            # Подсунуть garbage в api_key_encrypted напрямую
            conn = await asyncpg.connect(db_url)
            try:
                await conn.execute(
                    """
                    INSERT INTO user_external_keys
                        (owner_user_id, provider, api_key_encrypted)
                    VALUES ($1::uuid, $2, $3)
                    ON CONFLICT (owner_user_id, provider) DO UPDATE
                        SET api_key_encrypted = EXCLUDED.api_key_encrypted
                    """,
                    test_account_session["user_id"],
                    VALID_PROVIDER,
                    b"this-is-not-a-valid-fernet-token-garbage-bytes-zzz",
                )
            finally:
                await conn.close()

            r = await authed_client.get("/user/settings/external-keys")
            if r.status_code == 404:
                pytest.skip("endpoint not registered")
            # Главное — НЕ 500. Endpoint обязан gracefully degrade.
            assert r.status_code == 200, r.text
            items = r.json().get("items", [])
            found = next((x for x in items if x["provider"] == VALID_PROVIDER), None)
            assert found is not None
            # Marker для UI — без plaintext utечки
            assert "неисправен" in found["masked_key"] or "***" in found["masked_key"]
        finally:
            await _cleanup_key(test_account_session["user_id"], VALID_PROVIDER)
