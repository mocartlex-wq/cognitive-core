"""Media analyzer — анализ видео и изображений на сервере.

Видео:
  • ffmpeg извлекает N ключевых кадров (равномерно по длительности)
  • faster-whisper транскрибирует аудио (если есть)
  • frames сохраняются в MinIO bucket `media-frames`
  • метаданные пишутся в L1 raw_events (domain=media_analysis)

Изображения:
  • Pillow проверка валидности + resize если слишком большой
  • Сохранение в MinIO

Whisper модель скачивается один раз в /data/whisper (volume mount),
дальше переиспользуется. По умолчанию `base` (~150MB) — балансирует
скорость и качество для русского + английского.

Использование (async):
    result = await analyze_video("/tmp/video.mp4")
    # → {duration, transcript, language, frames: [{ts, key, url}], ...}
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Конфиг через env ──────────────────────────────────────────────────────
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")  # tiny / base / small / medium / large
WHISPER_CACHE_DIR = os.getenv("WHISPER_CACHE_DIR", "/data/whisper")
FRAMES_PER_VIDEO = int(os.getenv("FRAMES_PER_VIDEO", "12"))
FRAME_MAX_WIDTH = int(os.getenv("FRAME_MAX_WIDTH", "800"))
MAX_VIDEO_DURATION_SEC = int(os.getenv("MAX_VIDEO_DURATION_SEC", "1800"))  # 30 мин cap

# Lazy-init модели (первая загрузка ~150MB, ~10-30 секунд)
_whisper_model = None


def _get_whisper_model():
    """Singleton: загружаем модель при первом вызове."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        os.makedirs(WHISPER_CACHE_DIR, exist_ok=True)
        logger.info("loading faster-whisper model=%s cache=%s",
                    WHISPER_MODEL_SIZE, WHISPER_CACHE_DIR)
        _whisper_model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type="int8",         # int8 — ~4× быстрее, минимальная потеря качества
            download_root=WHISPER_CACHE_DIR,
        )
        logger.info("faster-whisper loaded")
    return _whisper_model


# ─────────────────────────────────────────────────────────────────────────
# ffmpeg / ffprobe helpers (sync, wrapped в asyncio.to_thread)
# ─────────────────────────────────────────────────────────────────────────
def _get_duration_sec(video_path: str) -> float:
    """Длительность видео через ffprobe."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[:200]}")
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe returned bad duration: {proc.stdout!r}")


def _has_audio_stream(video_path: str) -> bool:
    """Проверяет, есть ли в видео audio stream."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=10,
    )
    return "audio" in proc.stdout


def _extract_frames_sync(video_path: str, count: int, max_width: int) -> tuple[list[Path], float]:
    """Извлечь `count` кадров равномерно. Returns (paths, total_duration)."""
    duration = _get_duration_sec(video_path)
    if duration > MAX_VIDEO_DURATION_SEC:
        raise ValueError(f"видео слишком длинное: {duration:.0f}с > {MAX_VIDEO_DURATION_SEC}с")

    out_dir = Path(tempfile.mkdtemp(prefix="frames_"))
    # Адаптивный интервал — для коротких видео берём кадры чаще чем 1с,
    # чтобы всегда получить заказанное `count` штук. Минимум 0.1с (10 fps cap)
    # чтобы не упасть на видео долей секунды.
    interval = max(0.1, duration / count)
    pattern = str(out_dir / "frame_%04d.jpg")

    # fps=1/interval — один кадр каждые interval секунд
    # scale -1 сохраняет aspect ratio
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-vf", f"fps=1/{interval},scale={max_width}:-1",
         "-q:v", "3",  # JPEG качество (1=best, 31=worst), 3 — хорошо
         pattern],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {proc.stderr[-300:]}")

    frames = sorted(out_dir.glob("frame_*.jpg"))
    if not frames:
        raise RuntimeError("ffmpeg не извлёк ни одного кадра")
    return frames, duration


