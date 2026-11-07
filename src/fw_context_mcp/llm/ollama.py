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
    ``_ollama_sem`` — per-process BoundedSemaphore.  See Design
    decisions above.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx

from ..config.settings import LLMConfig
from ..utils import is_fatal
from ._debug import _write_debug_log
from .calibration import EmbedMeasure, Key, Measure, Speed, select_context
from .client import ChatResult, ContextFit, Usage
from .embedder import Embedder, EmbedderContextUnknown, EmbedderInputTooLong
from .errors import LLMError

log = logging.getLogger(__name__)

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

    All requests of ``OllamaClient`` and ``call_ollama_embed`` should be wrapped
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
        if is_fatal(e):
            raise
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


def _extract_ollama_chunk(line: str | None, chunks: list[str], stats: dict) -> None:
    """Add the text of one Ollama streaming JSON line to *chunks*.

    The last line (``"done": true``) carries the token counts of the
    request; *stats* gets that line, for :func:`_ollama_usage`.
    """
    if not line:
        return
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    # A line that is JSON but not an object raises AttributeError here, and
    # the request fails (OllamaError), as before the client class.
    chunk = obj.get("response", "")
    if chunk:
        chunks.append(chunk)
    if obj.get("done"):
        stats.update(obj)


def _ollama_usage(answer: dict) -> Usage:
    """Return the token counts of an ``/api/generate`` answer.

    ``prompt_eval_count`` includes the tokens that the server took from its
    prompt cache (from Ollama 0.30.1, see ``MIN_OLLAMA_VERSION``);
    ``prompt_eval_cached_count`` is that part.  A field that the answer
    does not carry is None.
    """

    def count(key: str) -> int | None:
        value = answer.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return Usage(
        prompt_tokens=count("prompt_eval_count"),
        cached_tokens=count("prompt_eval_cached_count"),
        completion_tokens=count("eval_count"),
    )


#: The (server, model) pairs that this process checked for a cloud model.
_cloud_checked: set[tuple[str, str]] = set()


def _tell_cloud_model(cfg: LLMConfig, server_url: str) -> None:
    """Tell on stderr one time for each process when the chat model of *server_url* is a cloud model.

    The URL of such a server is a loopback one, thus the notice of ``load``
    (``settings._warn_external_llm``) does not see that the source code
    leaves the machine.  The server tells it (``remote_host``).
    """
    key = (server_url, cfg.model)
    if key in _cloud_checked:
        return
    from ._diag import remote_hosts

    hosts = remote_hosts(server_url)
    if hosts is None:
        return  # no list now: the next request asks again
    _cloud_checked.add(key)
    host = hosts.get(cfg.model)
    if host:
        print(
            f"ℹ The chat of LLM profile {cfg.profile or '(default)'!r} uses the cloud model {cfg.model}: "
            f"source code snippets go to {host}.",
            file=sys.stderr,
        )


