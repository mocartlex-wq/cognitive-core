import pytest

from app.services import embedder
from app.services.embedder import (
    EMBEDDING_DIM,
    _hash_embed,
    embed_text,
    get_embedding_provider,
    is_embedding_degraded,
)


class TestHashEmbed:
    def test_dimension(self):
        vec = _hash_embed("hello world")
        assert len(vec) == EMBEDDING_DIM

    def test_deterministic(self):
        a = _hash_embed("the same input")
        b = _hash_embed("the same input")
        assert a == b

    def test_normalized(self):
        vec = _hash_embed("test vector normalization")
        norm = sum(v * v for v in vec) ** 0.5
        assert abs(norm - 1.0) < 0.001

    def test_different_inputs(self):
        a = _hash_embed("python code testing")
        b = _hash_embed("javascript frontend ui")
        assert a != b

    def test_empty_string(self):
        vec = _hash_embed("")
        assert len(vec) == EMBEDDING_DIM

    @pytest.mark.asyncio
    async def test_embed_text_fallback(self):
        vec = await embed_text("test text for embedding")
        assert len(vec) == EMBEDDING_DIM
        assert all(isinstance(v, float) for v in vec)


class TestEmbeddingProviderSignal:
    """Регрессия (2026-06-14 аудит): провайдер должен честно сообщать
    'hash-fallback' при недоступном fastembed — а не обтекаемое 'unavailable'.
    /health использует это, чтобы выставить healthy=false когда KNN
    деградирован до случайного поиска."""

    def setup_method(self):
        self._saved = (embedder._fastembed_failed, embedder._fastembed_provider)

    def teardown_method(self):
        embedder._fastembed_failed, embedder._fastembed_provider = self._saved

    def test_provider_reports_hash_fallback_when_fastembed_failed(self):
        embedder._fastembed_failed = True
        embedder._fastembed_provider = None
        assert get_embedding_provider() == "hash-fallback"
        assert is_embedding_degraded() is True

    def test_provider_reports_not_initialized_before_warmup(self):
        embedder._fastembed_failed = False
        embedder._fastembed_provider = None
        assert get_embedding_provider() == "not-initialized"
        assert is_embedding_degraded() is False

    def test_provider_reports_loaded_state(self):
        embedder._fastembed_failed = False
        embedder._fastembed_provider = "CPU"
        assert get_embedding_provider() == "CPU"
        assert is_embedding_degraded() is False
