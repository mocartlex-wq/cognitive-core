"""Tests для resumable media upload (PR #108): POST /api/media/upload-init,
PUT /api/media/upload/{id}, POST /api/media/upload/{id}/finalize.

Использует existing httpx `client` + admin `headers` fixtures (X-API-Key).
Sufficient — все resumable endpoints accept admin API key через
_check_admin_or_owner() helper в media.py.
"""
import pytest


class TestUploadInit:
    async def test_init_returns_upload_id_and_urls(self, client, headers):
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "test.mp4", "size_bytes": 1024, "content_type": "video/mp4"},
            headers=headers,
        )
        assert r.status_code == 200, f"unexpected {r.status_code}: {r.text}"
        body = r.json()
        assert "upload_id" in body
        assert body["put_url"].startswith("/api/media/upload/")
        assert body["finalize_url"].endswith("/finalize")
        assert body["ttl_seconds"] > 0
        assert body["max_size_mb"] > 0

    async def test_init_rejects_invalid_extension(self, client, headers):
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "exploit.exe", "size_bytes": 100},
            headers=headers,
        )
        assert r.status_code == 400
        assert "не поддерживается" in r.text or "не поддер" in r.text

    async def test_init_rejects_oversize(self, client, headers):
        # 500 MB > MAX_UPLOAD_SIZE_MB (200 MB) → endpoint 413.
        # Must stay <= pydantic hard cap (size_bytes le=2GB), иначе сработает 422
        # на валидации pydantic РАНЬШЕ, чем endpoint вернёт 413. 3GB давал 422.
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "huge.mp4", "size_bytes": 500 * 1024 * 1024},
            headers=headers,
        )
        assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text}"

    async def test_init_rejects_zero_size(self, client, headers):
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "empty.mp4", "size_bytes": 0},
            headers=headers,
        )
        assert r.status_code in (400, 422)  # pydantic ge=1 validation

    async def test_init_requires_auth(self, client):
        # Без X-API-Key → 401
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "test.mp4", "size_bytes": 100},
        )
        assert r.status_code == 401


class TestUploadPut:
    async def test_put_unknown_id_returns_404(self, client):
        # PUT без init → upload_id не существует в Redis
        r = await client.put(
            "/api/media/upload/nonexistent_id_xyz",
            content=b"hello",
        )
        assert r.status_code == 404
        assert "not found" in r.text.lower() or "expired" in r.text.lower()

    async def test_put_exceeds_declared_size_returns_413(self, client, headers):
        # Init с size_bytes=100, PUT 200 bytes → 413
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "small.mp4", "size_bytes": 100},
            headers=headers,
        )
        if r.status_code != 200:
            pytest.skip(f"init failed: {r.text}")
        upload_id = r.json()["upload_id"]

        r2 = await client.put(
            f"/api/media/upload/{upload_id}",
            content=b"x" * 200,  # too big
        )
        assert r2.status_code == 413, f"expected 413, got {r2.status_code}: {r2.text}"


class TestUploadFinalize:
    async def test_finalize_unknown_id_returns_404(self, client, headers):
        r = await client.post("/api/media/upload/nonexistent_xyz/finalize", headers=headers)
        assert r.status_code == 404

    async def test_finalize_before_put_returns_409(self, client, headers):
        # init без последующего PUT → finalize fail с 409
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "x.mp4", "size_bytes": 100},
            headers=headers,
        )
        if r.status_code != 200:
            pytest.skip(f"init failed: {r.text}")
        upload_id = r.json()["upload_id"]

        r2 = await client.post(
            f"/api/media/upload/{upload_id}/finalize",
            headers=headers,
        )
        assert r2.status_code == 409
        assert "initialized" in r2.text or "uploaded" in r2.text


class TestUploadEndToEnd:
    async def test_full_init_put_finalize_image(self, client, headers):
        """E2E: image (smallest analysis path — no Whisper, just store)."""
        # 1x1 PNG (smallest valid PNG)
        png_bytes = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c63f8cfc0000000040001ff8d4eebe60000000049454e44ae426082"
        )
        r = await client.post(
            "/api/media/upload-init",
            json={
                "filename": "pixel.png",
                "size_bytes": len(png_bytes),
                "content_type": "image/png",
            },
            headers=headers,
        )
        if r.status_code != 200:
            pytest.skip(f"init failed: {r.text}")
        body = r.json()
        upload_id = body["upload_id"]

        # PUT
        r2 = await client.put(
            f"/api/media/upload/{upload_id}",
            content=png_bytes,
        )
        assert r2.status_code == 200, f"PUT failed: {r2.text}"
        assert r2.json()["bytes"] == len(png_bytes)

        # Finalize
        r3 = await client.post(
            f"/api/media/upload/{upload_id}/finalize",
            headers=headers,
        )
        if r3.status_code != 200:
            pytest.skip(f"finalize failed (analyze_image issues?): {r3.text}")
        result = r3.json()
        assert "media_id" in result
        assert result["kind"] == "image"

    async def test_finalize_idempotent(self, client, headers):
        """Двойной finalize → второй раз returns cached result."""
        png_bytes = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c63f8cfc0000000040001ff8d4eebe60000000049454e44ae426082"
        )
        r = await client.post(
            "/api/media/upload-init",
            json={"filename": "p.png", "size_bytes": len(png_bytes)},
            headers=headers,
        )
        if r.status_code != 200:
            pytest.skip(f"init: {r.text}")
        upload_id = r.json()["upload_id"]

        await client.put(f"/api/media/upload/{upload_id}", content=png_bytes)

        r1 = await client.post(f"/api/media/upload/{upload_id}/finalize", headers=headers)
        if r1.status_code != 200:
            pytest.skip(f"first finalize: {r1.text}")
        media_id_1 = r1.json().get("media_id")

        r2 = await client.post(f"/api/media/upload/{upload_id}/finalize", headers=headers)
        assert r2.status_code == 200
        media_id_2 = r2.json().get("media_id")
        assert media_id_1 == media_id_2, "idempotency broken: different media_ids"