class OllamaClient:
    """The chat client of one Ollama server (``/api/generate``).

    *generate_url* is the ``/api/generate`` URL of the requests;
    *server_url* is the base URL of the server, for the pull of the model,
    ``/api/show``, and the error messages.  The two differ only in the
    path: :func:`~.client.get_chat_client` gives both.

    Each request asks the server not to cut the prompt (``truncate:
    false``): a prompt longer than the context gets HTTP 400 with the exact
    count of its tokens (:class:`OllamaPromptTooLong`), thus the caller can
    select a larger context.

    Each request also turns off the thinking of the model (``think:
    false``).  Measured 2026-10-23 with gemma4:26b, which thinks by default:
    the model wrote its reasoning first, used each answer token, and the
    response was empty for each symbol.  The answer has no use for the
    reasoning.  A model without thinking accepts the key (measured with
    qwen2.5-coder:14b).  gpt-oss ignores it: it knows only the levels of
    thinking.

    Streaming (when ``cfg.stream`` is True, Plan B): ``httpx.stream()``
    with newline-delimited JSON chunks.  Without it, the request sends
    ``stream: false``; an answer that streams anyway is parsed as a stream
    (Plan A).  See the module docstring.
    """

    counts_tokens = True

    def __init__(self, cfg: LLMConfig, generate_url: str, server_url: str) -> None:
        self.cfg = cfg
        self.generate_url = generate_url
        # WHY rstrip: the config keeps a trailing slash of ``ollama_url``,
        # and "<url>//api/show" got HTTP 307, which httpx does not follow:
        # the trained context was then unknown, and the run made no analysis.
        self.server_url = server_url.rstrip("/")

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
    ) -> ChatResult:
        """Send *prompt* to ``/api/generate`` and return the answer and its token counts.

        *num_ctx* is the context of this request (None: ``cfg.num_ctx``).  A
        request with another context than the loaded model makes the server
        load the model again.

        Raises:
            OllamaModelNotFoundError: The model is not installed and
                ``auto_pull`` is off.
            OllamaPromptTooLong: The prompt has more tokens than the context.
            OllamaError: Each other failure of the network or the server.
        """
        note_chat_use(self.server_url, self.cfg.model)
        cfg = self.cfg
        _tell_cloud_model(cfg, self.server_url)
        options: dict = {"num_ctx": num_ctx if num_ctx is not None else cfg.num_ctx, "temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        payload: dict = {
            "model": cfg.model,
            "prompt": prompt,
            "stream": False,
            "truncate": False,
            "think": False,
            "keep_alive": cfg.keep_alive,
            "options": options,
        }
        t0 = time.monotonic()
        result = self._stream(payload) if cfg.stream else self._post(payload)
        if cfg.debug_log:
            entry = {
                "ts": datetime.now(UTC).isoformat(),
                "model": cfg.model,
                "num_ctx": options["num_ctx"],
                "latency_s": round(time.monotonic() - t0, 2),
                "prompt": prompt,
                "response": result.text,
            }
            if cfg.stream:
                entry["streamed"] = True
            _write_debug_log(cfg.debug_log, entry)
        return result

    def _post(self, payload: dict) -> ChatResult:
        """One non-streaming request (Plan A: an answer that streams anyway is parsed as a stream)."""
        cfg, url, base_url = self.cfg, self.generate_url, self.server_url
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
            too_long = _prompt_too_long(resp)
            if too_long is not None:
                raise too_long
            resp.raise_for_status()

            ct = resp.headers.get("content-type", "").lower()
            if "text/event-stream" in ct or "application/x-ndjson" in ct:
                log.debug("Ollama returned streaming response despite stream:false — parsing (Plan A fallback)")
                return ChatResult(_accumulate_ollama_sse(resp))
            answer = resp.json()
            return ChatResult(answer["response"], _ollama_usage(answer))
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

    def _stream(self, payload: dict) -> ChatResult:
        """One streaming request (Plan B).

        ``httpx.stream()`` keeps the connection busy with continuous data,
        thus a reverse proxy in front of a remote Ollama does not close it
        as idle during a long answer.
        """
        cfg, url, base_url = self.cfg, self.generate_url, self.server_url
        payload["stream"] = True
        chunks: list[str] = []
        stats: dict = {}
        try:
            with ollama_guard():
                with httpx.stream("POST", url, json=payload, timeout=cfg.timeout) as resp:
                    if resp.status_code == 404:
                        if not cfg.auto_pull:
                            raise OllamaModelNotFoundError(cfg.model, base_url) from None
                        log.info("LLM model '%s' not found, pulling...", cfg.model)
                        _pull_model(cfg.model, base_url)
                        with httpx.stream("POST", url, json=payload, timeout=cfg.timeout) as resp2:
                            self._read_stream(resp2, chunks, stats)
                    else:
                        self._read_stream(resp, chunks, stats)
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
            if is_fatal(e):
                raise
            raise OllamaError(str(e)) from e
        if not chunks:
            raise OllamaError("Streaming Ollama request returned no content")
        return ChatResult("".join(chunks), _ollama_usage(stats))

    @staticmethod
    def _read_stream(resp: httpx.Response, chunks: list[str], stats: dict) -> None:
        """Read the lines of a streaming answer into *chunks* and *stats*.

        An error answer is read first.  WHY: the handler of the error reads
        ``response.text``, and httpx raised ``ResponseNotRead`` for the body
        of a stream that was not read, in the place of the error.  A 400
        body also says whether the prompt is too long
        (:class:`OllamaPromptTooLong`).
        """
        if resp.status_code >= 400:
            resp.read()
        if resp.status_code == 400:
            too_long = _prompt_too_long(resp)
            if too_long is not None:
                raise too_long
        resp.raise_for_status()
        for line in resp.iter_lines():
            _extract_ollama_chunk(line, chunks, stats)

    def count_tokens(self, prompt: str, num_ctx: int) -> int:
        """Return the number of tokens of *prompt* for the chat model.

        A request with ``num_predict: 1`` and ``truncate: false``.  A prompt
        that fits the context gives ``prompt_eval_count``; a longer one
        gives the count in the error of the server
        (:class:`OllamaPromptTooLong`).  The count comes from the tokenizer
        of the model, not from a ratio of characters to tokens.

        WHY it costs almost nothing: the server keeps the evaluated prompt,
        and a request with the same prompt right after it evaluates one
        token only.  Measured with qwen2.5-coder:14b: 8626 tokens took 7.7 s
        for the count, and 0.08 s in the request that followed.

        *num_ctx* is the context of the request that follows; another value
        makes the server load the model again.

        Raises:
            OllamaError: The request failed.
        """
        note_chat_use(self.server_url, self.cfg.model)
        cfg, url = self.cfg, self.generate_url
        payload = {
            # "think": false as in ``generate``: the template of a thinking
            # model depends on it (gemma4:26b: 3 tokens more without it).
            "model": cfg.model, "prompt": prompt, "stream": False, "truncate": False, "think": False,
            "keep_alive": cfg.keep_alive, "options": {"num_ctx": num_ctx, "num_predict": 1},
        }
        try:
            with ollama_guard():
                resp = httpx.post(url, json=payload, timeout=cfg.timeout)
            too_long = _prompt_too_long(resp)
            if too_long is not None:
                return too_long.prompt_tokens
            resp.raise_for_status()
            return int(resp.json()["prompt_eval_count"])
        except httpx.TimeoutException:
            raise OllamaError(f"Ollama request timed out after {cfg.timeout:.0f}s") from None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise OllamaModelNotFoundError(cfg.model, self.server_url) from e
            raise OllamaError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, OSError, KeyError, json.JSONDecodeError, ValueError, TypeError) as e:
            raise OllamaError(str(e)) from e

    def trained_context(self) -> int | None:
        """Return the context that the chat model was trained for (``/api/show``), or None.

        The largest context that the server gives the model: a request with
        a larger ``num_ctx`` gets the trained one.  None when the server
        does not answer, or does not have the model.
        """
        return _trained_context(self.server_url, self.cfg.model)

    def pull_model(self) -> None:
        """Pull the chat model onto the server (``auto_pull``).

        Raises:
            OllamaError: The pull failed.
        """
        log.info("LLM model '%s' not found, pulling...", self.cfg.model)
        _pull_model(self.cfg.model, self.server_url)

    def fit_context(self, lower: int, upper: int, *, check: bool) -> ContextFit | None:
        """Select the context of a run from the measured speed of the model (``llm.calibration``).

        No choice when *upper* is not above *lower*: the run uses *upper*,
        and the key would cost two requests and ``nvidia-smi`` for nothing.
        """
        if upper <= lower:
            return None
        key = calibration_key(self.server_url, self.cfg.model, lower, upper)
        if key is None:
            return None
        if check:
            note_chat_use(self.server_url, self.cfg.model)

        def measure(num_ctx: int) -> Measure:
            return measure_speed(self.server_url, self.cfg.model, num_ctx, keep_alive=self.cfg.keep_alive,
                                 timeout=self.cfg.timeout, stream=self.cfg.stream)

        return select_context(measure, key, check=check)


