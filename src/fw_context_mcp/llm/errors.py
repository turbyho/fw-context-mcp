"""Errors of the chat backends of fw-context.

``LLMError`` is the base of each failure of a chat backend: a local or a
remote Ollama, and an API of a provider.  A caller that only reports the
failure catches ``LLMError``; a caller that acts on one cause (the model is
not installed, the prompt is too long) catches the subclass.

WHY a base outside ``ollama.py``: before, the base was ``OllamaError``, and
the error of an external API (``ChatApiError``) derived from it so that the
handlers caught it.  A handler of "the chat failed" then named one backend,
and each new backend had to pretend that it is Ollama.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """A chat request failed: network, HTTP, the answer, or the backend."""


class LLMStopRun(LLMError):
    """The backend refuses each request now: a run of the analysis stops, and a later run continues.

    A usage limit of a plan, or a sign-in that is not valid any more: each
    of the next thousand symbols would fail the same way, three times.
    """


class ModelDoesNotFit(LLMStopRun):
    """The model does not load with the smallest context of the analysis: the memory is not sufficient.

    A run of the analysis stops (``LLMStopRun``): each symbol would load
    the model three times, and each load would fail the same way.
    """


class ChatApiError(LLMError):
    """A request to an OpenAI-compatible chat API failed.

    Raised for HTTP, network and answer-format failures of the API of a
    provider (OpenAI, OpenRouter, DeepSeek, LiteLLM, vLLM, ...).
    *status_code* is the HTTP status of a reply that is not a success, else
    None: the client acts on a 400 that refuses a field of the request.
    *body* is the whole body of that reply, for that decision only; the
    message shows a part of it.
    """

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ChatPromptTooLong(ChatApiError):
    """The API refused the request: the prompt and ``max_tokens`` do not fit the context of the model.

    The API checks the rule itself (HTTP 400), because the client cannot
    count the tokens of a prompt.  The analysis then marks the symbol
    ``skip:toolarge``, as for Ollama: the same request fails again, thus
    a retry, and a new failure at each run, only cost time and money.
    """
