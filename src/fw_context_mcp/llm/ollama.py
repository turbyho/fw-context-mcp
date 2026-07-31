"""Thin Ollama client for fw-context LLM tools."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config.settings import LLMConfig
from .embedder import Embedder

log = logging.getLogger(__name__)

# Cache for Ollama model context lengths — populated lazily by OllamaEmbedder.max_tokens
_max_tokens_cache: dict[str, int] = {}

# Per-process semaphore controlling concurrent Ollama HTTP calls.
# Multiple concurrent requests to a single Ollama instance can cause
# timeouts, OOM, or hangs when the GPU is saturated — this semaphore
# caps in-flight Ollama requests per process.
#
# Default value 1 = serial (identical to a Lock).  Bump to 2–4 for
# multi-client transports (SSE/streamable) where embedding requests
# (small model, ~100 ms) can overlap without saturating the GPU.
#
# httpx.Client IS thread-safe — this semaphore is about Ollama server
# capacity, not about httpx correctness.
_ollama_sem: threading.BoundedSemaphore = threading.BoundedSemaphore(1)
_sem_value: int = 1


def _reconfigure_ollama_sem(max_concurrent: int) -> None:
    """Replace the semaphore when *max_concurrent* changes.

    Called from ``OllamaEmbedder.__init__`` when config provides a
    non-default ``ollama_max_concurrent``.  Idempotent — a semaphore
    with the same value is not recreated.
    """
    global _ollama_sem, _sem_value
    if max_concurrent < 1:
        max_concurrent = 1
    if max_concurrent != _sem_value:
        _ollama_sem = threading.BoundedSemaphore(max_concurrent)
        _sem_value = max_concurrent


@contextmanager
def ollama_guard() -> Generator[None, None, None]:
    """Context manager capping concurrent Ollama requests within this process.

    All ``call_ollama`` and ``call_ollama_embed`` calls should be wrapped
    in this guard.  Cross-process serialization is handled indirectly
    through the write lock (``db.write_lock``) — only the process that
    holds the write lock can perform LLM analysis writes.
    """
    with _ollama_sem:
        yield


def _pull_model(model: str, base_url: str) -> None:
    """Pull a model via Ollama /api/pull (blocking, streaming).

    Downloads the model if not already installed.  The streaming response
    is consumed to avoid leaving a dangling connection.
    """
    url = base_url.rstrip("/") + "/api/pull"
    try:
        with httpx.stream("POST", url, json={"name": model}, timeout=600.0) as resp:
            resp.raise_for_status()
            for _ in resp.iter_lines():
                pass  # consume stream
    except httpx.HTTPStatusError as e:
        raise OllamaError(
            f"Failed to pull model '{model}': HTTP {e.response.status_code}"
        ) from e
    except Exception as e:
        raise OllamaError(f"Failed to pull model '{model}': {e}") from e


def _write_debug_log(path: Path, entry: dict) -> None:
    """Append a JSON line to the LLM debug log file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("LLM debug log write failed: %s", e)