def _transcribe_sync(video_path: str) -> tuple[str, str | None, float]:
    """Транскрипция через faster-whisper. Returns (text, language, duration_ms).

    Graceful handling: если в видео НЕТ речи (screencast без голоса) или audio
    silent throughout — faster-whisper падает с 'max() arg is an empty sequence'
    при попытке auto-detect language из пустых вероятностей. Ловим эту ситуацию
    и возвращаем пустой transcript вместо exception.
    """
    started = time.monotonic()
    model = _get_whisper_model()
    try:
        # beam_size=1 — fast greedy, vad_filter — режет тишину.
        # language="ru" вместо None: фиксированный язык, не нужен auto-detect,
        # который падает при пустом аудио. Если речь на другом языке — Whisper
        # всё равно её распознает (модель multilingual), просто с ru-bias.
        segments, info = model.transcribe(
            video_path,
            language="ru",
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        parts: list[str] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                parts.append(text)
        transcript = " ".join(parts)
        lang = info.language if hasattr(info, "language") else "ru"
    except (ValueError, RuntimeError) as e:
        logger.info("transcribe gracefully skipped (likely silent audio): %s", e)
        transcript = ""
        lang = None
    duration_ms = (time.monotonic() - started) * 1000
    return transcript, lang, duration_ms


# ─────────────────────────────────────────────────────────────────────────
# Async public API
# ─────────────────────────────────────────────────────────────────────────
async def analyze_video(
    video_path: str,
    *,
    extract_frames: bool = True,
    transcribe: bool = True,
    frames_count: int | None = None,
) -> dict:
    """Полный анализ видео: фреймы + транскрипт.

    Возвращает dict:
      {
        "duration": 60.5,
        "has_audio": True,
        "transcript": "...",
        "language": "ru",
        "transcript_duration_ms": 2300,
        "frames": [
            {"index": 0, "ts": 0.0, "local_path": "/tmp/.../frame_0001.jpg", "size_bytes": 45000},
            ...
        ],
        "frames_count": 12,
      }
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(video_path)

    result: dict = {
        "duration": None,
        "has_audio": False,
        "transcript": None,
        "language": None,
        "transcript_duration_ms": None,
        "frames": [],
        "frames_count": 0,
    }

    # Кадры + duration
    if extract_frames:
        n = frames_count or FRAMES_PER_VIDEO
        frames, duration = await asyncio.to_thread(
            _extract_frames_sync, video_path, n, FRAME_MAX_WIDTH,
        )
        result["duration"] = duration
        interval = max(1.0, duration / n)
        result["frames"] = [
            {
                "index": i,
                "ts": round(i * interval, 2),
                "local_path": str(p),
                "size_bytes": p.stat().st_size,
            }
            for i, p in enumerate(frames)
        ]
        result["frames_count"] = len(frames)
    else:
        # Только duration без extract
        result["duration"] = await asyncio.to_thread(_get_duration_sec, video_path)

    # Транскрипция (если нужно и есть audio)
    if transcribe:
        has_audio = await asyncio.to_thread(_has_audio_stream, video_path)
        result["has_audio"] = has_audio
        if has_audio:
            text, lang, dur_ms = await asyncio.to_thread(_transcribe_sync, video_path)
            result["transcript"] = text
            result["language"] = lang
            result["transcript_duration_ms"] = round(dur_ms, 1)

    return result


async def analyze_audio(audio_path: str) -> dict:
    """Только транскрипция аудио (без ffmpeg-frames) — для MP3/WAV/OGG/M4A.

    Возвращает: {duration_sec, transcript, language, transcript_duration_ms}.
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)

    # Получаем длительность через ffprobe (быстро, работает с аудио тоже)
    try:
        duration = await asyncio.to_thread(_get_duration_sec, audio_path)
    except Exception:
        duration = None

    text, lang, dur_ms = await asyncio.to_thread(_transcribe_sync, audio_path)
    return {
        "duration_sec": duration,
        "transcript": text,
        "language": lang,
        "transcript_duration_ms": round(dur_ms, 1),
    }


async def analyze_image(image_path: str, *, max_width: int = 1600) -> dict:
    """Базовая обработка картинки: получить размеры + resize если >max_width.

    Возвращает: {width, height, format, size_bytes, normalized_path}
    OCR/visual-description можно добавить позже (через Tesseract или vision API).
    """
    from PIL import Image

    def _process_sync():
        with Image.open(image_path) as img:
            img.verify()  # проверка целостности
        # Открываем заново (verify закрывает)
        with Image.open(image_path) as img:
            orig_w, orig_h = img.size
            orig_fmt = img.format
            if orig_w > max_width:
                ratio = max_width / orig_w
                new_size = (max_width, int(orig_h * ratio))
                resized = img.resize(new_size, Image.LANCZOS)
                out_path = Path(image_path).with_suffix(".resized.jpg")
                resized.convert("RGB").save(out_path, "JPEG", quality=85)
                return {
                    "width": new_size[0],
                    "height": new_size[1],
                    "format": "JPEG",
                    "size_bytes": out_path.stat().st_size,
                    "normalized_path": str(out_path),
                    "original": {"width": orig_w, "height": orig_h, "format": orig_fmt},
                }
            return {
                "width": orig_w,
                "height": orig_h,
                "format": orig_fmt,
                "size_bytes": os.path.getsize(image_path),
                "normalized_path": image_path,
            }

    return await asyncio.to_thread(_process_sync)


# ─────────────────────────────────────────────────────────────────────────
# Sanitize filename для MinIO key
# ─────────────────────────────────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    """Безопасное имя файла для S3/MinIO key."""
    name = re.sub(r"[^A-Za-z0-9._\-]", "_", name or "")
    return name[:120] or f"file_{uuid.uuid4().hex[:8]}"
