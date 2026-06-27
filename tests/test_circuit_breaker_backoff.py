"""Регрессия (2026-06-14 аудит): circuit-breaker должен растягивать окно
OPEN экспоненциально + добавлять jitter, иначе при общем сбое все N
параллельных вызовов одновременно достают breaker'ы из OPEN в HALF_OPEN,
одновременно падают, одновременно вновь OPEN'ятся (thundering herd)."""
from __future__ import annotations

import time as _time

from app.services.llm_client import CircuitBreaker, CircuitState


def _force_open(b: CircuitBreaker) -> None:
    """Толкнуть breaker до OPEN, набрав threshold failures."""
    for _ in range(b.threshold):
        b.record_failure()


def test_initial_state_is_closed_and_allows():
    b = CircuitBreaker(threshold=3, timeout=2)
    assert b.state == CircuitState.CLOSED
    assert b.allow() is True


def test_opens_after_threshold_failures():
    b = CircuitBreaker(threshold=3, timeout=2)
    _force_open(b)
    assert b.state == CircuitState.OPEN
    assert b.consecutive_opens == 1
    assert b.allow() is False  # OPEN — fail-fast


def test_backoff_grows_with_consecutive_opens():
    """Каждое последующее открытие удваивает окно (с поправкой на jitter)."""
    b = CircuitBreaker(threshold=2, timeout=4)

    _force_open(b)
    first_timeout = b.current_timeout
    # Jitter ±25%, базовое окно = 4 * 2^0 = 4 → ожидаем 3..5
    assert 3.0 <= first_timeout <= 5.0

    # Прокручиваем время чтобы попасть в HALF_OPEN, затем снова падаем
    b.opened_at = _time.monotonic() - (first_timeout + 1)
    assert b.allow() is True  # переход в HALF_OPEN
    assert b.state == CircuitState.HALF_OPEN
    b.record_failure()  # HALF_OPEN → OPEN, 2-е консекутивное открытие
    second_timeout = b.current_timeout
    # base * 2^1 = 8 → jitter 6..10
    assert 6.0 <= second_timeout <= 10.0
    assert second_timeout > first_timeout
    assert b.consecutive_opens == 2


def test_backoff_capped_at_max():
    b = CircuitBreaker(threshold=2, timeout=4)
    max_t = b._max_timeout()  # 4 * 16 = 64
    # 6 последовательных открытий: 2^6 = 64x base = 256 > 64 cap
    for _ in range(6):
        b.consecutive_opens += 1
        b._apply_backoff()
    assert b.current_timeout <= max_t


def test_success_resets_backoff_and_closes():
    b = CircuitBreaker(threshold=2, timeout=4)
    _force_open(b)
    assert b.current_timeout > b.base_timeout * 0.5

    # Симулируем HALF_OPEN → success
    b.opened_at = _time.monotonic() - (b.current_timeout + 1)
    b.allow()  # → HALF_OPEN
    b.record_success()
    assert b.state == CircuitState.CLOSED
    assert b.failures == 0
    assert b.consecutive_opens == 0
    assert b.current_timeout == float(b.base_timeout)


def test_jitter_breaks_thundering_herd():
    """Два конкурентных breaker'а после одинакового сбоя должны разойтись
    по фактическому окну — иначе они снова падают одновременно."""
    a = CircuitBreaker(threshold=2, timeout=10)
    b = CircuitBreaker(threshold=2, timeout=10)
    _force_open(a)
    _force_open(b)
    # Из-за random jitter ±25% окна почти наверняка разные.
    # Гарантировать через 1 итерацию нельзя; проверим что система СПОСОБНА
    # разойтись — после 20 одновременных перезапусков хотя бы 3 разных значения.
    seen = {a.current_timeout, b.current_timeout}
    for _ in range(20):
        x = CircuitBreaker(threshold=2, timeout=10)
        _force_open(x)
        seen.add(round(x.current_timeout, 2))
    assert len(seen) >= 3, f"jitter не работает: 20 breaker'ов дали {len(seen)} разных значений"


def test_half_open_failure_increments_consecutive_opens():
    """Падение в HALF_OPEN тоже считается «ещё одно открытие»."""
    b = CircuitBreaker(threshold=2, timeout=4)
    _force_open(b)
    assert b.consecutive_opens == 1
    # Принудительно в HALF_OPEN
    b.opened_at = _time.monotonic() - (b.current_timeout + 1)
    b.allow()
    assert b.state == CircuitState.HALF_OPEN
    b.record_failure()
    assert b.state == CircuitState.OPEN
    assert b.consecutive_opens == 2
