"""Thin Ollama client for fw-context LLM tools."""

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
from pathlib import Path

import httpx

from ..config.settings import LLMConfig

log = logging.getLogger(__name__)

# Per-process lock serializing Ollama HTTP calls.  Multiple concurrent
# requests to a single Ollama instance can cause timeouts, OOM, or hangs
# when the GPU is saturated.  This lock ensures at most one in-flight
# Ollama request per process.
#
# NOTE: this is NOT about httpx thread-safety (httpx.Client is thread-safe).
# It prevents saturating the Ollama server, which typically runs with one
# GPU and limited VRAM.  In the MCP stdio transport (one request at a time),
# this lock has no throughput impact.  If you switch to a multi-client
# transport (SSE/streamable), consider replacing with a semaphore to allow
# limited concurrency for embedding requests (small model, fast).
_ollama_lock = threading.Lock()


@contextmanager
def ollama_guard() -> Generator[None, None, None]:
    """Context manager serializing Ollama access within this process.

    All ``call_ollama`` and ``call_ollama_embed`` calls should be wrapped
    in this guard to prevent concurrent requests from overwhelming the
    local Ollama instance.  Cross-process serialization is handled
    indirectly through the write lock (``db.write_lock``) — only the
    process that holds the write lock can perform LLM analysis writes.
    """
    with _ollama_lock:
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
    except OllamaError:
        raise  # Pass through — prevents wrapping by the broad except Exception below
    except Exception as e:
        raise OllamaError(str(e)) from e


def call_ollama_embed(inputs: list[str], cfg: LLMConfig, *, query: bool = True) -> list[list[float]]:
    """Generate embeddings for a batch of texts via Ollama.

    Uses the configured embedding model (e.g. ``mxbai-embed-large``).
    Batching is handled by Ollama — send all texts in one request.

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
                with ollama_guard():
                    resp = httpx.post(url, json=payload, timeout=cfg.timeout * 2)
                resp.raise_for_status()
                embeddings = resp.json()["embeddings"]
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
            raise OllamaModelNotFoundError(cfg.embed_model, cfg.ollama_url) from e
        raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except httpx.ConnectError as e:
        raise OllamaError(
            f"Cannot connect to Ollama at {cfg.ollama_url}. Make sure Ollama is running: https://ollama.com"
        ) from e
    except httpx.TimeoutException:
        raise OllamaError("Ollama embed request timed out") from None
    except OllamaError:
        raise  # Pass through — prevents wrapping by the broad except Exception below
    except Exception as e:
        raise OllamaError(str(e)) from e


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


class OllamaModelNotFoundError(OllamaError):
    """Raised when the requested model is not installed in Ollama.

    The error message includes the exact ``ollama pull`` command to run.
    """

    def __init__(self, model: str, url: str) -> None:
        self.model = model
        super().__init__(f"Model '{model}' not found in Ollama at {url}. Run: ollama pull {model}")
