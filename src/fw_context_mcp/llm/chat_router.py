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
  via ``net.stream()``.  This keeps the HTTP connection alive with
  continuous data flow, avoiding reverse-proxy idle timeouts.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from .. import net
from ..config.settings import LLMConfig
from ..utils import is_fatal
from ._debug import _write_debug_log
from .client import ChatResult, Usage
from .errors import ChatApiError, ChatPromptTooLong, LLMError

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
    if cfg.provider == "chatgpt":
        # The ChatGPT plan: the Responses API of OpenAI, no URL of the profile.
        from .chatgpt_plan import RESPONSES_URL

        return "chatgpt", RESPONSES_URL
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


def chat_ollama_server(cfg: LLMConfig) -> str | None:
    """Return the URL of the Ollama server that runs the chat model.

    Without ``chat_api_base``, this is ``ollama_url``.  With it, the
    format of :func:`detect_chat_endpoint` decides.  For ``"ollama"``,
    the server is the endpoint without ``/api/generate``.  For
    ``"openai"``, the return value is ``None``: an OpenAI-compatible API
    runs the model, and no Ollama server holds it.

    WHY: the check and the pull of the chat model must use the server
    that the chat calls use.  A check of ``ollama_url`` gave the wrong
    answer when ``chat_api_base`` named a different server or a cloud API.

    Raises:
        ValueError: ``chat_api_format`` is not valid.
    """
    if not cfg.chat_api_base:
        return cfg.ollama_url
    fmt, endpoint = detect_chat_endpoint(cfg)
    if fmt != "ollama":
        return None
    # Each "ollama" endpoint of detect_chat_endpoint ends with /api/generate.
    return endpoint[: -len("/api/generate")]


def chat_on_ollama_url(cfg: LLMConfig) -> bool:
    """Return True when the chat model runs on the Ollama server of ``ollama_url``.

    WHY one function: the release of a model between the phases and the
    version check of a server both need this rule, and two copies of it
    can disagree about which server runs the chat.

    Raises:
        ValueError: ``chat_api_format`` is not valid (see
            :func:`chat_ollama_server`).
    """
    server = chat_ollama_server(cfg)
    return server is not None and server.rstrip("/") == cfg.ollama_url.rstrip("/")


def chat_api_key(cfg: LLMConfig) -> str | None:
    """Return the API key of the chat: ``chat_api_key`` when a caller set it, else the stored key of the profile.

    ``llm.secrets`` looks in ``api_key_env``, the keyring and
    ``secrets.toml``, then at the key of the provider (a profile of
    OpenRouter uses the key stored as ``openrouter``).  A config without a
    profile (a test, an old caller) has no stored key.  The model list of
    the provider (``llm.model_list``) sends the same key.
    """
    if cfg.chat_api_key:
        return cfg.chat_api_key
    if not cfg.profile:
        return None
    from .secrets import get_api_key

    return get_api_key(cfg.profile, cfg.api_key_env, _provider_of_key(cfg))


def _provider_of_key(cfg: LLMConfig) -> str:
    """Return the provider whose stored key applies to the profile of *cfg*, or empty.

    Only for the URL of the provider: a profile of OpenRouter with the URL
    of a proxy must not send the key of OpenRouter to the proxy.
    """
    from .providers import provider_spec

    spec = provider_spec(cfg.provider)
    if spec is None or (cfg.chat_api_base or "").rstrip("/") != spec.base_url.rstrip("/"):
        return ""
    return spec.name


#: The marks of a refusal for the context in the body of an HTTP 400: the
#: error code of the OpenAI API (OpenRouter gives it too), and the message
#: that OpenAI and DeepSeek give ("This model's maximum context length is
#: N tokens").  From reports of the error bodies (2026-10-22), not from a
#: probe: a probe needs a prompt larger than 128k tokens.  The whole body,
#: lower case: OpenRouter wraps the error of the host in metadata.raw.
_CONTEXT_REFUSALS = ("context_length_exceeded", "maximum context length")


