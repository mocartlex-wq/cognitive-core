"""Unit tests для CircuitBreaker в app/services/llm_client.py.

Pure state-machine — no IO, no fixtures beyond pytest.
Manipulates _time.monotonic via monkeypatch для simulating timeouts.
"""
from unittest.mock import patch

from app.services.llm_client import (
    CircuitBreaker,
    CircuitState,
    _get_breaker,
    get_circuit_states,
    reset_circuit_breakers,
)


class TestCircuitBreakerBasic:
    def test_initial_state_closed_allows(self):
        cb = CircuitBreaker(threshold=3, timeout=60)
        assert cb.state == CircuitState.CLOSED
        assert cb.allow() is True
        assert cb.failures == 0

    def test_success_resets_failures(self):
        cb = CircuitBreaker(threshold=3, timeout=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.failures == 2
        cb.record_success()
        assert cb.failures == 0
        assert cb.state == CircuitState.CLOSED

    def test_threshold_failures_open_circuit(self):
        cb = CircuitBreaker(threshold=3, timeout=60)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.failures == 3
        assert cb.allow() is False, "OPEN must reject"

    def test_failures_below_threshold_stays_closed(self):
        cb = CircuitBreaker(threshold=3, timeout=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow() is True


class TestCircuitBreakerTimeout:
    def test_open_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(threshold=2, timeout=10)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Advance time past timeout
        with patch("app.services.llm_client._time.monotonic", return_value=cb.opened_at + 11):
            allowed = cb.allow()
            assert allowed is True
            assert cb.state == CircuitState.HALF_OPEN

    def test_open_within_timeout_stays_open(self):
        cb = CircuitBreaker(threshold=2, timeout=60)
        cb.record_failure()
        cb.record_failure()
        opened_at = cb.opened_at

        # Within timeout window
        with patch("app.services.llm_client._time.monotonic", return_value=opened_at + 5):
            assert cb.allow() is False
            assert cb.state == CircuitState.OPEN

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(threshold=2, timeout=10)
        cb.record_failure()
        cb.record_failure()
        cb.state = CircuitState.HALF_OPEN  # simulate post-timeout

        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failures == 0

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(threshold=2, timeout=10)
        cb.record_failure()
        cb.record_failure()
        cb.state = CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN, "failure в HALF_OPEN должен немедленно re-open"


class TestRegistry:
    def setup_method(self):
        reset_circuit_breakers()

    def teardown_method(self):
        reset_circuit_breakers()

    def test_get_breaker_creates_lazily(self):
        b1 = _get_breaker("test_endpoint_1")
        assert isinstance(b1, CircuitBreaker)
        b2 = _get_breaker("test_endpoint_1")
        assert b1 is b2, "same key should return same instance"

    def test_get_breaker_different_keys_different_instances(self):
        b1 = _get_breaker("ep_a")
        b2 = _get_breaker("ep_b")
        assert b1 is not b2

    def test_get_states_returns_snapshot(self):
        cb = _get_breaker("test_states_ep")
        cb.record_failure()

        snap = get_circuit_states()
        assert "test_states_ep" in snap
        assert snap["test_states_ep"]["state"] == CircuitState.CLOSED
        assert snap["test_states_ep"]["failures"] == 1
        assert snap["test_states_ep"]["opened_seconds_ago"] is None  # not opened yet

    def test_get_states_includes_opened_seconds(self):
        cb = _get_breaker("test_opened_ep")
        cb.threshold = 1  # easier trigger
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        snap = get_circuit_states()
        assert snap["test_opened_ep"]["opened_seconds_ago"] is not None
        assert snap["test_opened_ep"]["opened_seconds_ago"] >= 0

    def test_reset_clears_registry(self):
        _get_breaker("k1")
        _get_breaker("k2")
        assert len(get_circuit_states()) == 2
        reset_circuit_breakers()
        assert len(get_circuit_states()) == 0
