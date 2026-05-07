import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.db.postgres import init_db, close_db
from app.db.redis import init_redis, close_redis
from app.db.s3 import init_s3
from app.services.metrics import track_http, log_event

__version__ = "0.5.0"

_start_time: datetime | None = None
_scheduler_task: asyncio.Task | None = None
_outbox_publisher = None  # OutboxPublisher instance


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация всех подключений при старте."""
    global _scheduler_task, _start_time, _outbox_publisher
    _start_time = datetime.now(timezone.utc)
    log_event("info", "Starting Cognitive Core", version=__version__)
    await init_db()
    await init_redis()
    init_s3()

    from app.worker import scheduler_loop
    _scheduler_task = asyncio.create_task(scheduler_loop())

    # Outbox Publisher для NATS-replication. Если nats-py не установлен или
    # NATS недоступен — publisher тихо retry'ит, основная система не страдает.
    try:
        from app.replication import OutboxPublisher
        from app.db.postgres import get_pool
        pool = await get_pool()
        if pool is not None:
            _outbox_publisher = OutboxPublisher(pool)
            await _outbox_publisher.start()
            log_event("info", "OutboxPublisher started")
    except Exception as e:
        log_event("warn", "OutboxPublisher disabled", error=str(e))

    log_event("info", "Cognitive Core ready")
    yield

    if _outbox_publisher:
        await _outbox_publisher.stop()
    if _scheduler_task:
        _scheduler_task.cancel()
    await close_db()
    await close_redis()
    log_event("info", "Cognitive Core shutdown")


app = FastAPI(
    title="Cognitive Core",
    description="5-слойная система памяти с AI-куратором",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # с "*" credentials всё равно блокируются браузером
    allow_methods=["*"],
    allow_headers=["*"],
)


# HTTP метрики + логирование middleware
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    trace_id = log_event("info", "request", method=request.method, path=request.url.path)
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start
    track_http(request.method, request.url.path, response.status_code, duration)
    log_event("info", "response", trace_id=trace_id, method=request.method,
              path=request.url.path, status=response.status_code, duration_ms=round(duration * 1000, 2))
    return response


SANDBOX_DIR = os.path.join(os.path.dirname(__file__), "..", "sandbox")
app.mount("/static", StaticFiles(directory=SANDBOX_DIR), name="static")


@app.get("/")
async def home_page():
    """Главная: объяснение идеи проекта + быстрый старт."""
    return FileResponse(os.path.join(SANDBOX_DIR, "home.html"))


@app.get("/sandbox")
async def sandbox_page():
    """API-песочница: формы для всех эндпоинтов."""
    return FileResponse(os.path.join(SANDBOX_DIR, "index.html"))


@app.get("/metrics")
async def metrics():
    """Prometheus-метрики."""
    from app.services.metrics import get_metrics
    return Response(content=get_metrics(), media_type="text/plain; charset=utf-8")


@app.get("/ab-stats")
async def ab_stats():
    """A/B статистика по моделям."""
    from app.services.llm_client import get_ab_stats
    return get_ab_stats()


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Санитайзер и другие валидаторы выбрасывают ValueError → 422."""
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc)},
    )


# Регистрация роутеров
from app.api.events import router as events_router
from app.api.operative import router as operative_router
from app.api.memory import router as memory_router
from app.api.tools import router as tools_router
from app.api.dashboard import router as dashboard_router
from app.api.demo import router as demo_router
from app.api.agents import router as agents_router

app.include_router(events_router)
app.include_router(operative_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(dashboard_router)
app.include_router(demo_router)
app.include_router(agents_router)
from app.api.replication import router as replication_router
app.include_router(replication_router)


@app.get("/ui")
async def dashboard_page():
    """Web-дашборд: live-метрики, обозреватель слоёв, графики."""
    return FileResponse(os.path.join(SANDBOX_DIR, "dashboard.html"))


@app.get("/health")
async def health():
    """Проверка здоровья всех сервисов с детализацией."""
    from app.db.postgres import get_pool
    from app.db.redis import get_redis
    from app.db.s3 import get_s3
    from app.services.metrics import update_layer_size, get_llm_stats
    from app.services.embedder import get_embedding_provider, EMBEDDING_MODEL_NAME

    status = {"postgres": "ok", "redis": "ok", "minio": "ok"}
    layers = {}
    db_size_mb = 0

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            for layer, table in [
                ("l1", "l1_raw_events"), ("l2", "l2_daily_buffers"),
                ("l3_knowledge", "l3_master_knowledge"), ("l3_tools", "l3_tools_registry"),
                ("l4", "l4_snapshots"),
            ]:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                update_layer_size(layer, "all", count or 0)
                layers[layer] = count or 0
            # DB size
            db_size = await conn.fetchval(
                "SELECT pg_database_size(current_database())"
            )
            db_size_mb = round((db_size or 0) / (1024 * 1024), 2)
    except Exception as e:
        status["postgres"] = str(e)

    try:
        r = await get_redis()
        await r.ping()
    except Exception as e:
        status["redis"] = str(e)

    try:
        s3 = get_s3()
        s3.bucket_exists(settings.s3_bucket)
    except Exception as e:
        status["minio"] = str(e)

    all_ok = all(v == "ok" for v in status.values())

    uptime_seconds = 0
    if _start_time:
        uptime_seconds = round(
            (datetime.now(timezone.utc) - _start_time).total_seconds()
        )

    return {
        "healthy": all_ok,
        "version": __version__,
        "services": status,
        "layers": layers,
        "db_size_mb": db_size_mb,
        "uptime_seconds": uptime_seconds,
        "llm": get_llm_stats(),
        "embedding": {
            "model": EMBEDDING_MODEL_NAME,
            "provider": get_embedding_provider(),
        },
        "system": {
            "python": sys.version,
            "platform": sys.platform,
        },
    }
