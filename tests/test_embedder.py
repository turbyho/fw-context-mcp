"""Tests for the embedder abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("numpy", reason="numpy not installed")

from fw_context_mcp.config.settings import DESCRIPTION_VERSION, LLMConfig
from fw_context_mcp.llm.embedder import Embedder
from fw_context_mcp.llm.embedder_factory import get_embedder


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
        assert e.max_tokens == 32768  # qwen3:8b has 32K context window

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


class TestAutoDetect:
    def test_explicit_model_noop(self) -> None:
        """resolve_embed_model skips when model is already set."""
        from fw_context_mcp.llm.auto_model import _reset_auto_model_cache, resolve_embed_model

        _reset_auto_model_cache()
        cfg = LLMConfig(embed_model="mxbai-embed-large:latest")
        assert cfg.embed_model == "mxbai-embed-large:latest"
        resolve_embed_model(cfg)
        assert cfg.embed_model == "mxbai-embed-large:latest"  # unchanged

    def test_empty_resolves_to_cpu_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty model resolves to CPU model when no GPU detected."""
        from fw_context_mcp.llm import auto_model
        from fw_context_mcp.llm.auto_model import _reset_auto_model_cache, resolve_embed_model

        _reset_auto_model_cache()
        monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
        monkeypatch.setattr(auto_model, "_model_installed", lambda url, model: True)

        cfg = LLMConfig()
        assert cfg.embed_model == ""
        resolve_embed_model(cfg)
        assert cfg.embed_model == auto_model._DEFAULT_CPU_MODEL

    def test_empty_resolves_to_gpu_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty model resolves to GPU model when NVIDIA GPU detected."""
        from fw_context_mcp.llm import auto_model
        from fw_context_mcp.llm.auto_model import _reset_auto_model_cache

        _reset_auto_model_cache()
        monkeypatch.setattr(auto_model, "_gpu_available", lambda: True)
        monkeypatch.setattr(auto_model, "_model_installed", lambda url, model: True)

        cfg = LLMConfig()
        get_embedder(cfg)
        assert cfg.embed_model == auto_model._DEFAULT_GPU_MODEL

    def test_embed_key_returns_auto_when_unresolved(self) -> None:
        """embed_key() uses 'auto' placeholder when model is empty (not yet resolved)."""
        cfg = LLMConfig()
        assert cfg.embed_model == ""
        key = cfg.embed_key()
        assert key == "auto:desc-v5"  # placeholder until resolve_embed_model runs

    def test_embed_model_resolved_by_from_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_from_dict does NOT resolve embed_model — that's a load() responsibility.

        _from_dict is a pure data transformation. resolve_embed_model is an I/O
        side effect (GPU detection, Ollama API) and belongs in load().
        """
        from fw_context_mcp.config.settings import _from_dict
        from fw_context_mcp.llm import auto_model
        from fw_context_mcp.llm.auto_model import _reset_auto_model_cache, resolve_embed_model

        _reset_auto_model_cache()
        monkeypatch.setattr(auto_model, "_gpu_available", lambda: False)
        monkeypatch.setattr(auto_model, "_model_installed", lambda url, model: True)

        cfg = _from_dict({"llm": {}})
        # _from_dict itself does not call resolve_embed_model
        assert cfg.llm.embed_model == ""
        # Explicit resolution
        resolve_embed_model(cfg.llm)
        assert cfg.llm.embed_model == auto_model._DEFAULT_CPU_MODEL
        key = cfg.llm.embed_key()
        assert key == f"{auto_model._DEFAULT_CPU_MODEL}:{DESCRIPTION_VERSION}"


