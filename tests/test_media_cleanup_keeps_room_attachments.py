"""Вложения комнат и документы переживают TTL media_cleanup
(план «связь owner↔флот», Фаза 6, 2026-09-05).

media_cleanup HARD-DELETE'ил всё в domain=media_analysis старше 24ч без
разбора kind: документ или скрин, прикреплённый в комнате, исчезал вместе с
L1-строкой — переписка владельца с флотом теряла вложения за сутки. Кадры для
анализа видео по-прежнему чистятся.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import media_cleanup


def _pool(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value="DELETE 1")

    class _Acq:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Acq())
    return pool, conn


@pytest.mark.asyncio
async def test_documents_and_room_attachments_are_kept_video_frames_deleted():
    rows = [
        {"id": "11111111-1111-1111-1111-111111111111", "raw_payload": json.dumps({"media_id": "d1", "kind": "document"})},
        {"id": "22222222-2222-2222-2222-222222222222", "raw_payload": json.dumps({"media_id": "i1", "kind": "image", "room_id": "r1"})},
        {"id": "33333333-3333-3333-3333-333333333333", "raw_payload": json.dumps({"media_id": "v1", "kind": "video"})},
    ]
    pool, conn = _pool(rows)
    removed = []
    with patch.object(media_cleanup, "get_pool", AsyncMock(return_value=pool)), \
            patch.object(media_cleanup, "_cleanup_one_media",
                         AsyncMock(side_effect=lambda mid, kind: removed.append((mid, kind)) or (1, []))):
        stats = await media_cleanup.cleanup_expired_media()
    assert removed == [("v1", "video")], "чистятся только кадры анализа, не вложения комнат/документы"
    assert stats["rows_deleted"] == 1 and stats["kept"] == 2
    deleted_ids = [c.args[1] for c in conn.execute.await_args_list]
    assert deleted_ids == ["33333333-3333-3333-3333-333333333333"]


def test_restore_snapshot_writes_owner():
    """restore_snapshot вставлял L3 без owner_user_id — записи не видел ни один
    owner-scoped recall (2026-08-11: 159/269 знаний без владельца)."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "api" / "memory.py").read_text(encoding="utf-8")
    body = src.split("async def restore_snapshot", 1)[1]
    assert "caller_owner = await resolve_owner_user_id(request)" in body
    assert body.count('k.get("owner_user_id") or caller_owner') == 1
    assert body.count('t.get("owner_user_id") or caller_owner') == 1
    assert "COALESCE(l3_master_knowledge.owner_user_id" in body
    assert "COALESCE(l3_tools_registry.owner_user_id" in body
