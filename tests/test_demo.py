"""Тесты streaming /demo/run эндпоинта."""
import json

import pytest


@pytest.mark.asyncio
async def test_demo_run_streams_ndjson(client):
    """POST /demo/run возвращает поток NDJSON с типизированными событиями."""
    async with client.stream("POST", "/demo/run", timeout=240.0) as r:
        assert r.status_code == 200
        assert "application/x-ndjson" in r.headers.get("content-type", "")

        events = []
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pytest.fail(f"Invalid NDJSON line: {line[:200]}")
            # Не ждём всё — достаточно пары событий чтобы убедиться в формате
            if len(events) >= 2:
                break

        assert len(events) >= 2
        for e in events:
            assert "type" in e
            assert e["type"] in ("step_start", "step_done", "step_error", "final")


@pytest.mark.asyncio
async def test_demo_step_start_has_message(client):
    """Все step_start имеют поле message."""
    async with client.stream("POST", "/demo/run", timeout=240.0) as r:
        seen_start = False
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event["type"] == "step_start":
                assert "message" in event
                assert isinstance(event["message"], str) and event["message"]
                seen_start = True
                break
        assert seen_start
