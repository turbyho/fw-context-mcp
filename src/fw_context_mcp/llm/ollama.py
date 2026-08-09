"""Ollama client for fw-context LLM tools — chat and embedding over HTTP.

Design decisions
================

Single-process serialization via BoundedSemaphore
    Ollama runs a single inference engine (llama.cpp under the hood).
    Two simultaneous /api/generate requests to the same model can
    trigger OOM on GPU-poor machines or produce garbled output when
    the KV cache is shared.  The per-process semaphore caps in-flight
    Ollama HTTP calls so each request completes before the next one
    starts.  The default value (1) makes this identical to a Lock.

    Multi-client transports (SSE streaming) may tolerate 2-4 concurrent
    requests — embed requests (small model, ~100ms) rarely interfere
    with generation requests.  The semaphore is reconfigurable via
    ``ollama_max_concurrent`` in config.

Cross-process write serialization
    Multiple MCP workers can call Ollama in parallel (different
    processes).  The semaphore only serializes within one process.
    Cross-process ordering is enforced at a higher level by the
    database write lock (``db.write_lock``) — only the process that
    holds the write lock may perform LLM analysis writes, so two
    workers never race on generation→write for the same symbol.

Plan A / Plan B streaming
    Ollama sometimes ignores ``stream: false`` and returns SSE anyway
    (observed with certain model+Ollama version combos).  Two plans:

    * **Plan A** (auto-detect): request ``stream: false``.  If the
      response Content-Type is SSE, parse the already-complete body
      as SSE lines — no extra HTTP round-trip, no config needed.
    * **Plan B** (active streaming): request ``stream: true`` and
      consume SSE chunks via ``httpx.stream()``.  Keeps the connection
      alive during long generations — avoids reverse-proxy idle
      timeouts (nginx 60s, Cloudflare 100s).  Used when the operator
      sets ``cfg.stream = True``.

Auto-pull on 404
    When ``cfg.auto_pull`` is True and Ollama returns HTTP 404, the
    model is pulled before the request is retried.  Auto-pull is
    disabled by default (``auto_pull=False``) for intranet deployments
    where outbound bandwidth is limited or models require approval.

    Pull detection uses stall-based timeouts (not absolute) — a slow
    download is not a failure; only zero progress triggers a timeout.

Embedding prompt prepending
    ``embed_query_prompt`` and ``embed_doc_prompt`` are prepended to
    input texts before embedding.  Some models (e.g. E5-style) use
    these for asymmetric encoding — a query prefix triggers different
    internal behaviour than a document prefix.  The Ollama API sends
    input as-is; the prompt prefix is client-side prepending.

Module-level state
    ``_max_tokens_cache`` — model → context-length map, built lazily.
    Avoids an HTTP GET per ``max_tokens`` access (called frequently
    during truncation decisions).

    ``_ollama_sem`` — per-process BoundedSemaphore.  See Design
    decisions above.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx

from ..config.settings import LLMConfig
from ._debug import _write_debug_log
from .embedder import Embedder

log = logging.getLogger(__name__)

# Cache for Ollama model context lengths — populated lazily by OllamaEmbedder.max_tokens.
# Module-level (not instance-level) because context length depends only on the
# model name, not on the config or embedder instance.  Avoids an HTTP GET
# to /api/show every time max_tokens is accessed during batch embedding.
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
#
# BoundedSemaphore (not Semaphore) — raises ValueError on release()
# without acquire(), catching double-release bugs early.
_ollama_sem: threading.BoundedSemaphore = threading.BoundedSemaphore(1)
_sem_value: int = 1
# Protects _ollama_sem reassignment — reconfiguring while threads
# are acquiring the old semaphore would lose the cap.
_reconfigure_lock = threading.Lock()


def _reconfigure_ollama_sem(max_concurrent: int) -> None:
    """Replace the semaphore when *max_concurrent* changes.

    Called from ``OllamaEmbedder.__init__`` when config provides a
    non-default ``ollama_max_concurrent``.  Idempotent — a semaphore
    with the same value is not recreated.

    WHY idempotent: Embedder instances are created once per config load.
    On config reload the existing semaphore keeps working; recreating
    needlessly would risk losing in-flight acquires.
    """
    global _ollama_sem, _sem_value
    if max_concurrent < 1:
        max_concurrent = 1
    with _reconfigure_lock:
        if max_concurrent > _sem_value:
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


def _pull_model(
    model: str,
    base_url: str,
    *,
    stall_timeout: float = 120.0,
) -> None:
    """Pull a model via Ollama /api/pull with stall detection.

    Downloads the model, tracking progress from the streaming JSON
    response.  Only times out when NO progress is made for
    *stall_timeout* seconds — slow connections and large models
    are handled gracefully.

    WHY stall-based timeout (not absolute): Model downloads range from
    0.5 GB (qwen3-embedding:0.6b) to >20 GB (deepseek-coder-v2).
    A fixed timeout would be too short for large models or too long for
    small ones.  Stall detection handles both: it waits as long as bytes
    keep arriving, only failing when the connection truly hangs.
    """

    url = base_url.rstrip("/") + "/api/pull"
    last_progress = time.monotonic()
    # Track per-digest progress — Ollama streams progress per BLOB digest,
    # not as a single cumulative counter.  A new digest resets completed=0.
    digest_states: dict[str, int] = {}  # digest → last completed bytes

    try:
        with httpx.stream(
            "POST", url, json={"name": model},
            timeout=httpx.Timeout(
                connect=30.0,
                read=stall_timeout,   # per-read timeout = stall detection
                write=30.0,
                pool=30.0,
            ),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue

                status = entry.get("status", "")
                digest = entry.get("digest", "")
                completed = entry.get("completed", 0)

                if status == "downloading" and digest:
                    prev = digest_states.get(digest, -1)
                    if completed > prev:
                        digest_states[digest] = completed
                        last_progress = time.monotonic()

                elif status in ("pulling manifest", "verifying sha256 digest",
                                "writing manifest", "removing unused layers"):
                    last_progress = time.monotonic()

                elif status == "success":
                    break

                # Stall check
                if time.monotonic() - last_progress > stall_timeout:
                    raise OllamaError(
                        f"Pull stalled for {model}: no progress for "
                        f"{stall_timeout:.0f}s"
                    )
    except httpx.HTTPStatusError as e:
        raise OllamaError(f"Failed to pull model '{model}': HTTP {e.response.status_code}") from e
    except Exception as e:
        raise OllamaError(f"Failed to pull model '{model}': {e}") from e


def _accumulate_ollama_sse(resp: httpx.Response) -> str:
    """Parse a completed Ollama streaming response body and extract content.

    Ollama streaming format: each line is a complete JSON object with
    ``{"response": "chunk", "done": false}``.  The final line has
    ``"done": true`` and an empty or absent ``response`` field.

    This handles Plan A for the Ollama-native path: when the API returns
    a streaming response despite ``stream: false`` being requested.
    """
    chunks: list[str] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            chunk = obj.get("response", "")
            if chunk:
                chunks.append(chunk)
        except json.JSONDecodeError:
            continue
    if not chunks:
        raise OllamaError("Ollama SSE stream contained no content")
    return "".join(chunks)


def _stream_ollama_generate(
    url: str,
    payload: dict,
    cfg: LLMConfig,
    prompt: str,
    base_url: str,
    t0: float,
) -> str:
    """Send a streaming Ollama /api/generate request and accumulate response.

    Uses ``httpx.stream()`` to consume newline-delimited JSON chunks as they
    arrive (Plan B for the Ollama-native path).  This keeps the HTTP connection
    alive with continuous data flow, avoiding reverse-proxy idle timeouts
    when using a remote Ollama instance behind a proxy.

    Args:
        url: Full Ollama /api/generate URL.
        payload: Request payload (stream will be set to True).
        cfg: LLM configuration.
        prompt: Original prompt text (for debug logging).
        base_url: Ollama base URL (for error messages).
        t0: Start time (monotonic) for latency logging.

    Returns:
        The accumulated text response.

    Raises:
        OllamaError / OllamaModelNotFoundError on failure.
    """
    payload["stream"] = True
    chunks: list[str] = []
    try:
        with ollama_guard():
            with httpx.stream("POST", url, json=payload, timeout=cfg.timeout) as resp:
                if resp.status_code == 404:
                    if cfg.auto_pull:
                        log.info("LLM model '%s' not found, pulling...", cfg.model)
                        _pull_model(cfg.model, base_url)
                        # Retry with streaming
                        with httpx.stream("POST", url, json=payload, timeout=cfg.timeout) as resp2:
                            resp2.raise_for_status()
                            for line in resp2.iter_lines():
                                _extract_ollama_chunk(line, chunks)
                    else:
                        raise OllamaModelNotFoundError(cfg.model, base_url) from None
                else:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        _extract_ollama_chunk(line, chunks)
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {base_url}. Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError(f"Ollama request timed out after {cfg.timeout:.0f}s") from None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise OllamaModelNotFoundError(cfg.model, base_url) from e
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except OllamaError:
        raise
    except Exception as e:
        raise OllamaError(str(e)) from e

    if not chunks:
        raise OllamaError("Streaming Ollama request returned no content")

    response_text = "".join(chunks)
    if cfg.debug_log:
        _write_debug_log(
            cfg.debug_log,
            {
                "ts": datetime.now(UTC).isoformat(),
                "model": cfg.model,
                "num_ctx": cfg.num_ctx,
                "latency_s": round(time.monotonic() - t0, 2),
                "prompt": prompt,
                "response": response_text,
                "streamed": True,
            },
        )
    return response_text


def _extract_ollama_chunk(line: str | None, chunks: list[str]) -> None:
    """Extract a content chunk from an Ollama streaming JSON line."""
    if not line:
        return
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
        chunk = obj.get("response", "")
        if chunk:
            chunks.append(chunk)
    except json.JSONDecodeError:
        pass


def call_ollama(
    prompt: str,
    cfg: LLMConfig,
    *,
    temperature: float = 0.0,
    num_predict: int | None = None,
) -> str:
    """Send a prompt to the configured chat backend and return the response text.

    When ``cfg.chat_api_base`` is set, the call is routed to an external
    endpoint (OpenAI-compatible or Ollama-native, auto-detected from the URL).
    When ``cfg.chat_api_base`` is None, the local Ollama ``/api/generate``
    endpoint is used (original behaviour).

    Streaming behavior (when ``cfg.stream`` is True):

    * OpenAI-compatible path: delegates to ``call_openai_chat`` which uses
      ``httpx.stream()`` with SSE chunks (Plan B).
    * Ollama-native path (local or remote): uses ``httpx.stream()`` with
      newline-delimited JSON chunks (Plan B).

    When ``cfg.stream`` is False (default): sends ``stream: false``.  If the
    API returns a streaming response anyway, it is auto-detected and parsed
    transparently (Plan A fallback).

    WHY two separate paths: The OpenAI-compatible endpoint uses
    ``/chat/completions`` with a messages array; the Ollama-native endpoint
    uses ``/api/generate`` with a raw prompt string.  These are different
    HTTP payload formats — auto-detected from the URL so the operator
    doesn't need to know which one to use.  Embedding always goes to
    local Ollama (``/api/embed``) regardless of chat routing.

    Args:
        prompt: The full prompt text to send.
        cfg: LLM configuration.
        temperature: Sampling temperature (0.0 = deterministic, default).
        num_predict: Maximum tokens to generate (None = model default).

    Raises OllamaError on network or API failure.
    """
    if cfg.chat_api_base:
        from .chat_router import call_openai_chat, detect_chat_endpoint

        fmt, endpoint = detect_chat_endpoint(cfg)
        if fmt == "openai":
            return call_openai_chat(
                prompt,
                cfg,
                endpoint,
                temperature=temperature,
                num_predict=num_predict,
            )
        # fmt == "ollama": use the detected endpoint (may be a remote Ollama)
        url = endpoint
        base_url = cfg.chat_api_base
    else:
        url = cfg.ollama_url.rstrip("/") + "/api/generate"
        base_url = cfg.ollama_url

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

    # Plan B: active streaming for the Ollama-native path
    if cfg.stream:
        t0 = time.monotonic()
        return _stream_ollama_generate(url, payload, cfg, prompt, base_url, t0)

    # Non-streaming request (Plan A: SSE auto-detection below)
    t0 = time.monotonic()
    try:
        with ollama_guard():
            resp = httpx.post(url, json=payload, timeout=cfg.timeout)
        if resp.status_code == 404:
            if cfg.auto_pull:
                log.info("LLM model '%s' not found, pulling...", cfg.model)
                _pull_model(cfg.model, base_url)
                with ollama_guard():
                    resp = httpx.post(url, json=payload, timeout=cfg.timeout)
            else:
                raise OllamaModelNotFoundError(cfg.model, base_url) from None
        resp.raise_for_status()

        # Plan A: detect streaming response even when stream:false was requested
        ct = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in ct or "application/x-ndjson" in ct:
            log.debug("Ollama returned streaming response despite stream:false — parsing (Plan A fallback)")
            response_text = _accumulate_ollama_sse(resp)
        else:
            response_text = resp.json()["response"]

        if cfg.debug_log:
            _write_debug_log(
                cfg.debug_log,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "model": cfg.model,
                    "num_ctx": cfg.num_ctx,
                    "latency_s": round(time.monotonic() - t0, 2),
                    "prompt": prompt,
                    "response": response_text,
                },
            )
        return response_text
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {base_url}. Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError(f"Ollama request timed out after {cfg.timeout:.0f}s") from None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise OllamaModelNotFoundError(cfg.model, base_url) from e
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except (httpx.HTTPError, OSError, KeyError, json.JSONDecodeError, ValueError) as e:
        raise OllamaError(str(e)) from e


def _call_ollama_embed_impl(
    inputs: list[str], cfg: LLMConfig, *, query: bool = True, embedder: OllamaEmbedder | None = None
) -> list[list[float]]:
    """Generate embeddings for a batch of texts via Ollama.

    Internal implementation shared by ``OllamaEmbedder`` and the
    backward-compat ``call_ollama_embed`` wrapper.

    When *embedder* is passed, :attr:`OllamaEmbedder.dim` is updated after
    the first successful batch so the caller can detect dimensions lazily.

    When ``cfg.embed_query_prompt`` (query=True) or ``cfg.embed_doc_prompt``
    (query=False) is non-empty, the instruction is prepended to each input
    text before sending to Ollama.

    Args:
        inputs: List of text strings to embed.
        cfg: LLM configuration (ollama_url, embed_model, debug_log,
            embed_query_prompt, embed_doc_prompt).
        query: When True (default), prepend ``embed_query_prompt``.
            When False, prepend ``embed_doc_prompt`` (used during indexing).

    Returns:
        List of embedding vectors, each a list of floats (dimension varies
        by model — 1024 for mxbai-embed-large, 4096 for qwen3-embedding).

    Raises:
        OllamaError: On network failure. When ``cfg.auto_pull`` is True,
            auto-pulls the model on HTTP 404 then retries once.
    """
    if not cfg.ollama_url.startswith(("http://", "https://")):
        raise OllamaError(f"Invalid ollama_url scheme: {cfg.ollama_url} — must be http:// or https://")
    url = cfg.ollama_url.rstrip("/") + "/api/embed"
    # Prepend instruction prompt for asymmetric embedding models (e.g. E5-style).
    # Query: "Represent this sentence for searching relevant passages: "
    # Document: "" (empty — document encoding expects no prefix for some models)
    # The prompts are configured per-model in settings.py defaults.
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
            _write_debug_log(
                cfg.debug_log,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "model": cfg.embed_model,
                    "latency_s": round(time.monotonic() - t0, 2),
                    "num_inputs": len(inputs),
                    "embedding_dims": [len(e) for e in embeddings],
                },
            )
        return embeddings
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            if cfg.auto_pull:
                log.info("Embedding model '%s' not found, pulling...", cfg.embed_model)
                _pull_model(cfg.embed_model, cfg.ollama_url)
                try:
                    with ollama_guard():
                        resp = httpx.post(url, json=payload, timeout=cfg.timeout * 2)
                    resp.raise_for_status()
                    embeddings = resp.json()["embeddings"]
                    if embedder is not None:
                        embedder._update_dim(embeddings)
                    if cfg.debug_log:
                        _write_debug_log(
                            cfg.debug_log,
                            {
                                "ts": datetime.now(UTC).isoformat(),
                                "model": cfg.embed_model,
                                "latency_s": round(time.monotonic() - t0, 2),
                                "num_inputs": len(inputs),
                                "embedding_dims": [len(e) for e in embeddings],
                                "note": "pulled model first",
                            },
                        )
                    return embeddings
                except httpx.HTTPStatusError as e2:
                    if e2.response.status_code == 404:
                        raise OllamaModelNotFoundError(cfg.embed_model, cfg.ollama_url) from e2
                    raise OllamaError(f"Ollama HTTP {e2.response.status_code}: {e2.response.text[:200]}") from e2
                except (httpx.HTTPError, OSError, KeyError, json.JSONDecodeError, ValueError) as e2:
                    raise OllamaError(str(e2)) from e2
            raise OllamaModelNotFoundError(cfg.embed_model, cfg.ollama_url) from e
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {cfg.ollama_url}. Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError("Ollama embed request timed out") from None
    except (httpx.HTTPError, OSError, KeyError, json.JSONDecodeError, ValueError) as e:
        raise OllamaError(str(e)) from e


def call_ollama_embed(inputs: list[str], cfg: LLMConfig, *, query: bool = True) -> list[list[float]]:
    """Backward-compatible wrapper — delegates to ``_call_ollama_embed_impl``.

    Kept for ``experiments/`` scripts that import this directly.
    New code should use :class:`OllamaEmbedder` via :func:`get_embedder`.
    """
    from .auto_model import resolve_embed_model
    resolve_embed_model(cfg)
    return _call_ollama_embed_impl(inputs, cfg, query=query)


class OllamaEmbedder(Embedder):
    """Embedding backend that delegates to Ollama's HTTP API.

    Thin wrapper around :func:`_call_ollama_embed_impl` that satisfies the
    ``Embedder`` interface.
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
        except (ValueError, TypeError, OSError, json.JSONDecodeError, httpx.HTTPError):
            pass
        model_lower = model.lower()
        if "qwen3-embedding" in model_lower:
            tokens = 32768
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
    return await asyncio.to_thread(call_ollama, prompt, cfg, temperature=temperature, num_predict=num_predict)