# The oldest Ollama that the LLM analysis and the embeddings can use.  Read
# from the source of Ollama:
# * v0.30.0 runs GGUF models in llama-server, and a prompt longer than the
#   context then gets HTTP 400 with the count of its tokens
#   (``exceed_context_size_error``, ``n_prompt_tokens``).
# * v0.30.1 counts the cached tokens of a prompt in ``prompt_eval_count``.
#   In v0.30.0 the count of a prompt that shares a start with the prompt
#   before it was too small, thus the answer budget too.
# The analysis counts each prompt from the two (``OllamaClient.count_tokens``).
# Before them, a ``/api/generate`` request with ``truncate: false`` (v0.12.6)
# got an error without a count, and a server before v0.12.6 cut the prompt
# without an error.  ``/api/ps`` names the context of a loaded model from
# v0.10.0.  Tested with v0.35.1.
MIN_OLLAMA_VERSION = (0, 30, 1)


def ollama_version(base_url: str) -> tuple[int, int, int] | None:
    """Return the version of the Ollama server at *base_url* (``/api/version``), or None.

    None when the server does not answer, or gives a version that is not
    ``X.Y.Z`` (a build from source gives ``0.0.0``, which is a valid answer).
    A suffix such as ``-rc0`` does not count.
    """
    try:
        resp = httpx.get(base_url.rstrip("/") + "/api/version", timeout=5.0)
        resp.raise_for_status()
        version = resp.json().get("version")
    except (httpx.HTTPError, OSError, ValueError, AttributeError):
        return None
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def ollama_version_problem(base_url: str) -> str | None:
    """Return a sentence about an Ollama server that is too old for fw-context, or None.

    None also when the version is not known: the check of the server
    reports a server that does not answer.  ``0.0.0`` (a build from source)
    is not known to be old, thus it is None too.
    """
    version = ollama_version(base_url)
    if version is None or version == (0, 0, 0) or version >= MIN_OLLAMA_VERSION:
        return None
    have, need = ".".join(map(str, version)), ".".join(map(str, MIN_OLLAMA_VERSION))
    return (
        f"Ollama {have} at {base_url} is older than {need}: the LLM analysis cannot count the tokens "
        f"of a prompt, and the server can cut a long prompt or a long embedding input without an error. "
        f"Update Ollama"
    )


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
        EmbedderInputTooLong: A document text is longer than the context
            of the loaded model (HTTP 400 "exceeds the context length").
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
    payload: dict[str, object] = {
        "model": cfg.embed_model,
        "input": inputs,
        "keep_alive": cfg.keep_alive,
    }
    if not query:
        # A document longer than the context of the loaded model gets an
        # error, not a vector of its head only: the server cuts an input
        # without a word when "truncate" is true (its default).  The caller
        # makes the chunk shorter (EmbedderInputTooLong).  A query stays as
        # it is: it is short, and a search without an answer helps nobody.
        payload["truncate"] = False
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
        if e.response.status_code == 400 and "exceeds the context length" in e.response.text:
            raise EmbedderInputTooLong(
                f"an input is longer than the context of {cfg.embed_model}: {e.response.text[:200]}"
            ) from e
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


#: Set in the test session (conftest), also for each process that a test
#: starts: a test must not unload a model of the Ollama of the operator,
#: and must not load one to measure its speed (``llm.calibration``).
NO_RELEASE_ENV = "FW_CONTEXT_NO_OLLAMA_RELEASE"

#: The (server, chat model) pairs that this process sent requests to.  The
#: release before the embeddings unloads only such a model: a model that
#: another project or another client uses is not ours to unload.
_chat_used: set[tuple[str, str]] = set()

#: How long a release waits until ``/api/ps`` no longer lists the model.
_RELEASE_WAIT_S = 10.0


def _with_tag(model: str) -> str:
    """Return *model* with its tag: ``/api/ps`` names ``nomic-embed-text`` as ``nomic-embed-text:latest``.

    The tag follows the last ``/``: in ``host:5000/team/model`` the colon
    is of the port.
    """
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