def _is_context_refusal(body: str) -> bool:
    """Return True when the body of an HTTP 400 says that the prompt does not fit the context of the model."""
    text = body.lower()
    return any(mark in text for mark in _CONTEXT_REFUSALS)


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


def _openai_usage(answer: dict) -> Usage:
    """Return the token counts of an OpenAI-compatible answer (its ``usage`` object).

    The providers name the parts differently: OpenAI and OpenRouter give
    ``prompt_tokens_details.cached_tokens`` and
    ``completion_tokens_details.reasoning_tokens``; DeepSeek gives
    ``prompt_cache_hit_tokens``; OpenRouter gives ``cost`` (US dollars).
    A field that the answer does not carry is None.
    """
    usage = answer.get("usage")
    if not isinstance(usage, dict):
        return Usage()

    def number(obj: object, key: str) -> int | None:
        value = obj.get(key) if isinstance(obj, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    cached = number(usage.get("prompt_tokens_details"), "cached_tokens")
    if cached is None:
        cached = number(usage, "prompt_cache_hit_tokens")
    cost = usage.get("cost")
    return Usage(
        prompt_tokens=number(usage, "prompt_tokens"),
        cached_tokens=cached,
        completion_tokens=number(usage, "completion_tokens"),
        reasoning_tokens=number(usage.get("completion_tokens_details"), "reasoning_tokens"),
        cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


class OpenAIChatClient:
    """The chat client of an OpenAI-compatible ``/chat/completions`` endpoint.

    No ``ollama_guard``, no ``keep_alive`` and ``num_ctx``, no auto-pull:

    * WHY no ollama_guard: the semaphore protects a local Ollama process
      from GPU saturation.  A cloud API is multi-tenant and made for
      concurrent requests; to serialize them only adds latency.
    * WHY no num_ctx: the API has no context per request.  It also cannot
      count the tokens of a prompt that it refuses, thus ``counts_tokens``
      is False.
    * WHY no auto-pull: a cloud API reports 404 for a wrong model name or a
      missing deployment, and it has no ``ollama pull``.

    Streaming (when ``cfg.stream`` is True, Plan B): ``net.stream()`` with
    SSE chunks.  Without it, the request sends ``stream: false``; an answer
    that streams anyway is parsed as SSE (Plan A).
    """

    counts_tokens = False

    def __init__(self, cfg: LLMConfig, endpoint: str) -> None:
        from .providers import provider_spec

        self.cfg = cfg
        self.endpoint = endpoint
        self.spec = provider_spec(cfg.provider)
        # True after the model refused to turn the reasoning off: the next
        # requests ask for the lowest reasoning at once, not for a refusal.
        self._low_reasoning = False

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
    ) -> ChatResult:
        """Send *prompt* as one user message and return the answer and its usage.

        *num_ctx* is ignored: the API has no context per request.

        Raises:
            ChatApiError: On any network, API, or answer-format failure.
        """
        cfg = self.cfg
        headers: dict[str, str] = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload: dict = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        t0 = time.monotonic()
        result = self._send(headers, payload)
        if cfg.debug_log:
            _write_debug_log(
                cfg.debug_log,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "model": cfg.model,
                    "endpoint": self.endpoint,
                    "latency_s": round(time.monotonic() - t0, 2),
                    "prompt": prompt,
                    "response": result.text,
                    "streamed": bool(cfg.stream),
                },
            )
        return result

    def _send(self, headers: dict[str, str], payload: dict) -> ChatResult:
        """Send *payload* with the fields of the provider; after a refusal of the reasoning field, send it again.

        Some models of OpenRouter cannot turn the reasoning off (gpt-oss):
        the API replies HTTP 400 and names the reasoning.  Such a model gets
        the lowest reasoning without its text (``reasoning_low``), for this
        request and each later one of this client.  The flag is not shared
        between threads with a lock: a client is made for each call
        (``call_llm``) and the analysis sends its requests one by one, thus
        no two threads use one client.
        """
        spec = self.spec
        if spec is not None:
            payload.update(spec.request_fields(self.cfg.routing, low_reasoning=self._low_reasoning))
        try:
            return self._stream(headers, payload) if self.cfg.stream else self._post(headers, payload)
        except ChatApiError as exc:
            # The whole body: OpenRouter wraps the error of the host in
            # metadata.raw, and the word can be after the part that a
            # message shows.  "reasoning", not "reason": a 400 about
            # finish_reason turned the reasoning on for the whole run.
            refused = (exc.status_code == 400 and "reasoning" in exc.body.lower()
                       and not isinstance(exc, ChatPromptTooLong))
            if spec is None or not spec.reasoning_low or self._low_reasoning or not refused:
                raise
            # A warning: the reasoning tokens are billed as output.
            log.warning(
                "Model %s refused to turn the reasoning off: it gets the lowest reasoning, which is billed as output",
                self.cfg.model,
            )
            self._low_reasoning = True
            for key in spec.reasoning_off:
                payload.pop(key, None)
            payload.update(spec.request_fields(self.cfg.routing, low_reasoning=True))
            return self._stream(headers, payload) if self.cfg.stream else self._post(headers, payload)

    def _post(self, headers: dict[str, str], payload: dict) -> ChatResult:
        """One non-streaming request (Plan A: an SSE answer is parsed as a stream).

        Some backends (Ollama's own ``/v1`` layer in particular) ignore the
        stream flag and answer ``text/event-stream``; the detection here
        avoids a JSON decode error.
        """
        try:
            resp = net.post(self.endpoint, json=payload, headers=headers, timeout=self.cfg.timeout)
            resp.raise_for_status()
            if _is_sse_response(resp):
                log.debug("API returned SSE despite stream:false — parsing as SSE (Plan A fallback)")
                return ChatResult(_accumulate_openai_sse(resp))
            answer = resp.json()
            return ChatResult(_extract_openai_text(answer["choices"][0]["message"]), _openai_usage(answer))
        except (KeyError, IndexError) as e:
            raise ChatApiError(f"Unexpected chat API response format: {e}") from e
        except LLMError:
            raise
        except Exception as e:
            if is_fatal(e):
                raise
            raise self._error(e) from (None if isinstance(e, httpx.TimeoutException) else e)

    def _stream(self, headers: dict[str, str], payload: dict) -> ChatResult:
        """One streaming request (Plan B).

        ``net.stream()`` keeps the connection busy with continuous data,
        thus a reverse proxy (nginx 60 s, Cloudflare 100 s) does not close
        it as idle during a long answer.  A chunk with a ``usage`` object
        gives the usage.
        """
        payload["stream"] = True
        chunks: list[str] = []
        usage = Usage()
        try:
            with net.stream(
                "POST", self.endpoint, json=payload, headers=headers, timeout=self.cfg.timeout
            ) as resp:
                # The body of an error answer, before the error: _error
                # reads it, and httpx raised ResponseNotRead for a stream.
                if resp.status_code >= 400:
                    resp.read()
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
                        if isinstance(obj.get("usage"), dict):
                            usage = _openai_usage(obj)
                        content = _extract_openai_text(obj.get("choices", [{}])[0].get("delta", {}))
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if content:
                        chunks.append(content)
        except LLMError:
            raise
        except Exception as e:
            if is_fatal(e):
                raise
            raise self._error(e) from (None if isinstance(e, httpx.TimeoutException) else e)
        if not chunks:
            raise ChatApiError("Streaming chat API returned no content chunks")
        return ChatResult("".join(chunks), usage)

    def _api_key(self) -> str | None:
        """Return the API key: ``chat_api_key`` when a caller set it, else the stored key of the profile.

        ``llm.secrets`` looks in ``api_key_env``, the keyring and
        ``secrets.toml``, then at the key of the provider (a profile of
        OpenRouter uses the key stored as ``openrouter``).  A config
        without a profile (a test, an old caller) has no stored key.
        """
        return chat_api_key(self.cfg)

    def _provider_of_key(self) -> str:
        return _provider_of_key(self.cfg)

    def _error(self, e: Exception) -> ChatApiError:
        """Return the ``ChatApiError`` with a sentence for the operator for the failure *e* of a request.

        The caller re-raises a fatal exception (``is_fatal``) and an
        ``LLMError`` as they are, and suppresses the context of a timeout
        (``from None``), as the Ollama client does.
        """
        if isinstance(e, httpx.ConnectError):
            return ChatApiError(f"Cannot connect to chat API at {self.endpoint}: {e}")
        if isinstance(e, httpx.TimeoutException):
            return ChatApiError(f"Chat API request timed out after {self.cfg.timeout:.0f}s")
        if isinstance(e, httpx.HTTPStatusError):
            status_code = e.response.status_code
            if status_code == 404 and self.spec is not None and self.spec.options:
                # OpenRouter: no endpoint of the model matches the routing
                # (a quantization filter for a model that runs fp4 only).
                return ChatApiError(
                    f"{self.spec.label} has no endpoint for model '{self.cfg.model}' that matches the routing "
                    f"{self.spec.request_fields(self.cfg.routing).get('provider')} (HTTP 404: "
                    f"{e.response.text[:200]}). Change the model, or [llm.profiles.{self.cfg.profile}.routing] "
                    "(for example quantizations = [])",
                    status_code=status_code,
                )
            if status_code == 404:
                return ChatApiError(
                    f"Model '{self.cfg.model}' not found at {self.endpoint}. "
                    "HTTP 404 — check model name and API endpoint.",
                    status_code=status_code,
                )
            if e.response.status_code in (401, 403):
                # The text of the server stays out: some servers repeat a
                # part of the key that they refused.
                status = f"Chat API HTTP {e.response.status_code}: {self.endpoint} refused the API key"
                if not self.cfg.profile or self.cfg.chat_api_key:
                    return ChatApiError(f"{status} that the caller gave, or got none", status_code=status_code)
                # The cached key goes: another process can have stored a new one.
                from .secrets import forget

                forget(self.cfg.profile)
                shared = self._provider_of_key()
                return ChatApiError(
                    f"{status} of LLM profile {self.cfg.profile!r}, or got none. Store the key with "
                    f"'fw-context llm key set {self.cfg.profile}'"
                    + (f" (or 'fw-context llm key set provider:{shared}' for each profile of {shared})" if shared else "")
                    + (f", or set the environment variable {self.cfg.api_key_env}" if self.cfg.api_key_env else ""),
                    status_code=status_code,
                )
            if status_code == 400 and _is_context_refusal(e.response.text):
                return ChatPromptTooLong(
                    f"Chat API HTTP 400: the prompt and the answer budget do not fit the context of "
                    f"'{self.cfg.model}': {e.response.text[:200]}",
                    status_code=status_code, body=e.response.text,
                )
            return ChatApiError(
                f"Chat API HTTP {status_code}: {e.response.text[:200]}", status_code=status_code, body=e.response.text,
            )
        return ChatApiError(str(e))

    def count_tokens(self, prompt: str, num_ctx: int) -> int:
        """An OpenAI-compatible API gives no count of the tokens of a prompt.

        Raises:
            ChatApiError: Always.
        """
        raise ChatApiError("an OpenAI-compatible chat endpoint gives no count of the tokens of a prompt")

    def trained_context(self) -> int | None:
        """None: this client counts no tokens.  The analysis reads the context from ``llm.model_list``."""
        return None

    def fit_context(self, lower: int, upper: int, *, check: bool) -> None:
        """None: an API has no GPU of fw-context, thus its context is not a question of memory."""
        return None

    def pull_model(self) -> None:
        """An OpenAI-compatible API cannot install a model.

        Raises:
            ChatApiError: Always.
        """
        raise ChatApiError("an OpenAI-compatible chat endpoint has no pull")
