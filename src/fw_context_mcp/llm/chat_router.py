"""Auto-detecting chat router: Ollama native vs OpenAI-compatible.

When ``LLMConfig.chat_api_base`` is set, chat calls are routed to an
external endpoint instead of the local Ollama ``/api/generate``.  The
API format (Ollama native or OpenAI-compatible) is auto-detected from
the URL, or can be forced via ``chat_api_format``.

Supported OpenAI-compatible backends include: OpenAI, DeepSeek, vLLM,
llama.cpp, LM Studio, LiteLLM proxy, and Ollama's own OpenAI layer.

Embedding calls always go to local Ollama (``/api/embed``) — this module
only handles chat/generation.

Streaming support (Plan A + Plan B):

* **Plan A** (auto-detect): When ``cfg.stream`` is False (default), the
  request sends ``stream: false``.  If the API ignores this and returns
  a ``text/event-stream`` response anyway, it is auto-detected and parsed
  transparently — no crash, no configuration needed.
* **Plan B** (active streaming): When ``cfg.stream`` is True, the request
  sends ``stream: true`` and the response is consumed as an SSE stream
  via ``httpx.stream()``.  This keeps the HTTP connection alive with
  continuous data flow, avoiding reverse-proxy idle timeouts.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from ..config.settings import LLMConfig
from ._debug import _write_debug_log
from .ollama import ChatApiError, OllamaError

log = logging.getLogger(__name__)


def detect_chat_endpoint(cfg: LLMConfig) -> tuple[str, str]:
    """Detect API format and resolve the full chat endpoint URL.

    Returns:
        ``("ollama", endpoint_url)`` or ``("openai", endpoint_url)``.

    Detection rules (in priority order):

    1. Explicit ``chat_api_format`` override (``"ollama"`` / ``"openai"``).
    2. URL already ends with ``/api/generate`` -> Ollama, as-is.
    3. URL already ends with ``/chat/completions`` -> OpenAI, as-is.
    4. URL contains ``/v1`` -> OpenAI, append ``/chat/completions``.
    5. URL ends with ``/api`` -> Ollama, append ``/generate``.
    6. URL contains ``:11434`` -> Ollama, append ``/api/generate``.
    7. Bare host (none of above) -> OpenAI, append ``/v1/chat/completions``.

    WHY heuristic-based: There is no standard discovery endpoint for
    Ollama vs OpenAI.  The URL patterns above cover 95%+ of real-world
    configurations (Ollama default port 11434, OpenAI-compatible servers
    at ``/v1``, LiteLLM/vLLM at bare hosts).  The operator can always
    override with ``chat_api_format`` if the heuristic guesses wrong.

    WHY ``chat_api_format`` takes priority over URL patterns: An operator
    who explicitly sets ``chat_api_format = "ollama"`` on a URL that looks
    like OpenAI (e.g. a reverse proxy) knows what they are doing.  The
    explicit override is never second-guessed.
    """
    if not cfg.chat_api_base:
        raise ValueError("chat_api_base is empty — set it or leave as None")
    base = cfg.chat_api_base.rstrip("/")
    lower = base.lower()

    fmt = cfg.chat_api_format.lower()

    # 1. Explicit override
    if fmt == "ollama":
        if lower.endswith("/api/generate"):
            return ("ollama", base)
        return ("ollama", base + "/api/generate")
    if fmt == "openai":
        if lower.endswith("/chat/completions"):
            return ("openai", base)
        if "/v1" in lower:
            return ("openai", base + "/chat/completions")
        return ("openai", base + "/v1/chat/completions")
    if fmt != "auto":
        raise ValueError(f"Invalid chat_api_format '{cfg.chat_api_format}' — use 'auto', 'ollama', or 'openai'")

    # 2-3. Full endpoint path already specified
    if lower.endswith("/api/generate"):
        return ("ollama", base)
    if lower.endswith("/chat/completions"):
        return ("openai", base)

    # 4. /v1 in URL -> OpenAI-compatible
    if "/v1" in lower:
        return ("openai", base + "/chat/completions")

    # 5. Ends with /api -> Ollama
    if lower.endswith("/api"):
        return ("ollama", base + "/generate")

    # 6. Ollama default port -> Ollama native
    if ":11434" in lower:
        return ("ollama", base + "/api/generate")

    # 7. Bare host -> OpenAI-compatible (LiteLLM, vLLM, llama.cpp, etc.)
    return ("openai", base + "/v1/chat/completions")


def _is_sse_response(resp: httpx.Response) -> bool:
    """Check whether an HTTP response is Server-Sent Events (SSE).

    Some APIs ignore ``stream: false`` and return ``text/event-stream``
    anyway.  This detects that situation so we can parse the SSE body
    instead of failing with a JSON decode error.

    WHY check Content-Type (not first-byte heuristic): The Content-Type
    header is the canonical signal.  Checking for ``text/event-stream``
    with or without charset suffix covers all known SSE implementations.
    A first-byte heuristic (e.g. checking if body starts with ``data:``)
    would be fragile — some APIs prepend BOM or whitespace.
    """
    ct = resp.headers.get("content-type", "").lower()
    return ct == "text/event-stream" or ct.startswith("text/event-stream;")


def _extract_openai_text(msg_or_delta: dict) -> str:
    """Extract text from an OpenAI message or delta dict.

    Strategy A: prefer ``content``; fall back to ``reasoning_content``
    when ``content`` is empty or absent.  Some reasoning models
    (e.g. GLM, DeepSeek-R1) emit the actual answer in
    ``reasoning_content`` — either exclusively (when truncated by a
    small ``max_tokens``) or interleaved with ``content``.  Using it as
    a fallback ensures we still return useful text in those cases
    rather than an empty string.

    WHY fallback to reasoning_content: The OpenAI API spec reserves
    ``reasoning_content`` for chain-of-thought output.  Some model
    providers repurpose it for the final answer when ``max_tokens``
    is set too low for the thinking phase + answer.  Without this
    fallback, the response would be silently empty — confusing for
    the operator who sees no error but no output either.
    """
    content = msg_or_delta.get("content")
    if content:
        return content
    return msg_or_delta.get("reasoning_content") or ""


def _accumulate_openai_sse(resp: httpx.Response) -> str:
    """Parse a completed OpenAI SSE response body and extract content.

    Handles the case where the API returned ``text/event-stream`` despite
    ``stream: false`` being requested (Plan A fallback).

    Each SSE line starting with ``data: `` contains a JSON chunk with
    ``choices[0].delta.content``.  The final ``data: [DONE]`` line marks
    the end of the stream.

    Args:
        resp: A completed ``httpx.Response`` whose body is SSE text.

    Returns:
        The accumulated text content from all delta chunks.

    Raises:
        ChatApiError: If no content was extracted from the SSE stream.
    """
    chunks: list[str] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or not line.startswith("data: "):
            continue
        data = line[6:]  # strip "data: " prefix
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
            delta = obj.get("choices", [{}])[0].get("delta", {})
            content = _extract_openai_text(delta)
            if content:
                chunks.append(content)
        except (json.JSONDecodeError, KeyError, IndexError):
            continue  # skip malformed chunks
    if not chunks:
        raise ChatApiError("SSE stream contained no content chunks")
    return "".join(chunks)


def _stream_openai_chat(
    prompt: str,
    cfg: LLMConfig,
    endpoint: str,
    headers: dict[str, str],
    payload: dict,
    *,
    temperature: float,
    num_predict: int | None,
) -> str:
    """Send a streaming chat request and accumulate the response (Plan B).

    Uses ``httpx.stream()`` to consume SSE chunks as they arrive.  This
    keeps the HTTP connection alive with continuous data flow, avoiding
    reverse-proxy idle timeouts (nginx 60s, Cloudflare 100s).

    Args:
        prompt: The full prompt text (for debug logging).
        cfg: LLM configuration.
        endpoint: Full endpoint URL.
        headers: HTTP headers (Content-Type, Authorization).
        payload: Base payload dict (will have stream set to True).
        temperature: Sampling temperature.
        num_predict: Max tokens (for debug logging only — already in payload).

    Returns:
        The accumulated text response.

    Raises:
        ChatApiError: On network, API, or parse failure.
    """
    payload["stream"] = True
    t0 = time.monotonic()
    chunks: list[str] = []
    try:
        with httpx.stream("POST", endpoint, json=payload, headers=headers, timeout=cfg.timeout) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                line = line.strip() if line else ""
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    content = _extract_openai_text(delta)
                    if content:
                        chunks.append(content)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
    except httpx.ConnectError as e:
        raise ChatApiError(f"Cannot connect to chat API at {endpoint}: {e}") from e
    except httpx.TimeoutException:
        raise ChatApiError(f"Chat API request timed out after {cfg.timeout:.0f}s") from None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ChatApiError(
                f"Model '{cfg.model}' not found at {endpoint}. HTTP 404 — check model name and API endpoint."
            ) from e
        raise ChatApiError(f"Chat API HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except OllamaError:
        raise
    except Exception as e:
        raise ChatApiError(str(e)) from e

    if not chunks:
        raise ChatApiError("Streaming chat API returned no content chunks")

    response_text = "".join(chunks)
    if cfg.debug_log:
        _write_debug_log(
            cfg.debug_log,
            {
                "ts": datetime.now(UTC).isoformat(),
                "model": cfg.model,
                "endpoint": endpoint,
                "latency_s": round(time.monotonic() - t0, 2),
                "prompt": prompt,
                "response": response_text,
                "streamed": True,
            },
        )
    return response_text


def call_openai_chat(
    prompt: str,
    cfg: LLMConfig,
    endpoint: str,
    *,
    temperature: float = 0.0,
    num_predict: int | None = None,
) -> str:
    """Send a chat request to an OpenAI-compatible endpoint.

    Does NOT use ``ollama_guard`` (no local GPU contention with cloud APIs).
    Does NOT send ``keep_alive`` / ``num_ctx`` (Ollama-specific parameters).
    Does NOT auto-pull on 404 (cloud APIs don't support model pulling).

    WHY no ollama_guard: The semaphore protects a local Ollama process
    from GPU saturation.  Cloud APIs are multi-tenant and designed for
    concurrent requests — serializing them would add unnecessary latency.

    WHY no auto-pull: Cloud APIs report 404 for invalid model names or
    missing deployments.  Auto-pulling would be meaningless (there is no
    ``ollama pull`` equivalent for OpenAI/DeepSeek/vLLM).  The operator
    should fix the model name in config.

    Streaming behavior:

    * When ``cfg.stream`` is True (Plan B): sends ``stream: true`` and
      consumes SSE chunks via ``httpx.stream()`` — keeps the connection
      alive, avoiding reverse-proxy idle timeouts.
    * When ``cfg.stream`` is False (default): sends ``stream: false``. If
      the API returns SSE anyway (ignoring the flag), the response is
      auto-detected and parsed transparently (Plan A fallback).

    Args:
        prompt: The full prompt text to send.
        cfg: LLM configuration (chat_api_key, model, timeout, debug_log, stream).
        endpoint: Full endpoint URL (from ``detect_chat_endpoint``).
        temperature: Sampling temperature (0.0 = deterministic, default).
        num_predict: Maximum tokens to generate (None = model default).

    Returns:
        The model's text response.

    Raises:
        ChatApiError: On any network, API, or response-format failure.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.chat_api_key:
        headers["Authorization"] = f"Bearer {cfg.chat_api_key}"

    payload: dict = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    if num_predict is not None:
        payload["max_tokens"] = num_predict

    # Plan B: active streaming
    if cfg.stream:
        return _stream_openai_chat(
            prompt, cfg, endpoint, headers, payload,
            temperature=temperature, num_predict=num_predict,
        )

    # Non-streaming request (Plan A: SSE auto-detection below)
    t0 = time.monotonic()
    try:
        resp = httpx.post(endpoint, json=payload, headers=headers, timeout=cfg.timeout)
        resp.raise_for_status()

        # Plan A: detect SSE response even when stream:false was requested.
        # Some API backends (especially Ollama's /v1 endpoint) ignore the
        # stream flag and return text/event-stream regardless.  Detecting it
        # here avoids a JSON decode error — the SSE body is parsed as a
        # completed stream instead.
        if _is_sse_response(resp):
            log.debug("API returned SSE despite stream:false — parsing as SSE (Plan A fallback)")
            response_text = _accumulate_openai_sse(resp)
        else:
            message = resp.json()["choices"][0]["message"]
            response_text = _extract_openai_text(message)

        if cfg.debug_log:
            _write_debug_log(
                cfg.debug_log,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "model": cfg.model,
                    "endpoint": endpoint,
                    "latency_s": round(time.monotonic() - t0, 2),
                    "prompt": prompt,
                    "response": response_text,
                    "streamed": False,
                },
            )
        return response_text

    except httpx.ConnectError as e:
        raise ChatApiError(f"Cannot connect to chat API at {endpoint}: {e}") from e
    except httpx.TimeoutException:
        raise ChatApiError(f"Chat API request timed out after {cfg.timeout:.0f}s") from None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ChatApiError(
                f"Model '{cfg.model}' not found at {endpoint}. HTTP 404 — check model name and API endpoint."
            ) from e
        raise ChatApiError(f"Chat API HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except (KeyError, IndexError) as e:
        raise ChatApiError(f"Unexpected chat API response format: {e}") from e
    except OllamaError:
        raise
    except Exception as e:
        raise ChatApiError(str(e)) from e
