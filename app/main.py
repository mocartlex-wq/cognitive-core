"""Cognitive Core FastAPI bootstrap.

Изменения 2026-05-17:
  • Подключены auth_router + user_router (магик-ссылка вход + профиль)
  • /ui/login → sandbox/login.html
  • /ui/profile → sandbox/profile.html
  • CORS allow_credentials=True для /auth/* и /user/* (нужно для cookies),
    остальные endpoints — без credentials (агенты ходят через X-API-Key)
  • Secret-redaction middleware в логах (Phase 3 task 3.2)
"""
import asyncio
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db.postgres import close_db, init_db
from app.db.redis import close_redis, init_redis
from app.db.s3 import init_s3
from app.services.metrics import log_event, track_http

__version__ = "0.6.0"  # bumped 2026-05-17 (accounts + email)

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
        from app.db.postgres import get_pool
        from app.replication import OutboxPublisher
        pool = await get_pool()
        if pool is not None:
            _outbox_publisher = OutboxPublisher(pool)
            await _outbox_publisher.start()
            log_event("info", "OutboxPublisher started")
    except Exception as e:
        log_event("warn", "OutboxPublisher disabled", error=str(e))

    # Pre-cache Whisper модели в фоне. Если её ещё нет в volume — закачается
    # с HuggingFace при старте api, а не при первом upload'е. Загружаем не-
    # блокирующе через task: 30-60с на cold-cache, но api уже принимает запросы.
    async def _prefetch_whisper():
        try:
            from app.services.media_analyzer import _get_whisper_model
            await asyncio.to_thread(_get_whisper_model)
            log_event("info", "Whisper model pre-cached")
        except Exception as e:
            log_event("warn", "Whisper pre-cache failed", error=str(e)[:200])
    asyncio.create_task(_prefetch_whisper())

    # Media cleanup loop — TTL 15 мин, scan каждые 5 мин (owner-decision).
    # Удаляет MinIO files старше TTL, L1 metadata остаётся вечно.
    try:
        from app.services.media_cleanup import cleanup_loop as _media_cleanup_loop
        _media_cleanup_task = asyncio.create_task(_media_cleanup_loop())
        app.state.media_cleanup_task = _media_cleanup_task
        log_event("info", "media_cleanup loop started (TTL=15min)")
    except Exception as e:
        log_event("warn", "media_cleanup loop failed to start", error=str(e))

    log_event("info", "Cognitive Core ready")
    yield

    if _outbox_publisher:
        await _outbox_publisher.stop()
    if _scheduler_task:
        _scheduler_task.cancel()
    if hasattr(app.state, "media_cleanup_task"):
        app.state.media_cleanup_task.cancel()
    await close_db()
    await close_redis()
    log_event("info", "Cognitive Core shutdown")


app = FastAPI(
    title="Cognitive Core",
    description="5-слойная система памяти с AI-куратором + аккаунты",
    version=__version__,
    lifespan=lifespan,
)

# CORS: разрешены креды для cookie-flow. Origins по умолчанию — все.
# Для production'а ставим в .env CORS_ORIGINS_CSV="https://aimail.art,https://mcp.me-ai.ru,https://mcp.ии-память.рф"
_origins_env = os.getenv("CORS_ORIGINS_CSV", "").strip()
if _origins_env:
    _origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Без явных origins нельзя совмещать "*" + credentials=True (браузер откажет).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ─────────────────────────────────────────────────────────────────────────
# Secret-redaction для логов (Phase 3 task 3.2)
# ─────────────────────────────────────────────────────────────────────────
_REDACT_HEADERS = {
    "authorization", "x-api-key", "x-room-key", "x-session-id",
    "cookie", "set-cookie",
}
_REDACT_QUERY_RE = re.compile(
    r"\b(token|api_key|apikey|key|password|secret)=([^&\s]+)",
    re.IGNORECASE,
)


def _redact_path(path: str, query: str) -> str:
    """Маскирует sensitive query-параметры в пути для логирования."""
    if not query:
        return path
    masked = _REDACT_QUERY_RE.sub(r"\1=***", query)
    return f"{path}?{masked}"