def call_ollama(
    prompt: str,
    cfg: LLMConfig,
    *,
    temperature: float = 0.0,
    num_predict: int | None = None,
) -> str:
    """Send a prompt to Ollama and return the response text.

    Args:
        prompt: The full prompt text to send.
        cfg: LLM configuration.
        temperature: Sampling temperature (0.0 = deterministic, default).
        num_predict: Maximum tokens to generate (None = model default).

    Raises OllamaError on network or API failure.
    """
    if not cfg.ollama_url.startswith(("http://", "https://")):
        raise OllamaError(f"Invalid ollama_url scheme: {cfg.ollama_url} — must be http:// or https://")
    url = cfg.ollama_url.rstrip("/") + "/api/generate"
    options: dict = {"num_ctx": cfg.num_ctx, "temperature": temperature}
    if num_predict is not None:
        options["num_predict"] = num_predict
    payload: dict = {
        "model": cfg.model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": cfg.keep_alive,
        "options": options,
    }
    t0 = time.monotonic()
    try:
        with ollama_guard():
            resp = httpx.post(url, json=payload, timeout=cfg.timeout)
        if resp.status_code == 404:
            log.info("LLM model '%s' not found, pulling...", cfg.model)
            _pull_model(cfg.model, cfg.ollama_url)
            with ollama_guard():
                resp = httpx.post(url, json=payload, timeout=cfg.timeout)
        resp.raise_for_status()
        response_text = resp.json()["response"]
        if cfg.debug_log:
            _write_debug_log(cfg.debug_log, {
                "ts": datetime.now(UTC).isoformat(),
                "model": cfg.model,
                "num_ctx": cfg.num_ctx,
                "latency_s": round(time.monotonic() - t0, 2),
                "prompt": prompt,
                "response": response_text,
            })
        return response_text
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {cfg.ollama_url}. "
            "Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError(f"Ollama request timed out after {cfg.timeout:.0f}s") from None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise OllamaModelNotFoundError(cfg.model, cfg.ollama_url) from e
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except OllamaError:
        raise  # Pass through — prevents wrapping by the broad except Exception below
    except Exception as e:
        raise OllamaError(str(e)) from e


def _call_ollama_embed_impl(
    inputs: list[str], cfg: LLMConfig, *, query: bool = True, embedder: OllamaEmbedder | None = None
) -> list[list[float]]:
    """Generate embeddings for a batch of texts via Ollama.

    Internal implementation shared by ``OllamaEmbedder`` and the
    backward-compat ``call_ollama_embed`` wrapper.

    When *embedder* is passed, :attr:`OllamaEmbedder.dim` is updated after
    the first successful batch so the caller can detect dimensions lazily.
    """
    if not cfg.ollama_url.startswith(("http://", "https://")):
        raise OllamaError(f"Invalid ollama_url scheme: {cfg.ollama_url} — must be http:// or https://")
    url = cfg.ollama_url.rstrip("/") + "/api/embed"
    prompt = cfg.embed_query_prompt if query else cfg.embed_doc_prompt
    if prompt:
        inputs = [prompt + inp for inp in inputs]
    payload = {
        "model": cfg.embed_model,
        "input": inputs,
        "keep_alive": cfg.keep_alive,
    }
    t0 = time.monotonic()
    try:
        with ollama_guard():
            resp = httpx.post(url, json=payload, timeout=cfg.timeout * 2)
        resp.raise_for_status()
        embeddings = resp.json()["embeddings"]
        if embedder is not None:
            embedder._update_dim(embeddings)
        if cfg.debug_log:
            _write_debug_log(cfg.debug_log, {
                "ts": datetime.now(UTC).isoformat(),
                "model": cfg.embed_model,
                "latency_s": round(time.monotonic() - t0, 2),
                "num_inputs": len(inputs),
                "embedding_dims": [len(e) for e in embeddings],
            })
        return embeddings
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            # Model not installed — pull it, then retry
            log.info("Embedding model '%s' not found, pulling...", cfg.embed_model)
            _pull_model(cfg.embed_model, cfg.ollama_url)
            with ollama_guard():
                resp = httpx.post(url, json=payload, timeout=cfg.timeout * 2)
            resp.raise_for_status()
            embeddings = resp.json()["embeddings"]
            if embedder is not None:
                embedder._update_dim(embeddings)
            if cfg.debug_log:
                _write_debug_log(cfg.debug_log, {
                    "ts": datetime.now(UTC).isoformat(),
                    "model": cfg.embed_model,
                    "latency_s": round(time.monotonic() - t0, 2),
                    "num_inputs": len(inputs),
                    "embedding_dims": [len(e) for e in embeddings],
                    "note": "pulled model first",
                })
            return embeddings
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {cfg.ollama_url}. "
            "Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError("Ollama embed request timed out") from None
    except OllamaError:
        raise
    except Exception as e:
        raise OllamaError(str(e)) from e


