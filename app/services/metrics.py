"""Prometheus метрики + структурированное логирование."""

import json
import time
import uuid
from datetime import datetime, timezone
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CollectorRegistry, REGISTRY

registry = REGISTRY

# HTTP метрики
http_requests = Counter(
    "cognitive_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

http_latency = Histogram(
    "cognitive_http_latency_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# LLM метрики
llm_calls = Counter(
    "cognitive_llm_calls_total",
    "Total LLM calls",
    ["function", "model", "status"],
)

llm_latency = Histogram(
    "cognitive_llm_latency_seconds",
    "LLM call latency",
    ["function", "model"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# Слои памяти
layer_size = Gauge(
    "cognitive_layer_records",
    "Number of records per layer",
    ["layer", "domain"],
)

# Rate limit
rate_limit_hits = Counter(
    "cognitive_rate_limit_hits_total",
    "Rate limit hits",
    ["agent_id"],
)

# Аудит
audit_events = Counter(
    "cognitive_audit_events_total",
    "Audit events",
    ["action", "success"],
)


def track_http(method: str, endpoint: str, status: int, duration: float):
    http_requests.labels(method=method, endpoint=endpoint, status=str(status)).inc()
    http_latency.labels(method=method, endpoint=endpoint).observe(duration)


def track_llm(function: str, model: str, status: str, duration: float):
    llm_calls.labels(function=function, model=model, status=status).inc()
    llm_latency.labels(function=function, model=model).observe(duration)


def update_layer_size(layer: str, domain: str, count: int):
    layer_size.labels(layer=layer, domain=domain).set(count)


def track_rate_limit(agent_id: str):
    rate_limit_hits.labels(agent_id=agent_id).inc()


def track_audit(action: str, success: bool):
    audit_events.labels(action=action, success=str(success).lower()).inc()


def get_metrics() -> bytes:
    return generate_latest(registry)


def get_llm_stats() -> dict:
    """Возвращает статистику LLM-вызовов для health-эндпоинта."""
    stats = {}
    for metric in registry.collect():
        if metric.name == "cognitive_llm_calls_total" and metric.samples:
            for sample in metric.samples:
                labels = sample.labels
                key = f"{labels.get('function', '?')}:{labels.get('model', '?')}:{labels.get('status', '?')}"
                stats[f"calls_{key}"] = int(sample.value)
        elif metric.name == "cognitive_http_requests_total" and metric.samples:
            total = 0
            for sample in metric.samples:
                total += int(sample.value)
            stats["total_http_requests"] = total
    return stats


# ---- Structured JSON Logging ----

def log_event(level: str, message: str, **kwargs) -> str:
    """Формирует структурированный JSON-лог с trace_id."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "msg": message,
        "trace_id": kwargs.pop("trace_id", str(uuid.uuid4())[:8]),
        **kwargs,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    print(line, flush=True)
    return record["trace_id"]