# HTTP метрики + логирование middleware
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    # Для лога — маскируем токены в URL
    safe_path = request.url.path
    if settings.log_redact_secrets and request.url.query:
        safe_path = _redact_path(request.url.path, request.url.query)

    trace_id = log_event("info", "request", method=request.method, path=safe_path)
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start
    track_http(request.method, request.url.path, response.status_code, duration)
    log_event("info", "response", trace_id=trace_id, method=request.method,
              path=safe_path, status=response.status_code, duration_ms=round(duration * 1000, 2))
    return response


SANDBOX_DIR = os.path.join(os.path.dirname(__file__), "..", "sandbox")
app.mount("/static", StaticFiles(directory=SANDBOX_DIR), name="static")


# UI HTML-страницы — НЕ кешируем (часто меняем дизайн + auth-зависимый контент).
# Static (CSS/JS/SVG) кешируется через query-string version, а HTML всегда свежий.
_NO_CACHE_HTML = {"Cache-Control": "no-store, no-cache, must-revalidate"}


def _html(path: str):
    return FileResponse(os.path.join(SANDBOX_DIR, path), headers=_NO_CACHE_HTML)


# Favicon — браузеры автоматически запрашивают /favicon.ico на каждой странице.
# Без этого route на каждой странице будет 404 в консоли. SVG-иконка inline,
# без отдельного файла. Кешируется на сутки.
_FAVICON_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    b'<rect width="64" height="64" rx="14" fill="#58a6ff"/>'
    b'<text x="50%" y="58%" font-size="34" font-weight="700" fill="white" '
    b'text-anchor="middle" font-family="system-ui">C</text>'
    b'</svg>'
)


@app.get("/favicon.ico")
async def favicon():
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/")
async def home_page():
    """Главная: объяснение идеи проекта + быстрый старт."""
    return _html("home.html")


@app.get("/sandbox")
async def sandbox_page():
    """API-песочница: формы для всех эндпоинтов."""
    return _html("index.html")


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


# ─────────────────────────────────────────────────────────────────────────
# Регистрация роутеров
# ─────────────────────────────────────────────────────────────────────────
from app.api.agents import router as agents_router
from app.api.agents_collab import router as agents_collab_router
from app.api.dashboard import router as dashboard_router
from app.api.demo import router as demo_router
from app.api.events import router as events_router
from app.api.memory import router as memory_router
from app.api.onboard import router as onboard_router
from app.api.operative import router as operative_router
from app.api.rules import router as rules_router
from app.api.tools import router as tools_router

