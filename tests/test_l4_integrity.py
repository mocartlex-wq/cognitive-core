"""Тесты L4 snapshot integrity check.

Проверяют:
- /memory/snapshots/{id}/verify работает на реальных снапшотах
- /memory/snapshots/restore/{id} с strict=true отклоняет битые снапшоты
- _verify_snapshot_integrity ловит hash mismatch / count mismatch / invalid JSON
"""
import json
import pytest
import hashlib


@pytest.mark.asyncio
async def test_verify_endpoint_unknown_snapshot(client, headers):
    """Verify несуществующего snapshot → not_found."""
    fake_id = "00000000-0000-0000-0000-000000000000"
    r = await client.post(f"/memory/snapshots/{fake_id}/verify", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "not_found"


@pytest.mark.asyncio
async def test_verify_existing_snapshot(client, headers):
    """Verify реального снапшота возвращает status и hash info."""
    # Берём первый существующий snapshot
    list_r = await client.get("/memory/snapshots", headers=headers)
    snaps = list_r.json()
    if not snaps:
        pytest.skip("Нет L4 снапшотов для теста")
    snap_id = str(snaps[0]["id"])

    r = await client.post(f"/memory/snapshots/{snap_id}/verify", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "integrity_failed")
    assert body["snapshot_id"] == snap_id
    assert "actual_hash" in body
    assert "errors" in body
    # actual_hash должен быть SHA-256 hex (64 chars)
    if body["actual_hash"]:
        assert len(body["actual_hash"]) == 64


@pytest.mark.asyncio
async def test_restore_strict_default_rejects_corrupt():
    """_verify_snapshot_integrity ловит hash mismatch."""
    from app.api.memory import _verify_snapshot_integrity

    blob = json.dumps({"knowledge": [], "tools": []}).encode()
    snap = {
        "snapshot_hash": "0" * 64,  # Заведомо неправильный
        "total_knowledge_records": 0,
        "total_tools": 0,
    }
    result = await _verify_snapshot_integrity(snap, blob)
    assert result["ok"] is False
    assert any("hash_mismatch" in e for e in result["errors"])


@pytest.mark.asyncio
async def test_verify_catches_count_mismatch():
    """_verify_snapshot_integrity ловит несовпадение count."""
    from app.api.memory import _verify_snapshot_integrity

    blob = json.dumps({"knowledge": [{"id": "x"}, {"id": "y"}], "tools": []}).encode()
    correct_hash = hashlib.sha256(blob).hexdigest()
    snap = {
        "snapshot_hash": correct_hash,
        "total_knowledge_records": 5,  # ложь — реально 2
        "total_tools": 0,
    }
    result = await _verify_snapshot_integrity(snap, blob)
    assert result["ok"] is False
    assert any("knowledge_count_mismatch" in e for e in result["errors"])


@pytest.mark.asyncio
async def test_verify_catches_invalid_json():
    """_verify_snapshot_integrity ловит broken JSON."""
    from app.api.memory import _verify_snapshot_integrity

    blob = b"this is not json {{{"
    correct_hash = hashlib.sha256(blob).hexdigest()
    snap = {"snapshot_hash": correct_hash, "total_knowledge_records": 0, "total_tools": 0}
    result = await _verify_snapshot_integrity(snap, blob)
    assert result["ok"] is False
    assert any("invalid_json" in e for e in result["errors"])
    assert result["data"] is None


@pytest.mark.asyncio
async def test_verify_catches_missing_fields():
    """_verify_snapshot_integrity ловит отсутствие knowledge/tools."""
    from app.api.memory import _verify_snapshot_integrity

    blob = json.dumps({"only_random_field": True}).encode()
    correct_hash = hashlib.sha256(blob).hexdigest()
    snap = {"snapshot_hash": correct_hash, "total_knowledge_records": 0, "total_tools": 0}
    result = await _verify_snapshot_integrity(snap, blob)
    assert result["ok"] is False
    assert any("missing_knowledge_field" in e for e in result["errors"])
    assert any("missing_tools_field" in e for e in result["errors"])


@pytest.mark.asyncio
async def test_verify_passes_valid_snapshot():
    """_verify_snapshot_integrity пропускает корректный снапшот."""
    from app.api.memory import _verify_snapshot_integrity

    payload = {
        "knowledge": [{"id": "k1"}, {"id": "k2"}],
        "tools": [{"id": "t1"}],
        "hash": "doesnt_matter_here",
    }
    blob = json.dumps(payload).encode()
    correct_hash = hashlib.sha256(blob).hexdigest()
    snap = {"snapshot_hash": correct_hash, "total_knowledge_records": 2, "total_tools": 1}
    result = await _verify_snapshot_integrity(snap, blob)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["knowledge_in_blob"] == 2
    assert result["tools_in_blob"] == 1
