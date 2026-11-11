"""An index run with an Ollama older than the minimum of a use makes nothing of that use.

Ollama 0.30.10 on macOS stopped its runner on each embedding input of more
than 2048 tokens, after part of the embeddings (measured 2026-11-11).
Before, only `fw-context doctor` named an old server, and the run failed
in the middle of the step.  The steps now check the version first.  The
crash occurs only in the embedding mode of the runner, thus the LLM
analysis has its own, older minimum (``OllamaUse``).
"""

from __future__ import annotations

import httpx
import pytest

import fw_context_mcp.net as net
from fw_context_mcp.config.settings import LLMConfig
from fw_context_mcp.indexer import _embedding, _llm_analysis
from fw_context_mcp.llm import ollama
from fw_context_mcp.llm.ollama import OllamaClient, OllamaEmbedder


class _Reached(Exception):
    """The step went past the checks of the server: the first property of the embedder."""


def _server(monkeypatch, version: str | None) -> None:
    """An Ollama that answers /api/tags, and gives *version* at /api/version (None: HTTP 404)."""

    def get(url, **_):
        request = httpx.Request("GET", url)
        if url.endswith("/api/version"):
            if version is None:
                return httpx.Response(404, json={"error": "not found"}, request=request)
            return httpx.Response(200, json={"version": version}, request=request)
        if url.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "qwen3-embedding:0.6b"}]}, request=request)
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(net, "get", get)


def _embedder(monkeypatch) -> list[str]:
    """Give an Ollama embedder whose first property raises _Reached; return the list of released models."""
    released: list[str] = []
    monkeypatch.setattr(ollama, "release_other_model", lambda cfg, role: released.append(role))

    class _Embedder(OllamaEmbedder):
        @property
        def name(self) -> str:
            raise _Reached

    monkeypatch.setattr(_embedding, "get_embedder", lambda cfg: _Embedder(cfg))
    return released


_CFG = LLMConfig(embed_model="qwen3-embedding:0.6b")


@pytest.mark.parametrize("symbol_ids", [None, [1, 2]], ids=["index run", "reindex_file"])
def test_the_embeddings_stop_before_the_first_batch_with_an_old_ollama(tmp_path, monkeypatch, symbol_ids):
    _server(monkeypatch, "0.35.1")
    released = _embedder(monkeypatch)

    with pytest.raises(_embedding.EmbeddingsIncomplete, match=r"older than 0\.40\.15.*no embeddings were written"):
        _embedding._build_embeddings(None, "h", _CFG, tmp_path, symbol_ids=symbol_ids)
    assert released == [], "a step that sends no embedding request keeps the chat model loaded"


@pytest.mark.parametrize("version", ["0.40.15", None, "0.0.0"], ids=["minimum", "unknown", "source build"])
def test_the_embeddings_continue_with_an_applicable_or_unknown_version(tmp_path, monkeypatch, version):
    """An unknown version is not known to be old: the step continues to its first request."""
    _server(monkeypatch, version)
    released = _embedder(monkeypatch)

    with pytest.raises(_Reached):
        _embedding._build_embeddings(None, "h", _CFG, tmp_path)
    assert released == ["embeddings"]


def _client(monkeypatch, trained: int) -> OllamaClient:
    client = OllamaClient.__new__(OllamaClient)
    client.server_url = "http://localhost:11434"
    monkeypatch.setattr(OllamaClient, "trained_context", lambda self: trained)
    return client


def test_the_analysis_makes_nothing_with_an_old_ollama(monkeypatch, caplog):
    _server(monkeypatch, "0.30.0")

    assert _llm_analysis._analysis_contexts(_client(monkeypatch, 32768), LLMConfig(num_ctx=16384)) is None
    assert "older than 0.30.1" in caplog.text


@pytest.mark.parametrize("version", ["0.30.1", "0.35.1"])
def test_the_analysis_runs_with_a_server_below_the_embeddings_minimum(monkeypatch, version):
    """The runner crash of 0.30.x is in the embedding mode only: it does not stop the analysis."""
    _server(monkeypatch, version)

    assert _llm_analysis._analysis_contexts(_client(monkeypatch, 32768), LLMConfig(num_ctx=16384)) == (16384, 32768)
