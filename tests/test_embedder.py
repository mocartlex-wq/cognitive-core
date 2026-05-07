import pytest
from app.services.embedder import embed_text, _hash_embed, EMBEDDING_DIM


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
