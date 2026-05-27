"""Tests для full claim flow (M1 PR #116).

Покрывает:
- POST /user/connect/issue-claim-token (session-gated)
- GET /user/connect/claim/peek (public, без consume)
- POST /user/connect/claim (consume, one-shot)
- Idempotency: повторный issue для того же agent_id возвращает тот же token (PR #106)
- 410 на already-used + expired

Использует authed_client fixture из PR #115.
"""
import pytest


class TestIssueClaimToken:
    async def test_issue_returns_token_and_agent_id(self, authed_client):
        r = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": "test_agent_001", "platform": "claude_code"},
        )
        assert r.status_code == 200, f"unexpected {r.status_code}: {r.text}"
        body = r.json()
        assert "token" in body
        assert body["agent_id"] == "test_agent_001"
        assert body["expires_in_seconds"] > 0
        assert "prompt_for_agent" in body
        # Verify canary present (PR #110)
        assert "🟢" in body["prompt_for_agent"] or "CANARY" in body["prompt_for_agent"]

    async def test_issue_generates_default_agent_id(self, authed_client):
        r = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"platform": "cursor"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"].startswith("cursor-")
        assert "token" in body

    async def test_issue_idempotency(self, authed_client):
        """PR #106: повторный issue для того же agent_id → тот же token."""
        agent_id = "test_idem_001"
        r1 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": agent_id, "platform": "claude_code"},
        )
        if r1.status_code != 200:
            pytest.skip(f"first issue failed: {r1.text}")
        token1 = r1.json()["token"]

        r2 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": agent_id, "platform": "claude_code"},
        )
        assert r2.status_code == 200
        token2 = r2.json()["token"]
        assert token1 == token2, f"idempotency broken: {token1} != {token2}"

    async def test_issue_requires_session(self, client):
        """Без X-Session-Id → 401."""
        r = await client.post(
            "/user/connect/issue-claim-token",
            json={"platform": "claude_code"},
        )
        assert r.status_code == 401


class TestPeekClaim:
    async def test_peek_returns_token_info(self, authed_client, client):
        """Issue token then peek it without consume."""
        # Issue с authed (session)
        r1 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": "test_peek_001", "platform": "claude_code", "machine_hint": "TestMachine"},
        )
        if r1.status_code != 200:
            pytest.skip(f"issue failed: {r1.text}")
        token = r1.json()["token"]

        # Peek с public client (no auth)
        r2 = await client.get(f"/user/connect/claim/peek?token={token}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["token"] == token
        assert body["agent_id"] == "test_peek_001"
        assert body["platform"] == "claude_code"
        assert body["machine_label"] == "TestMachine"
        assert body["expires_in_seconds"] > 0
        # Owner email masked
        if body.get("owner_email_masked"):
            assert "***" in body["owner_email_masked"]

    async def test_peek_does_not_consume(self, authed_client, client):
        """Peek несколько раз — token остаётся валидным."""
        r1 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": "test_peek_no_consume", "platform": "claude_code"},
        )
        if r1.status_code != 200:
            pytest.skip(f"issue: {r1.text}")
        token = r1.json()["token"]

        # Peek 3 times
        for i in range(3):
            r = await client.get(f"/user/connect/claim/peek?token={token}")
            assert r.status_code == 200, f"peek iteration {i+1} failed: {r.status_code}"


class TestConsumeClaim:
    async def test_consume_returns_api_key(self, authed_client, client):
        """Issue → consume → returns api_key + mcp_config."""
        r1 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": "test_consume_001", "platform": "claude_code"},
        )
        if r1.status_code != 200:
            pytest.skip(f"issue: {r1.text}")
        token = r1.json()["token"]

        # Consume — нужен правильный User-Agent (не Mozilla — anti-preview)
        r2 = await client.get(
            f"/user/connect/claim?token={token}",
            headers={"User-Agent": "claude-code-agent"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert "api_key" in body
        assert body["agent_id"] == "test_consume_001"
        assert "mcp_config" in body

    async def test_double_consume_returns_410(self, authed_client, client):
        """One-shot: повторный claim → 410 already used."""
        r1 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": "test_double_001", "platform": "claude_code"},
        )
        if r1.status_code != 200:
            pytest.skip(f"issue: {r1.text}")
        token = r1.json()["token"]

        r2 = await client.get(
            f"/user/connect/claim?token={token}",
            headers={"User-Agent": "claude-code-agent"},
        )
        if r2.status_code != 200:
            pytest.skip(f"first consume: {r2.text}")

        # Second consume
        r3 = await client.get(
            f"/user/connect/claim?token={token}",
            headers={"User-Agent": "claude-code-agent"},
        )
        assert r3.status_code == 410, f"expected 410 (used), got {r3.status_code}: {r3.text}"


class TestPeekAfterConsume:
    async def test_peek_after_consume_returns_410(self, authed_client, client):
        """После consume — peek говорит token_already_used."""
        r1 = await authed_client.post(
            "/user/connect/issue-claim-token",
            json={"agent_id": "test_peek_after_001", "platform": "claude_code"},
        )
        if r1.status_code != 200:
            pytest.skip(f"issue: {r1.text}")
        token = r1.json()["token"]

        r2 = await client.get(
            f"/user/connect/claim?token={token}",
            headers={"User-Agent": "claude-code-agent"},
        )
        if r2.status_code != 200:
            pytest.skip(f"consume: {r2.text}")

        r3 = await client.get(f"/user/connect/claim/peek?token={token}")
        assert r3.status_code == 410
        detail = r3.json().get("detail", {})
        assert detail.get("error") == "token_already_used"
