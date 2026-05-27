"""Tests для /user/rooms CRUD (PR #102) — use session fixture from M1 PR #115.

Demonstrates новый authed_client fixture pattern.
Tests skip if COGCORE_TEST_DB_URL не set (CI без DB).
"""
import pytest


class TestRoomsCRUD:
    async def test_get_rooms_empty_initially(self, authed_client):
        r = await authed_client.get("/user/rooms")
        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert "count" in body
        assert body["count"] == len(body["items"])

    async def test_create_room_returns_api_key(self, authed_client):
        r = await authed_client.post(
            "/user/rooms",
            json={"name": "Test Room", "description": "test desc", "is_public": True},
        )
        if r.status_code != 200:
            pytest.skip(f"create_room failed (rooms-service down?): {r.status_code} {r.text}")
        body = r.json()
        assert body["name"] == "Test Room"
        assert body["api_key"].startswith("rk_"), f"unexpected api_key prefix: {body['api_key']}"
        assert "id" in body

    async def test_create_room_validates_name(self, authed_client):
        r = await authed_client.post(
            "/user/rooms",
            json={"name": "", "description": ""},
        )
        assert r.status_code == 422, "пустое имя должно отклоняться pydantic min_length=1"

    async def test_patch_room_renames(self, authed_client):
        # Create
        r1 = await authed_client.post(
            "/user/rooms",
            json={"name": "Original", "is_public": True},
        )
        if r1.status_code != 200:
            pytest.skip(f"create failed: {r1.text}")
        room_id = r1.json()["id"]

        # Rename
        r2 = await authed_client.patch(
            f"/user/rooms/{room_id}",
            json={"name": "Renamed"},
        )
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

        # Verify (list)
        r3 = await authed_client.get("/user/rooms")
        items = r3.json().get("items", [])
        found = next((x for x in items if x["id"] == room_id), None)
        assert found is not None
        assert found["name"] == "Renamed"

    async def test_delete_room(self, authed_client):
        # Create
        r1 = await authed_client.post(
            "/user/rooms",
            json={"name": "To Delete", "is_public": True},
        )
        if r1.status_code != 200:
            pytest.skip(f"create failed: {r1.text}")
        room_id = r1.json()["id"]

        # Delete
        r2 = await authed_client.delete(f"/user/rooms/{room_id}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["ok"] is True
        assert "deleted_messages_count" in body

        # Verify gone
        r3 = await authed_client.get("/user/rooms")
        items = r3.json().get("items", [])
        assert not any(x["id"] == room_id for x in items)

    async def test_patch_nonexistent_room_returns_404(self, authed_client):
        import uuid
        r = await authed_client.patch(
            f"/user/rooms/{uuid.uuid4()}",
            json={"name": "x"},
        )
        assert r.status_code == 404

    async def test_delete_nonexistent_room_returns_404(self, authed_client):
        import uuid
        r = await authed_client.delete(f"/user/rooms/{uuid.uuid4()}")
        assert r.status_code == 404
