import pytest
from uuid import UUID
from pydantic import ValidationError
from app.models.event import RawEventInput
from app.models.tools import ToolRegistryInput
from app.models.operative import OperativeQuery, OperativeClose, OperativeFeedback


class TestRawEventInput:
    def test_valid_event(self):
        e = RawEventInput(source_agent="agent1", domain="coding", payload={"x": 1})
        assert e.source_agent == "agent1"
        assert e.domain == "coding"
        assert e.payload == {"x": 1}

    def test_invalid_agent_name(self):
        with pytest.raises(ValidationError):
            RawEventInput(source_agent="a", domain="coding", payload={"x": 1})

    def test_invalid_domain(self):
        with pytest.raises(ValidationError):
            RawEventInput(source_agent="agent1", domain="BadDomain", payload={"x": 1})

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            RawEventInput(source_agent="agent1", domain="coding", payload={}, extra=1)


class TestToolRegistryInput:
    def test_valid_tool(self):
        t = ToolRegistryInput(domain="coding", tool_name="pytest", tool_type="library")
        assert t.tool_name == "pytest"
        assert t.tool_type == "library"

    def test_invalid_tool_type(self):
        with pytest.raises(ValidationError):
            ToolRegistryInput(domain="coding", tool_name="bad", tool_type="invalid_type")

    def test_all_valid_types(self):
        for tt in ("api", "script", "prompt", "library", "service"):
            t = ToolRegistryInput(domain="d", tool_name="t", tool_type=tt)
            assert t.tool_type == tt


class TestOperativeModels:
    def test_query_defaults(self):
        q = OperativeQuery(domain="coding")
        assert q.top_k == 5
        assert q.include_tools is True

    def test_query_custom(self):
        q = OperativeQuery(domain="coding", context="search text", top_k=3, include_tools=False)
        assert q.top_k == 3
        assert q.include_tools is False

    def test_close_defaults(self):
        sid = UUID("12345678-1234-1234-1234-123456789abc")
        c = OperativeClose(session_id=sid)
        assert c.keep_results is False
        assert c.source_agent == "user"

    def test_feedback(self):
        sid = UUID("12345678-1234-1234-1234-123456789abc")
        rid = UUID("87654321-4321-4321-4321-cba987654321")
        f = OperativeFeedback(session_id=sid, record_id=rid, record_type="knowledge", useful=True)
        assert f.useful is True
