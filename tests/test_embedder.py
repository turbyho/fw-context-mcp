"""Tests for the embedder abstraction."""

from __future__ import annotations

import pytest

from fw_context_mcp.config.settings import DESCRIPTION_VERSION, LLMConfig
from fw_context_mcp.llm.embedder import Embedder, get_embedder


class FakeEmbedder(Embedder):
    """Deterministic embedder for testing."""

    def __init__(self, *, name: str = "fake", dim: int = 8, simulate_dim: bool = True) -> None:
        self._name = name
        self._dim = dim
        self._simulate_dim = simulate_dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._simulate_dim:
            dim = self._dim
        else:
            dim = len(texts[0]) if texts else self._dim
        return [[float(i + j) for j in range(dim)] for i, _ in enumerate(texts)]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def max_tokens(self) -> int:
        return 512


class TestEmbedderAbstract:
    def test_embed_documents(self) -> None:
        e = FakeEmbedder(dim=4)
        result = e.embed_documents(["a", "b"])
        assert len(result) == 2
        assert len(result[0]) == 4
        assert result[0] == [0.0, 1.0, 2.0, 3.0]
        assert result[1][:2] == [1.0, 2.0]

    def test_embed_queries(self) -> None:
        e = FakeEmbedder(dim=4)
        result = e.embed_queries(["query"])
        assert len(result) == 1
        assert len(result[0]) == 4

    def test_name(self) -> None:
        e = FakeEmbedder(name="my-model")
        assert e.name == "my-model"

    def test_dim(self) -> None:
        e = FakeEmbedder(dim=128)
        assert e.dim == 128

    def test_max_tokens(self) -> None:
        e = FakeEmbedder()
        assert e.max_tokens == 512


class TestGetEmbedder:
    def test_ollama_default(self) -> None:
        cfg = LLMConfig(embed_model="mxbai-embed-large:latest")
        e = get_embedder(cfg)
        assert e.name == "mxbai-embed-large:latest"
        assert e.max_tokens == 512

    def test_qwen3_token_limit(self) -> None:
        cfg = LLMConfig(embed_model="qwen3-embedding:8b")
        e = get_embedder(cfg)
        assert e.max_tokens == 8192

    def test_ollama_default_for_unknown_model(self) -> None:
        cfg = LLMConfig(embed_model="some-unknown-model")
        e = get_embedder(cfg)
        assert e.max_tokens == 512  # conservative default

    @pytest.mark.parametrize("model,max_tokens", [
        ("ibm-granite/granite-embedding-311m-multilingual-r2", 32768),
        ("ibm-granite/granite-embedding-97m-multilingual-r2", 32768),
        ("lightonai/LateOn-Code-edge", 2048),
        ("BAAI/bge-small-en-v1.5", 512),
        ("cross-encoder/ms-marco-MiniLM-L6-v2", 32768),
        ("sentence-transformers/all-MiniLM-L6-v2", 32768),
    ])
    def test_st_prefix_routing(self, model: str, max_tokens: int) -> None:
        """ST prefix should route to ST backend.  The type is checked before
        lazy-loading — actual import failure (no sentence-transformers) is
        deferred to first embed call and not tested here."""
        import importlib
        st_spec = importlib.util.find_spec("sentence_transformers")
        if st_spec is None:
            pytest.skip("sentence-transformers not installed")
        cfg = LLMConfig(embed_model=model)
        e = get_embedder(cfg)
        assert e.name == model
        assert e.max_tokens == max_tokens


class TestModelKey:
    def test_embed_key_includes_description_version(self) -> None:
        cfg = LLMConfig(embed_model="mxbai-embed-large:latest")
        key = cfg.embed_key()
        assert key == f"mxbai-embed-large:latest:{DESCRIPTION_VERSION}"

    def test_embed_key_qwen3(self) -> None:
        cfg = LLMConfig(embed_model="qwen3-embedding:8b")
        key = cfg.embed_key()
        assert key == f"qwen3-embedding:8b:{DESCRIPTION_VERSION}"


class TestTruncateBody:
    def test_short_body_unchanged(self) -> None:
        from fw_context_mcp.indexer.runner import _truncate_body
        body = "int x = 1;\nreturn x;"
        result = _truncate_body(body, head=100, tail=50)
        assert result == body

    def test_long_body_truncated(self) -> None:
        from fw_context_mcp.indexer.runner import _truncate_body
        body = "x" * 2500
        result = _truncate_body(body, head=1200, tail=800)
        assert result.startswith("x" * 1200)
        assert "// ..." in result
        assert result.endswith("x" * 800)
        assert len(result) < len(body)
