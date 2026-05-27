"""SLO tracking endpoint — для SRE monitoring + alerting.

Возвращает compliance per SLO + violations за rolling window.
Только admin может смотреть.

Standard SLOs для Cognitive Core (v1.0 roadmap M2):
  - Availability: 99.5% uptime per 28d rolling window
  - Latency p95 memory endpoints: < 300ms (cognitive_recall/remember)
  - Error rate: < 1% of requests return 5xx
  - Postgres query p95: < 100ms

Destination в репо: app/api/admin_slo.py
Регистрация: app/main.py — `app.include_router(admin_slo.router)`
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from app.security.middleware import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/slo", tags=["admin-slo"])


# Static SLO targets — единый источник правды.
# При изменении синхронизировать с alert rules в Prometheus.
SLO_TARGETS: dict[str, dict[str, Any]] = {
    "availability": {
        "target": 0.995,
        "window_days": 28,
        "description": "Uptime / total time за 28-day rolling window.",
    },
    "latency_p95_memory": {
        "target_ms": 300,
        "endpoints": ["cognitive_recall", "cognitive_remember"],
        "description": "p95 latency для memory endpoints.",
    },
    "error_rate": {
        "target": 0.01,
        "description": "Доля 5xx responses от общего числа запросов.",
    },
    "postgres_p95": {
        "target_ms": 100,
        "description": "p95 длительности Postgres queries.",
    },
}


def _compute_availability_budget() -> dict[str, Any]:
    """Error budget computation для availability SLO.

    Формула: budget_min = window_min * (1 - target) — сколько минут downtime
    разрешено за окно. consumed_min — сколько уже потрачено.

    # TODO real Prometheus query when M2 PR Grafana lands
    """
    target = SLO_TARGETS["availability"]["target"]
    window_days = SLO_TARGETS["availability"]["window_days"]
    window_min = window_days * 24 * 60
    allowed_min = round(window_min * (1 - target), 1)
    # Dummy данные — real values придут из Prometheus (uptime / total)
    consumed_min = 12.0
    remaining_min = max(0.0, allowed_min - consumed_min)
    percent_consumed = round((consumed_min / allowed_min) * 100, 1) if allowed_min else 0.0
    return {
        "target": target,
        "allowed_downtime_min_per_28d": allowed_min,
        "consumed_min": consumed_min,
        "remaining_min": remaining_min,
        "percent_consumed": percent_consumed,
    }


def _compute_latency_budget() -> dict[str, Any]:
    """Latency budget — сколько раз p95 превысил target за окно.

    Допускаем небольшое число violations (transient spikes), но не больше
    `violations_allowed`. По умолчанию — 10 за 28d.

    # TODO real Prometheus query when M2 PR Grafana lands
    """
    return {
        "target_p95_ms": SLO_TARGETS["latency_p95_memory"]["target_ms"],
        "violations_count": 3,
        "violations_allowed_per_28d": 10,
        "remaining_violations": 7,
    }


@router.get("/")
async def get_slo_status(request: Request) -> dict[str, Any]:
    """Compliance per SLO indicator + violations за последние 24h.

    Все значения сейчас dummy — wiring к Prometheus в M2 PR (Grafana stack).
    Возвращаемый shape финальный, чтобы UI/dashboards могли разрабатываться параллельно.
    """
    await require_admin(request)
    # TODO real Prometheus query when M2 PR Grafana lands —
    # должен дёрнуть PromQL `avg_over_time(up{job="cognitive_api"}[28d])` и т.п.
    return {
        "window": "28d_rolling",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "indicators": {
            "availability": {
                "target": 0.995,
                "actual": 0.998,
                "compliant": True,
                "budget_remaining_min": 189,
            },
            "latency_p95_memory": {
                "target_ms": 300,
                "actual_ms": 245,
                "compliant": True,
            },
            "error_rate": {
                "target": 0.01,
                "actual": 0.003,
                "compliant": True,
            },
            "postgres_p95": {
                "target_ms": 100,
                "actual_ms": 85,
                "compliant": True,
            },
        },
        "violations_24h": [],
        "alerts": {
            "fired": 0,
            "muted": 0,
        },
    }


@router.get("/budget")
async def get_error_budget(request: Request) -> dict[str, Any]:
    """Error budget для текущего SLO окна.

    Используется SRE / on-call чтобы решить: можно ли деплоить рискованные
    изменения (budget consumed > 80% → freeze) или есть запас.
    """
    await require_admin(request)
    return {
        "window": "28d_rolling",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "availability_budget": _compute_availability_budget(),
        "latency_budget": _compute_latency_budget(),
    }


@router.get("/targets")
async def get_slo_targets(request: Request) -> dict[str, Any]:
    """Возвращает определения всех SLO — для UI настройки + docs.

    Открытый для admin (не для public) — описание целевых значений + window.
    """
    await require_admin(request)
    return {"targets": SLO_TARGETS}
