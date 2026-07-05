import os
from uuid import uuid4

import pytest


class TestHealth:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        data = r.json()
        # postgres + redis — обязательные сервисы (есть в любом окружении).
        assert data["services"]["postgres"] == "ok"
        assert data["services"]["redis"] == "ok"
        # MinIO/S3 теперь optional: init сделан non-fatal (app/main.py), и в
        # окружениях без объектного хранилища (напр. CI db-tests) /health честно
        # репортит minio как down → data["healthy"] становится False. Проверяем
        # только что ключ присутствует, не требуя "ok".
        assert "minio" in data["services"]
        assert "healthy" in data

    async def test_health_details(self, client):
        """New health fields: version, uptime, db_size, llm stats, system info."""
        r = await client.get("/health")
        data = r.json()
        assert "version" in data
        assert data["version"] == "0.6.0"
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] > 0
        assert "db_size_mb" in data
        assert "llm" in data
        assert "system" in data
        assert "python" in data["system"]

    async def test_metrics(self, client):
        r = await client.get("/metrics")
        assert r.status_code == 200
        assert "cognitive_http_requests_total" in r.text

    async def test_ab_stats(self, client):
        r = await client.get("/ab-stats")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    async def test_sandbox(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert "html" in r.headers.get("content-type", "").lower() or "text/html" in r.headers.get("content-type", "")


class TestAuth:
    async def test_no_key(self, client):
        r = await client.get("/memory/snapshots")
        assert r.status_code == 401

    async def test_invalid_key(self, client):
        r = await client.get("/memory/snapshots", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401


class TestEventIngest:
    async def test_ingest(self, client, headers):
        r = await client.post("/events", json={
            "source_agent": "test_runner",
            "domain": "test_api",
            "payload": {"action": "unit_test", "passed": True},
        }, headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "accepted"
        assert "id" in data

    async def test_ingest_validation(self, client, headers):
        r = await client.post("/events", json={
            "source_agent": "a",
            "domain": "test",
            "payload": {},
        }, headers=headers)
        assert r.status_code == 422

    async def test_bulk_ingest(self, client, headers):
        for i in range(4):
            r = await client.post("/events", json={
                "source_agent": "test_runner",
                "domain": "test_bulk",
                "payload": {"action": f"test_{i}", "value": i},
            }, headers=headers)
            assert r.status_code == 200


def _deepseek_key_present() -> bool:
    """Collection-safe predicate: есть ли непустой DeepSeek API-ключ?

    daily-консолидация для непустого domain вызывает реальный LLM
    (`analyze_daily_events` -> `llm_client._try_call`). На CI db-tests
    DEEPSEEK_API_KEY пуст -> LLM отдаёт openai.AuthenticationError, эндпоинт
    это не маппит и возвращает голый 500 -> `assert 500 == 200`. Это env-gap
    раннера (нет рабочего LLM-ключа), не баг логики тестов.

    ПОЧЕМУ skipif на os.getenv, а НЕ probe-фикстура (как было раньше):
    skipif вычисляется на этапе collection, когда pytest-фикстуры ещё НЕ
    существуют. Поэтому условие обязано быть само-достаточным: обычная функция,
    не зависящая от фикстур и не способная упасть. Прошлый probe делал реальный
    POST и скипал по сигнатуре LLM-auth в теле 500 — но эндпоинт отдаёт ROOT
    500 без LLM-строки в body, маркеры не совпали, skip не сработал -> тест
    падал. os.getenv даёт детерминированное, честное условие без сетевого
    вызова.
    """
    return bool((os.getenv("DEEPSEEK_API_KEY") or "").strip())


class TestConsolidation:
    @pytest.mark.skipif(
        not _deepseek_key_present(),
        reason="daily-консолидация вызывает реальный LLM; DEEPSEEK_API_KEY пуст/не задан (CI db-tests)",
    )
    async def test_daily(self, client, headers):
        r = await client.post("/memory/consolidate/daily", json={"domain": "test_api"}, headers=headers)
        assert r.status_code == 200
        data = r.json()
        # status="lock_held" — валидный ответ если параллельно идёт другой daily на тот же domain
        # (advisory lock защищает от двойной консолидации)
        assert data["status"] in ("ok", "lock_held", "no_events")
        if data["status"] == "ok":
            assert len(data["results"]) >= 1

    async def test_weekly(self, client, headers):
        r = await client.post("/memory/consolidate/weekly?domain=test_api", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("consolidated", "skipped", "no_buffers")

    async def test_monthly(self, client, headers):
        r = await client.post("/memory/audit/monthly?domain=test_api", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "health_score" in data


class TestOperative:
    @pytest.fixture
    async def session_id(self, client, headers):
        r = await client.post("/operative/query", json={
            "context": "unit testing", "domain": "test_api", "top_k": 2,
        }, headers=headers)
        data = r.json()
        return data["session_id"]

    async def test_query(self, client, headers):
        r = await client.post("/operative/query", json={
            "context": "test search", "domain": "test_api", "top_k": 2,
        }, headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "session_id" in data
        assert "results" in data
        assert isinstance(data["results"], list)

    async def test_close_session(self, client, headers, session_id):
        r = await client.post(f"/operative/sessions/{session_id}/close", json={
            "session_id": session_id,
            "keep_results": True,
            "results_summary": {"passed": True},
        }, headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "closed"

    async def test_feedback(self, client, headers):
        sid = str(uuid4())
        rid = str(uuid4())
        r = await client.post(f"/operative/sessions/{sid}/feedback", json={
            "session_id": sid, "record_id": rid,
            "record_type": "knowledge", "useful": True,
        }, headers=headers)
        assert r.status_code == 200


class TestSnapshots:
    async def test_list(self, client, headers):
        r = await client.get("/memory/snapshots", headers=headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    async def test_restore_not_found(self, client, headers):
        sid = str(uuid4())
        r = await client.post(f"/memory/snapshots/restore/{sid}", headers=headers)
        assert r.status_code in (200, 404)


class TestCleanup:
    async def test_cleanup(self, client, headers):
        r = await client.post("/memory/cleanup", json={}, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "cleaned"
        assert "deleted_stale_unprocessed" in body

    async def test_cleanup_keeps_unprocessed(self, client, headers):
        """Retention не удаляет события, не прошедшие в L2 (processed_to_l2=false).

        Регресс июня 2026: при активности < MIN_EVENTS_FOR_DAILY куратор скипал
        консолидацию, а prune удалял неанализированный опыт по общему TTL.
        """
        r = await client.post("/events", json={
            "source_agent": "test_runner",
            "domain": "test_api",
            "payload": {"note": "unprocessed event must survive cleanup"},
        }, headers=headers)
        assert r.status_code == 200
        event_id = r.json()["id"]

        r = await client.post("/memory/cleanup", json={}, headers=headers)
        assert r.status_code == 200

        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM l1_raw_events WHERE id = $1::uuid", event_id
            )
        assert row is not None, "cleanup удалил необработанное событие"


class TestEdgeCases:
    """Граничные случаи и ошибки."""

    async def test_consolidation_empty_domain(self, client, headers):
        """Weekly consolidation with domain that has no data."""
        r = await client.post("/memory/consolidate/weekly?domain=empty_nonexistent", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] in ("no_buffers", "skipped", "consolidated")

    async def test_operative_empty_search(self, client, headers):
        """OP query with no matching results."""
        r = await client.post("/operative/query", json={
            "context": "xyznonexistent12345", "domain": "test_api", "top_k": 1,
        }, headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "session_id" in data
        assert "results" in data

    async def test_close_nonexistent_session(self, client, headers):
        """Close a session that doesn't exist."""
        sid = str(uuid4())
        r = await client.post(f"/operative/sessions/{sid}/close", json={
            "session_id": sid, "keep_results": False,
        }, headers=headers)
        data = r.json()
        assert data["status"] in ("not_found", "closed")

    async def test_feedback_nonexistent_session(self, client, headers):
        """Feedback on nonexistent session returns session_not_found."""
        sid = str(uuid4())
        r = await client.post(f"/operative/sessions/{sid}/feedback", json={
            "session_id": sid, "record_id": str(uuid4()),
            "record_type": "knowledge", "useful": True,
        }, headers=headers)
        data = r.json()
        assert data["status"] == "session_not_found"

    async def test_invalid_uuid_snapshot(self, client, headers):
        """Restore snapshot with invalid UUID."""
        r = await client.post("/memory/snapshots/restore/not-a-uuid", headers=headers)
        assert r.status_code in (200, 422, 404)

    async def test_xss_payload_rejected(self, client, headers):
        """XSS in payload is sanitized."""
        r = await client.post("/events", json={
            "source_agent": "test_runner",
            "domain": "test_api",
            "payload": {"data": "<script>alert('xss')</script>"},
        }, headers=headers)
        # Should either be accepted (sanitized) or rejected
        assert r.status_code in (200, 422)

    async def test_deep_nesting_rejected(self, client, headers):
        """Payload with excessive nesting depth is rejected."""
        deep = {}
        current = deep
        for _ in range(15):
            current["nested"] = {}
            current = current["nested"]
        r = await client.post("/events", json={
            "source_agent": "test_runner",
            "domain": "test_api",
            "payload": deep,
        }, headers=headers)
        assert r.status_code == 422


class TestTools:
    """Инструменты L3 реестра."""

    async def test_register_and_list(self, client, headers):
        r = await client.post("/tools", json={
            "domain": "test_api",
            "tool_name": "pytest",
            "tool_type": "script",
            "description": "Test framework",
        }, headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "registered"

    async def test_list_tools(self, client, headers):
        r = await client.get("/tools?domain=test_api", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "tools" in data
        assert isinstance(data["tools"], list)

    async def test_invalid_tool_type(self, client, headers):
        r = await client.post("/tools", json={
            "domain": "test_api",
            "tool_name": "bad_tool",
            "tool_type": "invalid_type",
        }, headers=headers)
        assert r.status_code == 422

    async def test_deprecate_tool_not_found(self, client, headers):
        r = await client.delete(f"/tools/{uuid4()}", headers=headers)
        assert r.status_code in (200, 404)