app.include_router(events_router)
app.include_router(operative_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(dashboard_router)
app.include_router(demo_router)
app.include_router(agents_router)
app.include_router(agents_collab_router)
app.include_router(onboard_router)
app.include_router(rules_router)
from app.api.replication import router as replication_router

app.include_router(replication_router)
from app.api.mcp_protocol import router as mcp_router

app.include_router(mcp_router)

# Новые роутеры (2026-05-17): аккаунты + magic-link авторизация
from app.api.auth import router as auth_router
from app.api.user import router as user_router

app.include_router(auth_router)
app.include_router(user_router)

# Frontend error reporter (2026-05-20): /api/errors POST/GET
from app.api.docs_serve import router as docs_serve_router
from app.api.errors import router as errors_router

app.include_router(errors_router)
app.include_router(docs_serve_router)

# Media upload + analyze (2026-05-20): /api/media/{video,image,list,frame/...}
from app.api.media import router as media_router

app.include_router(media_router)

# Video generation (Phase post-launch 2026-05-26): /api/video/{generate,status/{id}}
# Per-tenant Kling/Sora API key через user_external_keys table
from app.api.video import router as video_router  # noqa: E402

app.include_router(video_router)

# Billing (Phase post-launch 2026-05-26): Stripe + ЮKassa subscriptions
# /api/billing/checkout/{tier}, /webhook/{provider}, /subscriptions/me
from app.api.admin_audit import router as admin_audit_router
from app.api.admin_slo import router as admin_slo_router
from app.api.billing import router as billing_router  # noqa: E402
from app.api.webhooks import router as webhooks_router

app.include_router(billing_router)
app.include_router(admin_slo_router)
app.include_router(admin_audit_router)
app.include_router(webhooks_router)

# Unified Agent Onboarding wizard (2026-05-21): /user/connect/* + /user/agents/{id}/verify
from app.api.connect import router as connect_router
from app.api.connect import verify_router as agents_verify_router

app.include_router(connect_router)
app.include_router(agents_verify_router)

# OpenAPI generator для ChatGPT Custom GPT (2026-05-21): /api/openapi/cognitive.{json,yaml}
from app.api.openapi_gen import router as openapi_router

app.include_router(openapi_router)

# Admin tenants management (Phase 5B 2026-05-22): /admin/tenants + tier change/suspend
from app.api.admin import router as admin_router

app.include_router(admin_router)

# Per-tenant external AI provider keys (2026-05-24): /user/settings/external-key*
# Owner-mandate: opt-in tenant keys для Qwen / MiniMax / GigaChat / Claude / OpenAI / Gemini,
# чтобы каждый платил со своего api_key, а не со shared платформенного.
from app.api.user_settings import router as user_settings_router

app.include_router(user_settings_router)


# ─────────────────────────────────────────────────────────────────────────
# UI страницы
# ─────────────────────────────────────────────────────────────────────────
@app.get("/ui")
async def dashboard_page():
    """Web-дашборд: live-метрики, обозреватель слоёв, графики."""
    return _html("dashboard.html")


@app.get("/ui/login")
async def login_page():
    """Страница входа — magic-link."""
    return _html("login.html")


@app.get("/ui/room")
async def room_page():
    return _html("room.html")


@app.get("/ui/profile")
async def profile_page():
    """Профиль — мои комнаты, помощники, устройства."""
    return _html("profile.html")


@app.get("/ui/admin/errors")
async def admin_errors_page():
    """Админ-панель: лог фронтенд-ошибок. Доступ проверяется в API
    (/api/errors требует is_admin). HTML отдаётся всем, но если нет
    прав — страница покажет 'Нужны права администратора'."""
    return _html("admin-errors.html")


@app.get("/ui/admin/media")
async def admin_media_page():
    """Админ-панель: загрузка видео/картинок с авто-анализом."""
    return _html("admin-media.html")


@app.get("/ui/admin/rule-proposals")
async def admin_rule_proposals_page():
    """Admin: review pending rule proposals from tenants."""
    return _html("admin-rule-proposals.html")


@app.get("/ui/connect")
async def connect_page():
    """Wizard «Подключить помощника» — 5-step flow для любой платформы."""
    return _html("connect.html")


@app.get("/ui/connect/mobile")
async def connect_mobile_page():
    """Mobile QR landing — single-screen с api_key + iOS Shortcut/Tasker hint."""
    return _html("connect-mobile.html")


@app.get("/ui/pricing")
async def pricing_page():
    """Phase 5B — публичная landing с тарифами (Free / Pro / Enterprise)."""
    return _html("pricing.html")


@app.get("/ui/welcome")
async def welcome_page():
    """Phase 5C — onboarding flow после OTP signup. 3 шага до first remember."""
    return _html("welcome.html")


@app.get("/ui/admin/tenants")
async def admin_tenants_page():
    """Phase 5B — admin-only список tenant'ов с usage + actions. Auth-gate в JS."""
    return _html("admin-tenants.html")


# ─────────────────────────────────────────────────────────────────────────
# Static installers: cogmedia + install-cogcore.{sh,ps1} раздаются как
# обычные файлы из sandbox/ (через StaticFiles mount выше). Source-of-truth
# живёт в scripts/, копии в sandbox/ обновляются при коммите. Это потому
# что StaticFiles mount перехватывает /static/* раньше route handlers,
# и явные @app.get('/static/...') routes были бы недостижимы.
# ─────────────────────────────────────────────────────────────────────────


# Алиасы для login (на случай закладок с trailing slash, короткого пути и т.п.)
@app.get("/login")
@app.get("/signin")
@app.get("/войти")
@app.get("/ui/login/")
@app.get("/auth/login")
async def login_aliases():
    """Все распространённые варианты URL для входа → редирект на /ui/login."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/login", status_code=301)


@app.get("/profile")
@app.get("/ui/profile/")
async def profile_aliases():
    """Алиасы для профиля."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/profile", status_code=301)


