"""One interface for each chat backend: send a prompt, get the text and the usage.

Each backend (a local or a remote Ollama, an OpenAI-compatible API) is a
class with the same methods (:class:`ChatClient`).  :func:`get_chat_client`
selects the class from the configuration; a caller does not test which
backend it has.

WHY an interface instead of one function with branches: before, the
function ``call_ollama`` sent the request to Ollama or, when
``chat_api_base`` was set, to an OpenAI-compatible API.  The LLM analysis
then asked a second function (``generate_endpoint``) whether the backend
was Ollama, to know whether it can count tokens.  Each new backend added a
branch in each of these places.  A class per backend keeps the request,
the answer format and the capabilities of the backend in one place.

``ChatResult.usage`` gives the token counts (and the cost when the
provider reports it) of one request.  The Ollama answer and most
OpenAI-compatible answers carry them; a value that the answer does not
carry is ``None``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config.settings import LLMConfig


@dataclass(frozen=True)
class Usage:
    """The token counts of one chat request, as the backend reports them.

    ``None`` means that the answer does not carry the value; ``0`` is a
    reported zero.  ``cached_tokens`` is the part of ``prompt_tokens``
    that the backend took from its prompt cache.  ``cost`` is in US
    dollars, and only a provider that reports it gives it.
    """

    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost: float | None = None


@dataclass(frozen=True)
class ChatResult:
    """The answer of one chat request: its text and its usage."""

    text: str
    usage: Usage = field(default_factory=Usage)


class ChatClient(Protocol):
    """A chat backend.

    ``counts_tokens`` is True when the backend counts the tokens of a
    prompt with the tokenizer of its model (:meth:`count_tokens`) and
    refuses a prompt that is longer than the context, instead of cutting
    it.  The LLM analysis puts each prompt into a context that holds it
    only when this is True.
    """

    counts_tokens: bool

    def generate(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
    ) -> ChatResult:
        """Send *prompt* and return the answer.

        *max_tokens* is the largest answer (None: the default of the
        model).  *num_ctx* is the context of the request; a backend that
        has no context per request ignores it.

        Raises:
            LLMError: The request failed.  ``OllamaPromptTooLong`` when the
                prompt does not fit *num_ctx*.
        """
        ...

    def count_tokens(self, prompt: str, num_ctx: int) -> int:
        """Return the number of tokens of *prompt*.

        Raises:
            LLMError: The backend cannot count (``counts_tokens`` is
                False), or the request failed.
        """
        ...

    def trained_context(self) -> int | None:
        """Return the context that the model was trained for, or None when it is not known."""
        ...

    def pull_model(self) -> None:
        """Install the model on the backend.

        Raises:
            LLMError: The backend cannot install a model, or the pull failed.
        """
        ...


def get_chat_client(cfg: LLMConfig) -> ChatClient:
    """Return the chat client for *cfg*.

    Without ``chat_api_base``: the Ollama server at ``ollama_url``.  With
    it: the format that :func:`~.chat_router.detect_chat_endpoint`
    finds — an Ollama server (it can be a remote one), or an
    OpenAI-compatible API.

    Raises:
        ValueError: ``chat_api_format`` is not a known format.
    """
    from .chat_router import OpenAIChatClient, chat_ollama_server, detect_chat_endpoint
    from .ollama import OllamaClient

    if not cfg.chat_api_base:
        return OllamaClient(cfg, cfg.ollama_url.rstrip("/") + "/api/generate", cfg.ollama_url)
    fmt, endpoint = detect_chat_endpoint(cfg)
    if fmt == "openai":
        return OpenAIChatClient(cfg, endpoint)
    # WHY not the raw chat_api_base as the server: the pull went to
    # ".../api/api/pull" for ".../api", and to ".../api/generate/api/pull"
    # for ".../api/generate".
    return OllamaClient(cfg, endpoint, chat_ollama_server(cfg) or cfg.chat_api_base)


def call_llm(
    prompt: str,
    cfg: LLMConfig,
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    num_ctx: int | None = None,
) -> str:
    """Send *prompt* to the chat backend of *cfg* and return the text of the answer.

    For a caller that needs only the text.  A caller that needs the usage
    or the capabilities of the backend uses :func:`get_chat_client`.

    Raises:
        LLMError: The request failed.
    """
    result = get_chat_client(cfg).generate(prompt, temperature=temperature, max_tokens=max_tokens, num_ctx=num_ctx)
    return result.text


async def call_llm_async(
    prompt: str,
    cfg: LLMConfig,
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """:func:`call_llm` in a worker thread, for an async MCP handler.

    WHY a thread: the request blocks for 10 to 60 s, and the event loop of
    the MCP server must keep serving the other requests.
    """
    return await asyncio.to_thread(call_llm, prompt, cfg, temperature=temperature, max_tokens=max_tokens)