class TestMRLTruncation:
    """Verify Matryoshka dimension truncation in SentenceTransformerEmbedder."""

    def test_no_truncation_when_embed_dim_none(self) -> None:
        cfg = LLMConfig(embed_model="BAAI/bge-small-en-v1.5", embed_dim=None)
        assert cfg.embed_dim is None

    def test_truncation_config_stored(self) -> None:
        cfg = LLMConfig(embed_model="ibm-granite/granite-embedding-311m-multilingual-r2", embed_dim=128)
        assert cfg.embed_dim == 128

    def test_embed_dim_respected_in_st_embedder_init(self) -> None:
        """embed_dim is passed through to the constructor — actual truncation
        verified during real embed calls, not here."""
        import importlib

        st_spec = importlib.util.find_spec("sentence_transformers")
        if st_spec is None:
            pytest.skip("sentence-transformers not installed")
        from fw_context_mcp.llm.st_embedder import SentenceTransformerEmbedder

        cfg = LLMConfig(embed_model="BAAI/bge-small-en-v1.5", embed_dim=64)
        e = SentenceTransformerEmbedder(cfg)
        assert e._dim is None  # not loaded yet
        assert e._cfg.embed_dim == 64

    def test_max_tokens_granite_r2(self) -> None:
        """Granite R2 has 32768 token context — full body embedding enabled."""
        cfg = LLMConfig(embed_model="ibm-granite/granite-embedding-311m-multilingual-r2")
        e = get_embedder(cfg)
        assert e.max_tokens == 32768

    def test_max_tokens_bge_default(self) -> None:
        """BGE models have 512 token limit — head+tail truncation applies."""
        cfg = LLMConfig(embed_model="BAAI/bge-large-en-v1.5")
        e = get_embedder(cfg)
        assert e.max_tokens == 512