def call_ollama_embed(inputs: list[str], cfg: LLMConfig, *, query: bool = True) -> list[list[float]]:
    """Backward-compatible wrapper — delegates to ``_call_ollama_embed_impl``.

    Kept for ``experiments/`` scripts that import this directly.
    New code should use :class:`OllamaEmbedder` via :func:`get_embedder`.
    """
    return _call_ollama_embed_impl(inputs, cfg, query=query)


class OllamaEmbedder(Embedder):
    """Embedding backend that delegates to Ollama's HTTP API.

    Thin wrapper around ``call_ollama_embed`` that satisfies the ``Embedder``
    interface.  The standalone ``call_ollama_embed`` function is kept for
    backward compatibility with ``experiments/`` scripts — it simply
    forwards to :meth:`embed_documents` / :meth:`embed_queries`.
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._dim: int | None = None
        if cfg.ollama_max_concurrent > 1:
            _reconfigure_ollama_sem(cfg.ollama_max_concurrent)

    def _update_dim(self, embeddings: list[list[float]]) -> None:
        """Lazily detect embedding dimension from the first batch."""
        if self._dim is None and embeddings:
            self._dim = len(embeddings[0])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return _call_ollama_embed_impl(texts, self._cfg, query=False, embedder=self)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return _call_ollama_embed_impl(texts, self._cfg, query=True, embedder=self)

    @property
    def name(self) -> str:
        return self._cfg.embed_model

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def max_tokens(self) -> int:
        model = self._cfg.embed_model
        cached = _max_tokens_cache.get(model)
        if cached is not None:
            return cached
        try:
            resp = httpx.get(
                f"{self._cfg.ollama_url.rstrip('/')}/api/show",
                params={"name": model}, timeout=5.0,
            )
            if resp.status_code == 200:
                info = resp.json().get("model_info", {})
                ctx_len = info.get("context_length")
                if isinstance(ctx_len, int) and ctx_len > 0:
                    _max_tokens_cache[model] = ctx_len
                    return ctx_len
        except Exception:
            pass
        model_lower = model.lower()
        if "qwen3-embedding:0.6b" in model_lower or "qwen3-embedding:4b" in model_lower:
            tokens = 32768
        elif "qwen3-embedding:8b" in model_lower:
            tokens = 8192
        elif "mxbai" in model_lower:
            tokens = 512
        else:
            tokens = 512
        _max_tokens_cache[model] = tokens
        return tokens


async def call_ollama_async(
    prompt: str,
    cfg: LLMConfig,
    *,
    temperature: float = 0.0,
    num_predict: int | None = None,
) -> str:
    """Async wrapper for ``call_ollama`` — offloads blocking httpx to a thread.

    Use from async MCP tool handlers to avoid blocking the event loop during
    long-running Ollama requests (10-60 s).

    Args:
        prompt: The full prompt string to send to the chat model.
        cfg: LLM configuration (ollama_url, model, num_ctx, debug_log).
        temperature: Sampling temperature (0.0 = deterministic, default).
        num_predict: Maximum tokens to generate (None = model default).

    Returns:
        The model's text response.
    """
    return await asyncio.to_thread(
        call_ollama, prompt, cfg, temperature=temperature, num_predict=num_predict
    )


class OllamaError(RuntimeError):
    """Base exception for Ollama API and network failures."""


class OllamaModelNotFoundError(OllamaError):
    """Raised when the requested model is not installed in Ollama.

    The error message includes the exact ``ollama pull`` command to run.
    """

    def __init__(self, model: str, url: str) -> None:
        self.model = model
        super().__init__(
            f"Model '{model}' not found in Ollama at {url}. "
            f"Run: ollama pull {model}"
        )

from ._diag import check_setup

