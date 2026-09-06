"""Метрика l3_coverage_pct в /health.deep: доля доменов с живым знанием L3.

Считаем без БД: вычисление вынесено в чистую функцию _coverage_pct, а SQL-зонд
проверяем по форме — что он берёт домены из L1, спрашивает ЖИВОЕ знание
(effective_to IS NULL) и не роняет /health при ошибке.
"""
import inspect

from app.main import _coverage_pct


class TestCoveragePct:
    def test_none_when_no_domains(self):
        # Доменов нет — процент неопределён, а не 0: 0% читается как «всё плохо».
        assert _coverage_pct(0, 0) is None

    def test_full_coverage(self):
        assert _coverage_pct(7, 7) == 100.0

    def test_zero_coverage(self):
        assert _coverage_pct(0, 12) == 0.0

    def test_partial_rounded_to_one_decimal(self):
        # 5/18 = 27.777… → 27.8
        assert _coverage_pct(5, 18) == 27.8

    def test_returns_float(self):
        assert isinstance(_coverage_pct(1, 3), float)

    def test_third_is_rounded_not_truncated(self):
        # 2/3 = 66.66… → 66.7, а не 66.6
        assert _coverage_pct(2, 3) == 66.7


class TestProbeShape:
    """Зонд обязан считать по ЖИВОМУ L3 и по доменам L1, иначе метрика врёт."""

    def _health_source(self) -> str:
        from app.main import health
        return inspect.getsource(health)

    def test_probe_counts_only_active_knowledge(self):
        src = self._health_source()
        assert "l3_master_knowledge" in src
        assert "effective_to IS NULL" in src

    def test_denominator_is_l1_domains(self):
        src = self._health_source()
        assert "SELECT DISTINCT domain FROM l1_raw_events" in src

    def test_exposes_three_keys(self):
        src = self._health_source()
        for key in ("l3_domains_total", "l3_domains_covered", "l3_coverage_pct"):
            assert f'deep["{key}"]' in src, key

    def test_probe_failure_does_not_break_health(self):
        # Зонд обёрнут в try/except: свежая база без таблиц не должна ронять /health.
        src = self._health_source()
        i = src.index("l3_coverage_pct")
        assert "try:" in src[:i]
        assert "l3 coverage probe failed" in src