def loaded_models(server_url: str) -> list[str] | None:
    """Return the models that the Ollama server holds in memory (``/api/ps``), or None when it gives no list."""
    try:
        resp = httpx.get(server_url.rstrip("/") + "/api/ps", timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        return [] if isinstance(body, dict) and body.get("models") is None else None
    names = [m.get("name") or m.get("model") for m in models if isinstance(m, dict)]
    return [name for name in names if isinstance(name, str) and name]


def note_chat_use(server_url: str, model: str) -> None:
    """Record that this process sent a chat request to *model* on *server_url* (see ``_chat_used``)."""
    _chat_used.add((server_url.rstrip("/"), _with_tag(model)))


def release_model(server_url: str, model: str) -> bool | None:
    """Ask the Ollama server to unload *model*; True when ``/api/ps`` no longer lists it.

    None: the server did not hold the model, or gave no list (nothing to
    do).  False: the server still holds it after ``_RELEASE_WAIT_S``.
    ``keep_alive: 0`` of ``/api/generate`` unloads a chat model and an
    embedding model (measured with Ollama 0.35.1, 2026-10-21).  A release
    is not certain: a request of another client can load the model again
    at once.
    """
    name = _with_tag(model)
    held = loaded_models(server_url)
    if held is None or name not in held:
        return None
    try:
        resp = httpx.post(server_url.rstrip("/") + "/api/generate", json={"model": name, "keep_alive": 0},
                          timeout=30.0)
    except httpx.HTTPError as exc:
        log.info("Ollama at %s: the release of %s failed: %s", server_url, name, type(exc).__name__)
        return False
    if resp.status_code >= 400:
        log.info("Ollama at %s: the release of %s got HTTP %d", server_url, name, resp.status_code)
        return False
    deadline = time.monotonic() + _RELEASE_WAIT_S
    while time.monotonic() < deadline:
        held = loaded_models(server_url)
        if held is not None and name not in held:
            return True
        time.sleep(0.5)
    return False


def release_other_model(cfg: LLMConfig, phase: str) -> None:
    """Before *phase* (``"analysis"`` or ``"embeddings"``), unload the model of the other phase from Ollama.

    Only when the chat and the embeddings use one Ollama server: they then
    share its GPU memory, and a model that ``keep_alive`` holds (10 min by
    default) leaves the next model only the rest (measured 2026-10-20: a
    model of 3.5 GB ran 45 % on the GPU after three other models).  A chat
    on an API, or on another server, shares nothing.  A model that stays
    gives a warning, not an error: the run goes on, slower.
    """
    if os.environ.get(NO_RELEASE_ENV) or cfg.provider != "ollama" or not cfg.embed_model:
        return
    from .chat_router import chat_ollama_server
    from .embedder_factory import uses_ollama

    server = chat_ollama_server(cfg)
    if not uses_ollama(cfg) or not server or server.rstrip("/") != cfg.ollama_url.rstrip("/"):
        return
    if phase == "analysis":
        model = cfg.embed_model
    else:
        # Only a chat model that this process used: else a run without an
        # analysis unloaded the model of another project or client.
        key = (server.rstrip("/"), _with_tag(cfg.model))
        if key not in _chat_used:
            return
        _chat_used.discard(key)
        model = cfg.model
    result = release_model(server, model)
    if result is True:
        log.info("Ollama: released %s before the %s (the GPU memory goes to the next model)", model, phase)
    elif result is False:
        log.warning("Ollama at %s still holds %s before the %s: the next model gets less GPU memory",
                    server, model, phase)


#: The text of a load that failed for memory: the list of Ollama 0.40.7
#: (``llm/status.go``, ``outOfMemorySubstrings``), which Ollama uses for
#: its own retry after such an error.  WHY the same list: an error that
#: Ollama does not take as memory is not a measurement here either.  Plus
#: the error of the scheduler for a model that is too large for the memory
#: of the machine (``llm/server.go`` ``ErrLoadRequiredFull``, given at
#: ``server/sched.go`` "model is too large for system memory").
_OUT_OF_MEMORY = (
    "out of memory", "out of device memory", "cudamalloc failed", "hipmalloc failed", "failed to allocate",
    "allocation failed", "not enough memory", "insufficient memory", "vk_error_out_of_device_memory",
    "erroroutofmemory", "failed to reserve graph", "unable to load full model on gpu",
)

#: The reference request of ``measure_speed``: the body of a C function of
#: about 1700 tokens, the length of a typical prompt of the analysis.
#: Checked 2026-10-22: the reference prompt is 1690 tokens, and the mean
#: prompt of the 30 symbols of the blind review with the current prompt was
#: 2012 tokens (DeepSeek API).  The reference is 16 % shorter, and stays:
#: the length changes only the prompt part of the time of a symbol
#: (``Speed.seconds_per_symbol``), and that part is small against the
#: answer (qwen2.5-coder:14b, 2026-10-22: about 2 s of the prompt at 960 tokens/s,
#: 13 s of an answer of 300 tokens at 23 tokens/s).
_REFERENCE_BODY = "\n".join(
    f"    if (buf[{i}] & 0x{i:02x}) {{ crc = (crc >> 1) ^ table[(crc ^ buf[{i}]) & 0xff]; }}" for i in range(40)
)
_REFERENCE_PROMPT = (
    "Describe this C function. Answer with JSON: summary, inputs, outputs.\n\n"
    "uint32_t frame_crc(const uint8_t *buf, size_t len, const uint32_t *table)\n{\n"
    "    uint32_t crc = 0xffffffffu;\n" + _REFERENCE_BODY + "\n    return crc ^ 0xffffffffu;\n}\n"
)
#: The answer tokens of the reference request: enough for a stable rate.
_REFERENCE_ANSWER_TOKENS = 64
#: A number for each request of ``measure_speed`` in this process.
_REQUEST_NUMBERS = itertools.count()


def measure_speed(server_url: str, model: str, num_ctx: int, *, keep_alive: str, timeout: float,
                  stream: bool = False) -> Measure:
    """Load *model* with *num_ctx* and return the speed of the reference request (``llm.calibration``).

    Two requests of the same prompt, each with a first line of its own:
    the first loads the model and reads each weight that the prompt needs
    (measured 2026-10-21: the first request after a load can be 8 times
    slower, while the weights come from the disk; a short prompt does not
    reach each expert of a MoE model), then the reference request.  The
    new first line keeps the prompt cache of the runner from making the
    prompt of the second request free.  The rates come from the times of
    llama.cpp in the answer, not from the time of the HTTP request.  Each
    request goes through ``ollama_guard`` with the timeout and the
    ``stream`` of the profile, as each request of the analysis: a proxy
    can cut a slow answer that does not stream.  It also sends
    ``think: false``, as the analysis (``OllamaClient``): the measure is
    the speed of the answer that the analysis gets.
    """
    base = server_url.rstrip("/")
    options = {"num_ctx": num_ctx, "temperature": 0, "seed": 42}
    try:
        for tokens in (1, _REFERENCE_ANSWER_TOKENS):
            # The time keeps the line new for another process, the count
            # for two requests in one tick of the clock.
            prompt = f"Request {time.monotonic_ns()}-{next(_REQUEST_NUMBERS)}.\n{_REFERENCE_PROMPT}"
            with ollama_guard():
                resp = httpx.post(f"{base}/api/generate", json={
                    "model": model, "prompt": prompt, "stream": stream, "truncate": False, "think": False,
                    "keep_alive": keep_alive, "options": {**options, "num_predict": tokens}}, timeout=timeout)
            if resp.status_code != 200:
                error = _error_text(resp)
                return Measure(None, error, any(text in error.lower() for text in _OUT_OF_MEMORY))
        body = _final_answer(resp, stream)
    except httpx.HTTPError as e:
        return Measure(None, f"Ollama does not answer ({type(e).__name__}: {e})")
    except ValueError:
        return Measure(None, "Ollama gives an answer that is not JSON")
    # A stream that fails after its first part ends with an error line and
    # HTTP 200: the load is before the stream, thus not this way.
    stream_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(stream_error, str) and stream_error:
        return Measure(None, f"Ollama: {stream_error[:300]}",
                       any(text in stream_error.lower() for text in _OUT_OF_MEMORY))
    counts: list[int] = []
    for key in ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration"):
        value = body.get(key) if isinstance(body, dict) else None
        if not isinstance(value, int) or value <= 0:
            return Measure(None, "Ollama gives no times of the request")
        counts.append(value)
    prompt_count, prompt_ns, eval_count, eval_ns = counts
    return Measure(Speed(prompt_count, prompt_count / prompt_ns * 1e9, eval_count / eval_ns * 1e9))


#: The documents of ``measure_embed_speed``: one batch of the indexer
#: (``MAX_EMBED_BATCH_SYMBOLS`` of ``indexer._embedding``), each a
#: description of a symbol with a part of its body, about 400 tokens.  WHY
#: not longer: mxbai-embed-large has a context of 512, and a longer input
#: measures the cut of the server, not the model.
_EMBED_REFERENCE_SYMBOLS = 10
_EMBED_REFERENCE_BODY = "\n".join(_REFERENCE_BODY.splitlines()[:8])


def _embed_reference_documents() -> list[str]:
    """Return the reference documents, each with a first line of its own (a model can cache an input)."""
    stamp = f"{time.monotonic_ns()}-{next(_REQUEST_NUMBERS)}"
    return [f"Symbol {stamp}-{index}: the function frame_crc_{index} in drivers/crc computes the CRC of a "
            f"frame from a table. It reads the buffer and returns the CRC.\n{_EMBED_REFERENCE_BODY}"
            for index in range(_EMBED_REFERENCE_SYMBOLS)]


def measure_embed_speed(server_url: str, model: str, *, keep_alive: str, timeout: float) -> EmbedMeasure:
    """Load the embedding *model* and return the time of one symbol of a batch of the reference documents.

    Two batches: the first loads the model, the second is the measurement.
    The time comes from the answer of Ollama (``total_duration`` without
    ``load_duration``), not from the time of the HTTP request.  The
    documents get no instruction: the instruction of a document is empty
    or a few words, and the time of the model is the question.
    """
    base = server_url.rstrip("/")
    try:
        for _ in range(2):
            with ollama_guard():
                resp = httpx.post(f"{base}/api/embed", json={
                    "model": model, "input": _embed_reference_documents(), "keep_alive": keep_alive},
                    timeout=timeout)
            if resp.status_code != 200:
                error = _error_text(resp)
                return EmbedMeasure(None, error, any(text in error.lower() for text in _OUT_OF_MEMORY))
        body = resp.json()
    except httpx.HTTPError as e:
        return EmbedMeasure(None, f"Ollama does not answer ({type(e).__name__}: {e})")
    except ValueError:
        return EmbedMeasure(None, "Ollama gives an answer that is not JSON")
    total = body.get("total_duration") if isinstance(body, dict) else None
    load = body.get("load_duration", 0) if isinstance(body, dict) else None
    if not isinstance(total, int) or not isinstance(load, int) or total - load <= 0:
        return EmbedMeasure(None, "Ollama gives no times of the request")
    return EmbedMeasure((total - load) / 1e9 / _EMBED_REFERENCE_SYMBOLS)


def calibration_key(server_url: str, model: str, lower: int, upper: int) -> Key | None:
    """Return what a calibration of *model* on *server_url* is valid for; None: no calibration.

    None for a cloud model of Ollama (``remote_host`` in ``/api/tags``): it
    runs on another computer, thus its context is not a question of the
    memory here, and a measurement would send the reference request out.
    None too when the server does not list the model.  The memory of this
    machine is part of the key only for a server on this machine.
    """
    from urllib.parse import urlparse

    from ._diag import tag_models
    from .hardware import machine_memory

    try:
        resp = httpx.get(server_url.rstrip("/") + "/api/tags", timeout=5.0)
        resp.raise_for_status()
        version = httpx.get(server_url.rstrip("/") + "/api/version", timeout=5.0).json().get("version")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    name = _with_tag(model)
    entry = next((m for m in tag_models(resp) or [] if m["name"] == name), None)
    if entry is None or entry.get("remote_host"):
        return None
    # Only these names count as this machine: another address of it (a LAN
    # address, 127.0.1.1) gives a key without the memory, thus a new GPU is
    # not seen there.
    memory = machine_memory() if urlparse(server_url).hostname in ("localhost", "127.0.0.1", "::1") else None
    return Key(server=server_url.rstrip("/"), model=name, digest=str(entry.get("digest") or ""),
               ollama=str(version or ""), gpu_mib=memory.gpu_mib if memory else None,
               ram_mib=memory.ram_mib if memory else None, lower=lower, upper=upper)


def _final_answer(resp: httpx.Response, stream: bool) -> object:
    """Return the JSON of the answer; of a streamed answer the last line, which holds the times.

    Raises:
        ValueError: The answer is not JSON, or a stream has no line.
    """
    if not stream:
        return resp.json()
    lines = [line for line in resp.text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("an empty stream")
    return json.loads(lines[-1])


def _error_text(resp: httpx.Response) -> str:
    """Return the error of an Ollama answer: ``{"error": ...}``, else the start of the body."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    text = (error if isinstance(error, str) and error else resp.text)[:300]
    return f"HTTP {resp.status_code}: {text.strip()}"


def _trained_context(base: str, model: str) -> int | None:
    """Return the context length that *model* was trained for, from ``/api/show``.

    ``/api/show`` takes a POST with the model in the body; a GET gets 405.
    ``model_info`` names the value after the architecture of the model
    (``qwen3.context_length``), and ``general.architecture`` names it.
    """
    try:
        resp = httpx.post(f"{base}/api/show", json={"model": model}, timeout=5.0)
        if resp.status_code != 200:
            return None
        info = resp.json().get("model_info", {})
    except (ValueError, TypeError, OSError, json.JSONDecodeError, httpx.HTTPError):
        return None
    arch = info.get("general.architecture")
    value = info.get(f"{arch}.context_length") if isinstance(arch, str) else None
    return value if isinstance(value, int) and value > 0 else None


def _loaded_context(base: str, model: str) -> int | None:
    """Return the context length of the server for *model* when it is loaded (``/api/ps``).

    The list names a model with its tag; a name without a tag means
    ``:latest``, as for each call of the Ollama API.  The tag follows the
    last ``/``: in ``host:5000/team/model`` the colon is of the port.
    """
    names = {model} if ":" in model.rsplit("/", 1)[-1] else {model, f"{model}:latest"}
    try:
        resp = httpx.get(f"{base}/api/ps", timeout=5.0)
        if resp.status_code != 200:
            return None
        loaded = resp.json().get("models", [])
    except (ValueError, TypeError, OSError, json.JSONDecodeError, httpx.HTTPError):
        return None
    for entry in loaded:
        if entry.get("name") in names or entry.get("model") in names:
            value = entry.get("context_length")
            return value if isinstance(value, int) and value > 0 else None
    return None


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

    def _served_context(self, base: str, model: str) -> int:
        """Return the context length of the server for *model* for the requests of fw-context.

        One short embedding request goes first, also when the model is
        loaded: another client can load the model with another
        ``num_ctx``, and the server loads it again for a request with other
        options.  The request has the options of the index run, thus
        ``/api/ps`` after it gives the context of the requests of the run.

        The pair of requests goes a second time when ``/api/ps`` does not
        name the model: another client can make the server unload it
        between the two requests, and a ``keep_alive`` of 0 unloads it at
        once.  The second embedding request thus keeps the model loaded
        for 30 seconds, whatever ``keep_alive`` is; the requests of the step
        then have the configured value again.

        Raises:
            EmbedderContextUnknown: The server did not load the model, or
                it names no context length for it.  No value is guessed:
                the value sets the chunks and the content hash of each long
                body, thus a guessed value stores other vectors, and the
                next run with the real value embeds them again.
        """
        for cfg in (self._cfg, dataclasses.replace(self._cfg, keep_alive="30s")):
            try:
                _call_ollama_embed_impl(["x"], cfg, query=False, embedder=self)
            except OllamaError as exc:
                raise EmbedderContextUnknown(
                    f"Ollama did not load {model} to read its context length: {exc}"
                ) from exc
            served = _loaded_context(base, model)
            if served is not None:
                return served
        raise EmbedderContextUnknown(
            f"Ollama names no context length of {model} in /api/ps; "
            f"an older Ollama does not give it, update Ollama"
        )

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
    def document_settings(self) -> str:
        # The document prompt goes in front of each text (_call_ollama_embed_impl).
        prompt = self._cfg.embed_doc_prompt
        return f"doc_prompt={prompt}" if prompt else ""

    @property
    def max_tokens(self) -> int:
        """The number of tokens of one input that the server embeds.

        Two values set it, and the smaller one applies:

        * The context length of the server for the loaded model, from
          ``/api/ps``.  It comes from ``PARAMETER num_ctx`` of the model, or
          from the default of the server (``OLLAMA_CONTEXT_LENGTH``), thus
          each model and each installation can have a different one.
          Measured with qwen3-embedding:8b: the server used 4096 tokens.
          A document text that is longer gets an error
          (``EmbedderInputTooLong``), because the request asks the server
          not to cut it.  Before, the server cut it without an error, and
          the tail of the chunk was not in its vector.
        * The context length that the model was trained for, from
          ``/api/show`` (40960 for qwen3-embedding:8b).  An older server
          does not give it; the value of the server then applies alone.

        One short embedding request goes first (see ``_served_context``),
        thus the first read in a process loads the model, also in an
        embedding step that then embeds nothing: the step needs the value
        for the content hash of each symbol.

        Each read asks the server, and no value is kept: a value kept in a
        long-lived process (the MCP server) differs from the value of the
        index run after a change of the server setting, and each long body
        then gets other chunks, thus another content hash, in turn.  The
        embedding step reads the value one time.

        Raises:
            EmbedderContextUnknown: The server gave no context length (see
                ``_served_context``).
        """
        model = self._cfg.embed_model
        base = self._cfg.ollama_url.rstrip("/")
        served = self._served_context(base, model)
        trained = _trained_context(base, model)
        return min(served, trained) if trained is not None else served


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



def _without_ollama(result: dict, cfg: LLMConfig, ollama_embedding: bool, cause: str, *, server_runs: bool) -> dict:
    """Complete *result* for an ``ollama_url`` that fw-context cannot use, and return it.

    *cause* is the sentence that tells why: the server does not run, or it
    does not give the answer of Ollama.  The status depends on what needs
    Ollama: nothing when the chat goes to an API and the embedding model
    runs locally.  *server_runs* selects the advice: a server that runs and
    is not Ollama (a proxy page) needs a correct url of the Ollama profile,
    not a start of Ollama.
    """
    result["ollama_running"] = False
    repair = (
        "Correct the url of the Ollama profile so that it names an Ollama server"
        if server_runs else "Install and start Ollama"
    )
    if cfg.chat_api_base and not ollama_embedding:
        result["status"] = "ok"
    elif cfg.chat_api_base:
        result["status"] = "embedding_unavailable"
        link = "." if server_runs else ": https://ollama.com"
        result["message"] = (
            f"{cause} Chat is configured on an external API, but embedding "
            f"requires local Ollama. Semantic search is disabled. {repair}{link}"
        )
    else:
        result["status"] = "not_configured"
        result["message"] = (
            f"{cause} No chat API is configured. Options: (1) {repair[0].lower()}{repair[1:]}, "
            "(2) the operator adds a cloud profile with `fw-context llm setup` in a terminal, "
            "then configure_llm(profile=<name>) selects it for this project."
        )
    return result


def check_setup(cfg: LLMConfig) -> dict:
    """Check LLM backend connectivity and model availability.

    Reports on both the chat API (Ollama native or external) and the
    embedding backend.  An Ollama embedding model needs the Ollama server;
    a sentence-transformers or ``ft://`` model runs locally, and
    ``embedding_backend`` tells which one is configured.  When
    ``chat_api_base`` points to an external host, a compliance warning is
    included.

    Args:
        cfg: LLM configuration.

    Returns:
        dict with keys varying by status:

        ``status`` values:
            - ``"ok"`` — chat and embedding backends ready.
            - ``"disabled"`` — ``cfg.enabled`` is False.
            - ``"not_configured"`` — Ollama cannot be used and no
              chat_api_base.  Ollama does not run, or ``ollama_url`` names
              a server that is not Ollama; ``message`` says which.
            - ``"embedding_unavailable"`` — Ollama cannot be used (as
              above), chat on external API, and the embedding model is an
              Ollama model.  With a local embedding model the status is
              ``"ok"``.
            - ``"model_missing"`` — Ollama running, and the chat model is not
              on the Ollama server of the chat calls (``ollama_url``, or the
              Ollama server of ``chat_api_base``), or the Ollama embedding
              model is not installed.  ``message`` names the command for
              each missing model (`ollama_pull_command`).
            - ``"error"`` — unexpected error probing Ollama, or the Ollama
              server of ``chat_api_base`` gives no model list.

        Common keys: ``chat_api`` (dict), ``ollama_running`` (bool),
        ``installed_models`` (list, when Ollama is up), ``message`` (str),
        ``compliance_warning`` (str, when chat_api_base is external),
        ``embedding_compliance_warning`` (str, when the embeddings go to an
        external API, ``embed_provider``).
    """
    from ..config.settings import _is_loopback_url
    from .embedder_factory import uses_ollama

    ollama_embedding = uses_ollama(cfg)
    result: dict = {
        "suggest_cloud": False,
        "embedding_backend": ("ollama" if ollama_embedding else "local") if cfg.embed_provider == "ollama"
        else cfg.embed_provider,
    }
    if cfg.embed_provider != "ollama" and not _is_loopback_url(cfg.embed_api_base):
        result["embedding_compliance_warning"] = (
            f"The embeddings go to an external API ({cfg.embed_api_base}). Source code snippets and each search "
            "query WILL be sent to this endpoint. Ensure this complies with your organization's data security "
            "policies.")

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
        return _without_ollama(result, cfg, ollama_embedding, "Ollama is not running.", server_runs=False)
    except httpx.HTTPStatusError as e:
        # A server answers, with an HTTP error: the same case as the page of
        # a proxy below, thus the same status and the same advice.
        cause = f"{base_url}/api/tags gave HTTP {e.response.status_code}, not the model list of Ollama."
        return _without_ollama(result, cfg, ollama_embedding, cause, server_runs=True)
    except Exception as e:
        result["ollama_running"] = False
        result["status"] = "error"
        result["message"] = str(e)
        return result

    # ── Ollama running — check models ──
    from ._diag import not_ollama_message, tag_models

    models = tag_models(resp)
    if models is None:
        # The same answer as for a server that does not run: a setup that
        # needs no Ollama (chat on an API, local embeddings) is "ok".
        return _without_ollama(result, cfg, ollama_embedding, not_ollama_message(cfg.ollama_url), server_runs=True)
    installed = [m["name"] for m in models]
    result["ollama_running"] = True
    result["ollama_url"] = cfg.ollama_url
    result["installed_models"] = installed
    result["num_ctx"] = cfg.num_ctx

    def _details(model: dict) -> dict:
        details = model.get("details")
        return details if isinstance(details, dict) else {}

    model_details = [
        {
            "name": m["name"],
            "parameter_size": _details(m).get("parameter_size", "unknown"),
            "family": _details(m).get("family", "unknown"),
            "quantization": _details(m).get("quantization_level", "unknown"),
        }
        for m in models
    ]
    result["model_details"] = model_details

    # Embedding model check: only an Ollama model must be installed there.
    embed_found = not ollama_embedding or _check_model_installed(cfg.embed_model, installed)
    result["configured_embed_model"] = cfg.embed_model
    result["embedding_installed"] = embed_found

    # The chat model is on the Ollama server of the chat calls: the server
    # of ollama_url, or the Ollama server of chat_api_base.  `None`: an
    # OpenAI-compatible API runs it, and an Ollama server does not have to
    # hold it.  WHY: with chat_api_base on an Ollama server, the chat calls
    # go there, and this check did not look for the model at all.
    # detect_chat_endpoint above already raises ValueError for a format that
    # is not valid.
    from ._diag import installed_models
    from .chat_router import chat_ollama_server

    chat_server = chat_ollama_server(cfg)
    messages: list[str] = []
    chat_models: list[str] | None = []
    same_server = chat_server is not None and chat_server.rstrip("/") == base_url
    if chat_server is not None:
        result["configured_model"] = cfg.model
        chat_models = installed if same_server else installed_models(chat_server)
    if chat_models is None:
        messages.append(f"The Ollama server {chat_server} of the chat profile gives no model list (/api/tags).")
    elif chat_server is not None and not _check_model_installed(cfg.model, chat_models):
        messages.append(
            f"Chat model '{cfg.model}' is not installed. Run: {ollama_pull_command(cfg.model, chat_server)}"
        )
        # If the largest installed model is <7B parameters, suggest cloud API.
        # Models <7B generally produce poor-quality code analysis;
        # the operator may want to use an external API instead.
        # Only the server of ollama_url gives the sizes of its models.
        max_size = max(
            (_parse_parameter_size(md["parameter_size"]) for md in model_details),
            default=0.0,
        )
        if same_server and max_size < 7.0:
            result["suggest_cloud"] = True
    # The text of the embedding model is also in the message of "error": the
    # operator reads only `message` for that status.
    if not embed_found:
        messages.append(
            f"Embedding model '{cfg.embed_model}' is not installed. "
            f"Run: {ollama_pull_command(cfg.embed_model, cfg.ollama_url)}"
        )
    if chat_models is None:
        result["status"] = "error"
    else:
        result["status"] = "model_missing" if messages else "ok"
    if messages:
        result["message"] = " ".join(messages)

    if cfg.debug_log:
        result["debug_log"] = str(cfg.debug_log)

    return result




class OllamaError(LLMError):
    """A request to an Ollama server failed (network, HTTP, or the answer)."""


class OllamaPromptTooLong(OllamaError):
    """The prompt has more tokens than the context of the request (HTTP 400).

    The request asks the server not to cut the prompt (``truncate: false``).
    Without it, the server cuts a long prompt without an error, and the
    model answers about a part of it: measured, a prompt of 6413 tokens got
    1026 tokens to the model.  The server gives the exact count of the
    tokenizer of the model, thus the caller can select a larger context.
    """

    def __init__(self, prompt_tokens: int, context: int) -> None:
        super().__init__(f"the prompt has {prompt_tokens} tokens, the context of the request has {context}")
        self.prompt_tokens = prompt_tokens
        self.context = context


# The message of the runner (llama-server) for a prompt longer than the
# context.  Ollama 0.40 gives it as the plain string of ``error``.
_TOO_LONG_MESSAGE = re.compile(
    r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)"
)


def _prompt_too_long(resp: httpx.Response) -> OllamaPromptTooLong | None:
    """Return the error of a 400 answer that says the prompt is too long, else None.

    Two forms, both with the exact counts of the server:

    - Ollama 0.35 puts the error of its runner as a JSON string into the
      field ``error``: ``{"error": {"type": "exceed_context_size_error",
      "n_prompt_tokens": 6413, "n_ctx": 2048, ...}}``.
    - Ollama 0.40 puts only the message of the runner there: ``{"error":
      "request (18924 tokens) exceeds the available context size (16384
      tokens), try increasing it"}``.  Measured with Ollama 0.40.7: the
      parse of the first form alone did not see it, thus the analysis got
      HTTP 400 for a long prompt instead of a second pass with a larger
      context.
    """
    if resp.status_code != 400:
        return None
    try:
        outer = resp.json().get("error")
    except (ValueError, AttributeError):
        return None
    if isinstance(outer, str):
        try:
            inner = json.loads(outer)
        except ValueError:
            inner = None
        if not isinstance(inner, dict):
            match = _TOO_LONG_MESSAGE.search(outer)
            return OllamaPromptTooLong(int(match[1]), int(match[2])) if match else None
    else:
        inner = outer
    error = inner.get("error") if isinstance(inner, dict) else None
    if not isinstance(error, dict) or error.get("type") != "exceed_context_size_error":
        return None
    prompt_tokens, context = error.get("n_prompt_tokens"), error.get("n_ctx")
    if not isinstance(prompt_tokens, int) or not isinstance(context, int):
        return None
    return OllamaPromptTooLong(prompt_tokens, context)


def ollama_pull_command(model: str, url: str, system: str | None = None) -> str:
    """Return the shell command that pulls *model* onto the Ollama server at *url*.

    The ``ollama`` CLI has no option for the server: it reads
    ``OLLAMA_HOST``.  The command always sets it, in the syntax of
    PowerShell on Windows and of a POSIX shell on the other systems.
    *system* is a value of ``sys.platform``; ``None`` is this system.

    WHY always, also for the default ``ollama_url``: a plain ``ollama pull``
    uses the ``OLLAMA_HOST`` of the shell of the person.  When that names
    a different server, the model goes there, and the check still finds
    it missing.  The process that makes the command cannot know that
    shell: the MCP server runs in the environment of its client.
    """
    if (system or sys.platform) == "win32":
        # A PowerShell string in single quotes: a quote in it is doubled.
        host = url.replace("'", "''")
        name = model.replace("'", "''")
        return f"$env:OLLAMA_HOST='{host}'; ollama pull '{name}'"
    return f"OLLAMA_HOST={shlex.quote(url)} ollama pull {shlex.quote(model)}"


class OllamaModelNotFoundError(OllamaError):
    """Raised when the requested model is not installed in Ollama.

    The error message includes the exact ``ollama pull`` command to run,
    for the server at *url* (`ollama_pull_command`).
    """

    def __init__(self, model: str, url: str) -> None:
        self.model = model
        super().__init__(f"Model '{model}' not found in Ollama at {url}. Run: {ollama_pull_command(model, url)}")
