import pytest

from app.security.sanitizer import (
    _clean_string,
    _count_keys,
    _get_depth,
    sanitize_payload,
)


class TestDepth:
    def test_flat_dict(self):
        assert _get_depth({"a": 1}) == 1

    def test_nested_dict(self):
        assert _get_depth({"a": {"b": 1}}) == 2

    def test_deep_nested(self):
        assert _get_depth({"a": {"b": {"c": {"d": 1}}}}) == 4

    def test_empty_dict(self):
        assert _get_depth({}) == 0

    def test_list_with_nested(self):
        assert _get_depth({"items": [1, {"a": 1}]}) == 2


class TestCountKeys:
    def test_flat_dict(self):
        assert _count_keys({"a": 1, "b": 2}) == 2

    def test_nested_keys(self):
        assert _count_keys({"a": {"b": 1, "c": 2}, "d": 3}) == 4

    def test_empty(self):
        assert _count_keys({}) == 0

    def test_list_of_dicts(self):
        assert _count_keys({"items": [{"x": 1}, {"y": 2}]}) == 3


class TestCleanString:
    def test_sql_passthrough(self):
        # SQL filtering intentionally removed 2026-05-26: все queries параметризованы
        # (asyncpg $1/$2), поэтому SQL-подобный текст — валидные ДАННЫЕ, не атака.
        # Прошлый filter ломал em-dash, "pytest -- -k", lessons про SQL. См. sanitizer.py.
        w = []
        result = _clean_string("DROP TABLE users", w, "test.field")
        assert result == "DROP TABLE users"

    def test_sql_select_passthrough(self):
        w = []
        result = _clean_string("SELECT * FROM users", w, "query")
        assert result == "SELECT * FROM users"

    def test_js_eval(self):
        with pytest.raises(ValueError, match="JavaScript"):
            _clean_string("eval('alert(1)')", [], "script")

    def test_html_escape(self):
        w = []
        result = _clean_string("<script>alert('xss')</script>", w, "html")
        assert "<" not in result or "&lt;" in result
        assert len(w) > 0

    def test_shell_warning(self):
        w = []
        result = _clean_string("rm -rf /tmp/test", w, "cmd")
        assert len(w) > 0
        assert "Shell command escaped" in w[0]

    def test_clean_string(self):
        w = []
        result = _clean_string("hello world 123", w, "text")
        assert result == "hello world 123"
        assert len(w) == 0


class TestSanitizePayload:
    def test_clean_payload(self):
        r = sanitize_payload({"user": "alice", "score": 42})
        assert r.payload == {"user": "alice", "score": 42}
        assert r.warnings == []

    def test_sql_passthrough_payload(self):
        # SQL no longer filtered (parameterized queries) — passes through as data.
        r = sanitize_payload({"query": "DROP TABLE users"})
        assert r.payload["query"] == "DROP TABLE users"

    def test_js_rejected(self):
        with pytest.raises(ValueError, match="JavaScript"):
            sanitize_payload({"code": "eval('bad')"})

    def test_xss_escaped(self):
        r = sanitize_payload({"html": "<script>alert(1)</script>"})
        assert "script" not in str(r.payload["html"]).lower() or "&lt;" in str(r.payload["html"])

    def test_depth_limit(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": {"k": 1}}}}}}}}}}}
        with pytest.raises(ValueError, match="depth"):
            sanitize_payload(deep)

    def test_size_limit(self):
        big = {"data": "x" * 300_000}
        with pytest.raises(ValueError, match="size"):
            sanitize_payload(big)

    def test_key_limit(self):
        many = {f"key_{i}": i for i in range(600)}
        with pytest.raises(ValueError, match="keys"):
            sanitize_payload(many)
