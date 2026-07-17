"""Auto-detecting chat router: Ollama native vs OpenAI-compatible.

When ``LLMConfig.chat_api_base`` is set, chat calls are routed to an
external endpoint instead of the local Ollama ``/api/generate``.  The
API format (Ollama native or OpenAI-compatible) is auto-detected from
the URL, or can be forced via ``chat_api_format``.

Supported OpenAI-compatible backends include: OpenAI, DeepSeek, vLLM,
llama.cpp, LM Studio, LiteLLM proxy, and Ollama's own OpenAI layer.

Embedding calls always go to local Ollama (``/api/embed``) — this module
only handles chat/generation.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx

from ..config.settings import LLMConfig
from .ollama import OllamaError, _write_debug_log

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

    Args:
        prompt: The full prompt text to send.
        cfg: LLM configuration (chat_api_key, model, timeout, debug_log).
        endpoint: Full endpoint URL (from ``detect_chat_endpoint``).
        temperature: Sampling temperature (0.0 = deterministic, default).
        num_predict: Maximum tokens to generate (None = model default).

    Returns:
        The model's text response.

    Raises:
        OllamaError: On any network, API, or response-format failure.
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

    t0 = time.monotonic()
    try:
        resp = httpx.post(endpoint, json=payload, headers=headers, timeout=cfg.timeout)
        resp.raise_for_status()
        response_text = resp.json()["choices"][0]["message"]["content"]

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
                },
            )
        return response_text

    except httpx.ConnectError as e:
        raise OllamaError(f"Cannot connect to chat API at {endpoint}: {e}") from e
    except httpx.TimeoutException:
        raise OllamaError(f"Chat API request timed out after {cfg.timeout:.0f}s") from None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise OllamaError(
                f"Model '{cfg.model}' not found at {endpoint}. HTTP 404 — check model name and API endpoint."
            ) from e
        raise OllamaError(f"Chat API HTTP {e.response.status_code}: {e.response.text[:200]}") from e
    except (KeyError, IndexError) as e:
        raise OllamaError(f"Unexpected chat API response format: {e}") from e
    except OllamaError:
        raise
    except Exception as e:
        raise OllamaError(str(e)) from e