# ─────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Проверка здоровья всех сервисов с детализацией."""
    from app.db.postgres import get_pool
    from app.db.redis import get_redis
    from app.db.s3 import get_s3
    from app.services.embedder import EMBEDDING_MODEL_NAME, get_embedding_provider
    from app.services.llm_client import get_circuit_states
    from app.services.metrics import get_llm_stats, update_layer_size

    status = {"postgres": "ok", "redis": "ok", "minio": "ok"}
    layers = {}
    db_size_mb = 0
    deep = {}  # deep-health probes per layer (sprint task #4)

    # ─── Postgres: count + last_consolidation timestamps + query timing ──
    try:
        t0 = time.monotonic()
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
            db_size = await conn.fetchval("SELECT pg_database_size(current_database())")
            db_size_mb = round((db_size or 0) / (1024 * 1024), 2)
            # Deep: timestamps of last L2/L3 consolidation
            last_l2 = await conn.fetchval("SELECT MAX(date) FROM l2_daily_buffers")
            last_l3 = await conn.fetchval("SELECT MAX(created_at) FROM l3_master_knowledge")
            last_l4 = await conn.fetchval("SELECT MAX(created_at) FROM l4_snapshots")
            deep["postgres_query_ms"] = round((time.monotonic() - t0) * 1000, 1)
            deep["last_l2_buffer"] = last_l2.isoformat() if last_l2 else None
            deep["last_l3_knowledge"] = last_l3.isoformat() if last_l3 else None
            deep["last_l4_snapshot"] = last_l4.isoformat() if last_l4 else None
            # Новое: счётчики аккаунтов/сессий (если таблицы созданы 0003-миграцией)
            try:
                accs = await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL")
                sess = await conn.fetchval(
                    "SELECT COUNT(*) FROM sessions WHERE NOT revoked AND expires_at > NOW()"
                )
                deep["accounts_active"] = accs or 0
                deep["sessions_active"] = sess or 0
            except Exception:
                deep["accounts_active"] = None  # таблицы ещё не созданы (до миграции 0003)
    except Exception as e:
        status["postgres"] = str(e)

    # ─── Redis: ping + RediSearch index existence ─────────────────────────
    try:
        t0 = time.monotonic()
        r = await get_redis()
        await r.ping()
        deep["redis_ping_ms"] = round((time.monotonic() - t0) * 1000, 1)
        try:
            from app.db.redis import get_redis_raw
            raw = await get_redis_raw()
            info = await raw.execute_command("FT.INFO", "idx:operative")
            deep["redis_index_ok"] = info is not None
        except Exception as e:
            deep["redis_index_ok"] = False
            deep["redis_index_error"] = str(e)[:100]
    except Exception as e:
        status["redis"] = str(e)

    # ─── MinIO: bucket exists + can list ──────────────────────────────────
    try:
        t0 = time.monotonic()
        s3 = get_s3()
        bucket_ok = s3.bucket_exists(settings.s3_bucket)
        deep["minio_bucket_ok"] = bool(bucket_ok)
        deep["minio_query_ms"] = round((time.monotonic() - t0) * 1000, 1)
    except Exception as e:
        status["minio"] = str(e)

    # ─── Email backend health (легковесная проверка, без отправки) ───────
    try:
        deep["email_backend"] = settings.email_backend
        deep["email_smtp_configured"] = bool(settings.smtp_host and (
            settings.email_backend == "stdout" or
            (settings.smtp_user and settings.smtp_password)
        ))
    except Exception:
        pass

    # ─── Disk free (host volume; container sees /backups if mounted) ──────
    try:
        import shutil
        usage = shutil.disk_usage("/")
        deep["disk_free_gb"] = round(usage.free / (1024**3), 2)
        deep["disk_used_pct"] = round(usage.used / usage.total * 100, 1)
    except Exception as e:
        deep["disk_error"] = str(e)[:100]

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
        "llm_circuit_breakers": get_circuit_states(),
        "deep": deep,
        "embedding": {
            "model": EMBEDDING_MODEL_NAME,
            "provider": get_embedding_provider(),
        },
        "system": {
            "python": sys.version,
            "platform": sys.platform,
        },
    }