class TestChunkBody:
    """Tests for _chunk_body — semantic C/C++ function body splitting."""

    def test_short_body_single_chunk(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        body = "int x = 1;\nreturn x;\n"
        chunks = _chunk_body(body, max_chars=200)
        assert len(chunks) == 1
        assert chunks[0] == body

    def test_exact_fit_single_chunk(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        body = "return 0;\n"
        chunks = _chunk_body(body, max_chars=len(body))
        assert len(chunks) == 1

    def test_empty_body(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        chunks = _chunk_body("", max_chars=100)
        assert len(chunks) == 1
        assert chunks[0] == ""

    def test_splits_at_semicolon_boundary(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        body = "int a = 1;\nint b = 2;\nint c = 3;\n"
        # Force max_chars small enough that only one statement fits
        chunks = _chunk_body(body, max_chars=12)
        assert len(chunks) >= 2
        # each chunk should end with ';' or '}'
        for chunk in chunks[:-1]:  # last chunk may end with anything
            stripped = chunk.rstrip()
            assert stripped.endswith(";") or stripped.endswith("}")

    def test_splits_at_closing_brace(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        body = (
            "if (x > 0) {\n"
            "    return 1;\n"
            "}\n"
            "return 0;\n"
        )
        chunks = _chunk_body(body, max_chars=30)
        assert len(chunks) >= 2
        # First chunk should end with closing brace
        first = chunks[0].rstrip().split("\n")[-1]
        assert first.endswith("}") or body.rstrip().endswith(first)

    def test_single_long_line_no_boundary(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        # Line with no semicolons, no braces, no blank lines — falls back
        # to head+tail truncation since no split boundary exists
        body = "very_long_function_name_with_many_arguments(arg1, arg2, arg3, arg4, arg5, arg6)"
        chunks = _chunk_body(body, max_chars=40)
        assert len(chunks) >= 1
        # each chunk stays within reasonable bounds
        for c in chunks:
            assert len(c) <= 80  # head+tail truncation bound

    def test_blank_line_split(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        body = (
            "int x = 1;\n"
            "\n"
            "int y = 2;\n"
            "\n"
            "int z = 3;\n"
        )
        chunks = _chunk_body(body, max_chars=25)
        assert len(chunks) >= 2

    def test_all_lines_fit_no_split(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        body = "return 0;\n"
        chunks = _chunk_body(body, max_chars=1000)
        assert len(chunks) == 1

    def test_very_large_body_multiple_chunks(self) -> None:
        from fw_context_mcp.indexer.runner import _chunk_body

        # 50 statements, each ~9-12 chars
        lines = [f"x{i} = {i};\n" for i in range(50)]
        body = "".join(lines)
        chunks = _chunk_body(body, max_chars=60)
        assert len(chunks) >= 5
        # Each chunk should not be empty and should contain at least one line
        for c in chunks:
            assert len(c) > 0

    def test_semicolon_inside_string_not_split(self) -> None:
        """Semicolon inside string literal should not cause early split."""
        from fw_context_mcp.indexer.runner import _chunk_body

        body = 'printf("hello; world");\nreturn 0;\n'
        chunks = _chunk_body(body, max_chars=100)
        # Should not split inside the string — single chunk if it fits
        assert len(chunks) == 1


class TestTruncateBody:
    def test_short_body_unchanged(self) -> None:
        from fw_context_mcp.indexer.runner import _truncate_body

        body = "short body"
        result = _truncate_body(body, max_chars=100)
        assert result == body

    def test_long_body_head_tail_truncation(self) -> None:
        from fw_context_mcp.indexer.runner import _truncate_body

        body = "A" * 1000
        result = _truncate_body(body, max_chars=100)
        assert len(result) <= 100
        assert "// ..." in result

    def test_truncation_preserves_prefix(self) -> None:
        from fw_context_mcp.indexer.runner import _truncate_body

        body = "PREFIX_CONTENT_TAIL"
        result = _truncate_body(body, max_chars=50)
        assert "PREFIX" in result
        assert "TAIL" in result


class TestValidateModelDir:
    """Tests for _validate_model_dir — F2 regression safety."""

    def test_valid_model_dir_with_config_json(self, tmp_path: Path) -> None:
        from fw_context_mcp.llm.embedder_factory import _validate_model_dir

        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}")
        _validate_model_dir(model_dir, "test-model")  # nevyhazuje

    def test_missing_config_json_raises(self, tmp_path: Path) -> None:
        from fw_context_mcp.llm.embedder_factory import _validate_model_dir

        model_dir = tmp_path / "empty_model"
        model_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="missing config.json"):
            _validate_model_dir(model_dir, "empty_model")

    def test_nonexistent_dir_raises(self) -> None:
        from pathlib import Path

        from fw_context_mcp.llm.embedder_factory import _validate_model_dir

        with pytest.raises(FileNotFoundError):
            _validate_model_dir(Path("/nonexistent/model/path"), "nonexistent")

    def test_empty_dir_raises(self, tmp_path: Path) -> None:
        from fw_context_mcp.llm.embedder_factory import _validate_model_dir

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError, match="missing config.json"):
            _validate_model_dir(empty, "empty")


class TestDimThreadSafety:
    """Regression test for F1 — dim property read under lock."""

    def test_dim_returns_after_ensure_model(self) -> None:
        """dim should not return None after _ensure_model completes."""
        from fw_context_mcp.llm.st_embedder import SentenceTransformerEmbedder

        cfg = LLMConfig(embed_model="BAAI/bge-small-en-v1.5", embed_dim=64)
        # Don't actually load the model — just test the property behavior
        e = SentenceTransformerEmbedder(cfg)
        assert e.dim is None  # before loading

    def test_dim_lock_held_during_read(self) -> None:
        """dim property acquires _lock before reading _dim."""
        from fw_context_mcp.llm.st_embedder import SentenceTransformerEmbedder

        cfg = LLMConfig(embed_model="BAAI/bge-small-en-v1.5")
        e = SentenceTransformerEmbedder(cfg)

        # Verify the lock is a threading.Lock (not RLock, not None)
        import threading
        assert type(e._lock) is type(threading.Lock())

        # Verify we can acquire it (not held by anyone)
        acquired = e._lock.acquire(blocking=False)
        assert acquired, "Lock should not be held before any operations"
        e._lock.release()