def _check_model_installed(model: str, installed: list[str]) -> bool:
    """True if *model* is in *installed* (exact or prefix match)."""
    if model in installed:
        return True
    return any(m.startswith(model.split(":")[0]) for m in installed)


def _parse_parameter_size(size_str: str) -> float:
    """Parse Ollama parameter_size string (e.g. '7.6B', '13B', '70B') to float billions.

    Returns 0.0 for unparseable strings.
    """
    m = re.match(r"(\d+\.?\d*)B", size_str.strip())
    return float(m.group(1)) if m else 0.0



def check_setup(cfg: LLMConfig) -> dict:
    """Check LLM backend connectivity and model availability.

    Reports on both the chat API (Ollama native or external) and the
    embedding API (always Ollama).  When ``chat_api_base`` points to an
    external host, a compliance warning is included.

    Args:
        cfg: LLM configuration.

    Returns:
        dict with keys varying by status:

        ``status`` values:
            - ``"ok"`` — chat and embedding backends ready.
            - ``"disabled"`` — ``cfg.enabled`` is False.
            - ``"not_configured"`` — Ollama not running and no chat_api_base.
            - ``"embedding_unavailable"`` — Ollama down but chat on external API.
            - ``"model_missing"`` — Ollama running but chat model not installed.
            - ``"error"`` — unexpected error probing Ollama.

        Common keys: ``chat_api`` (dict), ``ollama_running`` (bool),
        ``installed_models`` (list, when Ollama is up), ``message`` (str),
        ``compliance_warning`` (str, when chat_api_base is external).
    """
    from ..config.settings import _is_loopback_url

    result: dict = {"suggest_cloud": False}

    # ── Chat API configuration info ──
    if cfg.chat_api_base:
        from .chat_router import detect_chat_endpoint

        fmt, endpoint = detect_chat_endpoint(cfg)
        result["chat_api"] = {
            "configured": True,
            "endpoint": endpoint,
            "format": fmt,
            "model": cfg.model,
        }
        if not _is_loopback_url(cfg.chat_api_base):
            result["chat_api"]["compliance_warning"] = (
                "External cloud API detected. Source code snippets in chat "
                "prompts WILL be sent to this external endpoint. Ensure this "
                "complies with your organization's data security policies. "
                "Consider using local Ollama or an internal API proxy."
            )
    else:
        result["chat_api"] = {"configured": False, "model": cfg.model}

    # ── Ollama connectivity (for embedding + possibly chat) ──
    base_url = cfg.ollama_url.rstrip("/")
    try:
        resp = httpx.get(base_url + "/api/tags", timeout=5.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        result["ollama_running"] = False
        if cfg.chat_api_base:
            result["status"] = "embedding_unavailable"
            result["message"] = (
                "Ollama is not running. Chat is configured on an external "
                "API, but embedding requires local Ollama. Semantic search "
                "will be disabled. Start Ollama: https://ollama.com"
            )
        else:
            result["status"] = "not_configured"
            result["message"] = (
                "Ollama is not running and no chat API is configured. "
                "Options: (1) install and start Ollama, (2) configure a "
                "cloud API via the configure_llm tool."
            )
        return result
    except Exception as e:
        result["ollama_running"] = False
        result["status"] = "error"
        result["message"] = str(e)
        return result

    # ── Ollama running — check models ──
    models = resp.json().get("models", [])
    installed = [m["name"] for m in models]
    result["ollama_running"] = True
    result["ollama_url"] = cfg.ollama_url
    result["installed_models"] = installed
    result["num_ctx"] = cfg.num_ctx
    model_details = [
        {
            "name": m["name"],
            "parameter_size": m.get("details", {}).get("parameter_size", "unknown"),
            "family": m.get("details", {}).get("family", "unknown"),
            "quantization": m.get("details", {}).get("quantization_level", "unknown"),
        }
        for m in models
    ]
    result["model_details"] = model_details

    # Embedding model check (embedding always uses Ollama)
    embed_found = _check_model_installed(cfg.embed_model, installed)
    result["configured_embed_model"] = cfg.embed_model
    result["embedding_installed"] = embed_found

    if cfg.chat_api_base:
        # Chat goes to external API — Ollama only needs embedding model
        if embed_found:
            result["status"] = "ok"
        else:
            result["status"] = "model_missing"
            result["message"] = (
                f"Embedding model '{cfg.embed_model}' is not installed. Run: ollama pull {cfg.embed_model}"
            )
    else:
        # Chat on Ollama — check chat model too
        model_found = _check_model_installed(cfg.model, installed)
        result["configured_model"] = cfg.model

        if model_found and embed_found:
            result["status"] = "ok"
        else:
            result["status"] = "model_missing"
            messages: list[str] = []
            if not model_found:
                messages.append(f"Chat model '{cfg.model}' is not installed. Run: ollama pull {cfg.model}")
                # If the largest installed model is <7B parameters, suggest cloud API.
                # Models <7B generally produce poor-quality code analysis;
                # the operator may want to use an external API instead.
                max_size = max(
                    (_parse_parameter_size(md["parameter_size"]) for md in model_details),
                    default=0.0,
                )
                if max_size < 7.0:
                    result["suggest_cloud"] = True
            if not embed_found:
                messages.append(
                    f"Embedding model '{cfg.embed_model}' is not installed. Run: ollama pull {cfg.embed_model}"
                )
            result["message"] = " ".join(messages)

    if cfg.debug_log:
        result["debug_log"] = str(cfg.debug_log)

    return result




class OllamaError(RuntimeError):
    """Base exception for Ollama API and network failures."""


class ChatApiError(OllamaError):
    """Exception for external chat API failures (OpenAI-compatible endpoints).

    Raised by :func:`~fw_context_mcp.llm.chat_router.call_openai_chat`
    for HTTP, network, and response-format errors from external APIs.
    Inherits from :class:`OllamaError` so existing ``except OllamaError``
    handlers catch it without changes.
    """


class OllamaModelNotFoundError(OllamaError):
    """Raised when the requested model is not installed in Ollama.

    The error message includes the exact ``ollama pull`` command to run.
    """

    def __init__(self, model: str, url: str) -> None:
        self.model = model
        super().__init__(f"Model '{model}' not found in Ollama at {url}. Run: ollama pull {model}")
