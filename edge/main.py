"""me-ai-edge — локальный edge-процессор медиа для Cognitive Core.

Зачем
=====
Cognitive Core сейчас обрабатывает медиа (видео/аудио/картинки) на сервере:
агент загружает raw-байты наружу → сервер гоняет ffmpeg + Whisper → embeddings.
Для security-strict тенантов (gov / military / medical, PII в кадрах и речи)
это неприемлемо — raw-байты не должны покидать периметр.

me-ai-edge запускается ЛОКАЛЬНО на машине агента (docker compose up) и делает
ту же обработку on-prem. Наружу, в облачный Cognitive Core, уходят ТОЛЬКО
производные данные: текст транскрипта, векторы embeddings и метаданные
(длительность / кодек / число кадров). Raw audio/video байты и JPEG-кадры
НИКОГДА не покидают локальную машину.

Статус: 0.1 STUB. Endpoints /process/* зарегистрированы и типизированы, но
реальной обработки пока нет — она появится в следующем PR (M4 0.2). Сейчас
они возвращают {status: "stub_not_implemented"} с описанием будущего flow.

Будущий flow (0.2), реализуется в /process/video:
    1. Принять raw-байты ЛОКАЛЬНО (multipart upload с того же хоста).
    2. ffmpeg извлекает N ключевых кадров + аудио-дорожку (во временный tmpfs).
    3. faster-whisper транскрибирует аудио локально (CPU int8).
    4. Локальная embed-модель считает векторы из transcript + кадров.
    5. POST upstream COGCORE_SERVER_URL/api/embeddings/ingest с COGCORE_API_KEY —
       уходят только embeddings + transcript text + metadata.
    6. Временные файлы (кадры, аудио) удаляются. Raw-видео не сохраняется и
       не отправляется. Целевой домен upstream проверяется по
       ALLOWED_UPSTREAM_DOMAINS (anti-exfil whitelist).
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

# ── Версия edge-компонента (semver edge-роадмапа, не версия Cognitive Core) ──
EDGE_VERSION = "0.1.0-stub"

# ── Конфиг через env ────────────────────────────────────────────────────────
# COGCORE_SERVER_URL    — базовый URL облачного Cognitive Core (куда POST-ить
#                         embeddings). Пусто = edge не сконфигурирован, /health
#                         вернёт server_configured=false.
# COGCORE_API_KEY       — per-agent API-ключ. Edge хранит его локально и шлёт
#                         в Authorization при upstream-ingest. Сервер валидирует.
# WHISPER_MODEL_SIZE    — tiny / base / small / medium / large (как на сервере).
# ALLOWED_UPSTREAM_DOMAINS — CSV whitelist доменов, на которые edge вправе
#                         отправлять данные. Anti-exfil guard: даже при подмене
#                         COGCORE_SERVER_URL байты не уйдут на чужой хост.
COGCORE_SERVER_URL = os.getenv("COGCORE_SERVER_URL", "").strip()
COGCORE_API_KEY = os.getenv("COGCORE_API_KEY", "").strip()
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base").strip()
ALLOWED_UPSTREAM_DOMAINS = [
    d.strip().lower()
    for d in os.getenv(
        "ALLOWED_UPSTREAM_DOMAINS",
        "mcp.me-ai.ru,mcp.xn--80aiacb7adkj.xn--p1ai",
    ).split(",")
    if d.strip()
]

# ── Pydantic модели (typed responses) ────────────────────────────────────────


class HealthResponse(BaseModel):
    """Ответ /health — используется docker HEALTHCHECK и агентом для проверки."""

    healthy: bool
    version: str
    server_configured: bool
    whisper_model: str


class StubResponse(BaseModel):
    """Заглушка для /process/* до реализации обработки в 0.2."""

    status: str = "stub_not_implemented"
    todo: str = "M4 next PR"
    media_kind: str
    future_flow: list[str]
    upstream_target: str | None = None
    raw_bytes_leave_local: bool = False


# ── App ───────────────────────────────────────────────────────────────────--

app = FastAPI(
    title="me-ai-edge",
    version=EDGE_VERSION,
    summary="Local on-prem media processor for Cognitive Core (privacy edge).",
)


def _upstream_target() -> str | None:
    """Вернуть upstream URL, только если домен в whitelist (anti-exfil).

    Даже на stub-стадии возвращаем валидированный таргет в ответе, чтобы агент
    видел, КУДА в будущем уйдут embeddings, и мог убедиться, что это его сервер.
    """
    if not COGCORE_SERVER_URL:
        return None
    host = (urlparse(COGCORE_SERVER_URL).hostname or "").lower()
    if host and host in ALLOWED_UPSTREAM_DOMAINS:
        return COGCORE_SERVER_URL.rstrip("/") + "/api/embeddings/ingest"
    # Домен не в whitelist — таргета нет, отправка будет заблокирована.
    return None


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness + конфиг-статус edge-процессора.

    server_configured=true означает, что заданы и URL, и API-key — то есть в
    0.2 edge сможет отправлять embeddings наверх. На stub-стадии всегда healthy.
    """
    return HealthResponse(
        healthy=True,
        version=EDGE_VERSION,
        server_configured=bool(COGCORE_SERVER_URL and COGCORE_API_KEY),
        whisper_model=WHISPER_MODEL_SIZE,
    )


@app.post("/process/video", response_model=StubResponse)
async def process_video() -> StubResponse:
    """[STUB] Локальная обработка видео → upstream embeddings.

    Будущий flow (0.2): ffmpeg извлекает кадры + аудио во временный tmpfs →
    faster-whisper транскрибирует речь локально → локальная embed-модель
    считает векторы → POST upstream /api/embeddings/ingest (только transcript +
    embeddings + metadata). Raw-видео и JPEG-кадры остаются на машине и
    удаляются после обработки — наружу не уходят.
    """
    return StubResponse(
        media_kind="video",
        upstream_target=_upstream_target(),
        future_flow=[
            "ffmpeg extract frames + audio (local tmpfs)",
            "faster-whisper transcribe audio (local CPU int8)",
            "compute embeddings from transcript + frames (local)",
            "POST embeddings + transcript + metadata upstream (API key)",
            "delete temp frames/audio — raw bytes never leave host",
        ],
    )


@app.post("/process/audio", response_model=StubResponse)
async def process_audio() -> StubResponse:
    """[STUB] Локальная транскрипция аудио → upstream embeddings.

    Будущий flow (0.2): faster-whisper транскрибирует MP3/WAV/OGG/M4A локально →
    embeddings из текста → POST upstream. Raw-аудио не покидает машину.
    """
    return StubResponse(
        media_kind="audio",
        upstream_target=_upstream_target(),
        future_flow=[
            "faster-whisper transcribe audio (local CPU int8)",
            "compute embeddings from transcript (local)",
            "POST embeddings + transcript + metadata upstream (API key)",
            "raw audio never leaves host",
        ],
    )


@app.post("/process/image", response_model=StubResponse)
async def process_image() -> StubResponse:
    """[STUB] Локальная обработка изображения → upstream embeddings.

    Будущий flow (0.2): Pillow валидирует + нормализует картинку локально →
    локальная vision/embed-модель считает вектор → POST upstream. Сам JPEG/PNG
    остаётся на машине.
    """
    return StubResponse(
        media_kind="image",
        upstream_target=_upstream_target(),
        future_flow=[
            "Pillow validate + normalize image (local)",
            "compute embedding from image (local)",
            "POST embedding + metadata upstream (API key)",
            "raw image never leaves host",
        ],
    )


if __name__ == "__main__":
    # Внутри контейнера слушаем 0.0.0.0; наружу публикуемся только на 127.0.0.1
    # через docker-compose (localhost-only bind — security).
    uvicorn.run(app, host="0.0.0.0", port=9099)
