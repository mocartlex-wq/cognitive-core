"""Real embeddings: fastembed (multilingual-e5-small, 384 dim, CPU/GPU).

Цепочка fallback:
  1. fastembed (multilingual-e5-small) — основной путь, локально
     - CUDA провайдер если EMBEDDING_USE_GPU=true и onnxruntime-gpu установлен
     - CPU провайдер по дефолту (минимум зависимостей)
  2. OpenAI text-embedding-3-small — если задан OPENAI_API_KEY и DIM=1536
  3. Ollama nomic-embed-text — если LOCAL_AI_ENABLED=true
  4. Хеш-эмбеддинг — последний fallback

Размерность определяется в одном месте: EMBEDDING_DIM.
Все слои используют одну модель = одна размерность = совместимость с RediSearch.

GPU acceleration:
  - Установить onnxruntime-gpu в контейнер (см. Dockerfile.gpu)
  - Поднять стек с GPU: docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
  - Ускорение: ~5-7x на embed (15-30ms CPU → 3-5ms GPU)
  - Не критично для текущей нагрузки, имеет смысл для multimodal v0.7
"""
import hashlib
import logging
import os
import struct

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384  # multilingual MiniLM (одинакова с e5-small)
# fastembed >= 0.4 убрал intfloat/multilingual-e5-small из реестра.
# sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 — официально поддерживаемая
# 384-dim multilingual альтернатива (50+ языков, recall сопоставим).
EMBEDDING_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Версия эмбеддинг-модели (для hot-reload: меняется → старые векторы помечаются stale)
EMBEDDING_MODEL_VERSION = f"{EMBEDDING_MODEL_NAME}:{EMBEDDING_DIM}"

# Опциональная GPU-акселерация (требует onnxruntime-gpu в контейнере + nvidia-container-toolkit)
EMBEDDING_USE_GPU = os.getenv("EMBEDDING_USE_GPU", "false").lower() in ("true", "1", "yes")

_fastembed_model = None
_fastembed_failed = False
_fastembed_provider = None  # "CUDA" / "CPU" — для логирования и метрик
_hash_warned = False


def get_model_version() -> str:
    """Возвращает уникальную строку идентифицирующую текущую модель.
    Используется для hot-reload: при смене модели в .env → старые векторы → stale."""
    return EMBEDDING_MODEL_VERSION


def _detect_cuda_available() -> bool:
    """Проверяет: есть ли CUDA EP в установленном onnxruntime."""
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        return "CUDAExecutionProvider" in providers
    except Exception:
        return False


def _get_fastembed():
    """Lazy-load fastembed. Кешируется. False = модель недоступна.
    Если EMBEDDING_USE_GPU=true и CUDA доступна — использует GPU.
    """
    global _fastembed_model, _fastembed_failed, _fastembed_provider
    if _fastembed_failed:
        return None
    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding
            kwargs = {
                "model_name": EMBEDDING_MODEL_NAME,
                "cache_dir": "/tmp/fastembed_cache",
            }
            # Опционально включаем CUDA провайдер, если запрошено и доступно
            if EMBEDDING_USE_GPU and _detect_cuda_available():
                kwargs["providers"] = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                _fastembed_provider = "CUDA"
            else:
                if EMBEDDING_USE_GPU:
                    logger.warning(
                        "EMBEDDING_USE_GPU=true но CUDAExecutionProvider недоступен — "
                        "fallback на CPU. Установите onnxruntime-gpu в контейнер."
                    )
                _fastembed_provider = "CPU"

            _fastembed_model = TextEmbedding(**kwargs)
            logger.info(
                "fastembed loaded: %s (%d dim) provider=%s",
                EMBEDDING_MODEL_NAME, EMBEDDING_DIM, _fastembed_provider,
            )
        except Exception as e:
            logger.warning("fastembed unavailable: %s", e)
            _fastembed_failed = True
            return None
    return _fastembed_model


def get_embedding_provider() -> str:
    """Возвращает имя текущего провайдера ('CUDA'/'CPU'/'unavailable').
    Используется в /health и /metrics."""
    if _fastembed_failed:
        return "unavailable"
    if _fastembed_provider is None:
        # Лениво вызвать инициализацию — но не блокировать health-check долго
        return "not-initialized"
    return _fastembed_provider


async def embed_text(text: str) -> list[float]:
    """Возвращает 384-dim эмбеддинг для текста."""
    if not text:
        return [0.0] * EMBEDDING_DIM
    text = text[:8000]  # truncate для стабильности

    # 1. fastembed (основной путь)
    model = _get_fastembed()
    if model is not None:
        try:
            embeddings = list(model.embed([text]))
            if embeddings:
                vec = embeddings[0].tolist()
                if len(vec) == EMBEDDING_DIM:
                    return vec
        except Exception as e:
            logger.warning("fastembed embed failed: %s", e)

    # 2. Хеш-fallback
    global _hash_warned
    if not _hash_warned:
        logger.warning(
            "Using hash-embedding fallback. Install fastembed for real KNN quality."
        )
        _hash_warned = True
    return _hash_embed(text)


def _hash_embed(text: str) -> list[float]:
    """Детерминированный хеш-эмбеддинг (последний fallback)."""
    text = text.lower().strip()
    vec = [0.0] * EMBEDDING_DIM

    words = text.split()
    for i, w in enumerate(words):
        idx = _hash_to_index(w, EMBEDDING_DIM)
        vec[idx] += 1.0
        if i < 3:
            idx2 = _hash_to_index(f"pos:{w}", EMBEDDING_DIM)
            vec[idx2] += 0.5

    for i in range(len(text) - 2):
        trigram = text[i:i + 3]
        idx = _hash_to_index(trigram, EMBEDDING_DIM)
        vec[idx] += 0.25

    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        idx = _hash_to_index(bigram, EMBEDDING_DIM)
        vec[idx] += 0.2

    for i in range(len(words) - 1):
        pair = f"{words[i]}:{words[i+1]}"
        idx = _hash_to_index(pair, EMBEDDING_DIM)
        vec[idx] += 0.4

    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _hash_to_index(s: str, dim: int) -> int:
    h = hashlib.md5(s.encode()).digest()
    return struct.unpack("<I", h[:4])[0] % dim
