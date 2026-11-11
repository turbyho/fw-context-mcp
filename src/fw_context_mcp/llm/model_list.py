"""The model list of the provider of an API or ChatGPT profile: the names of the models and their context.

WHY (decisions of the user, 2026-10-22): the context of the chat model
must be known, because a prompt and the answer budget must fit it; and the
names of the models come from the list of the provider, not from a fixed
name in the code.  A static ``context`` of the profile applies only to a
model whose list gives none (an ``openai_compatible`` API): the user gives
it only where the API does not.  The list of the provider has priority.

The sources, each measured 2026-10-22:

* DeepSeek: ``GET <url>/models`` with the key, ``context_window`` of each
  model (1048576).
* OpenRouter: ``GET <url>/models`` for the names; the context is the
  smallest ``context_length`` of ``GET <url>/models/<id>/endpoints``.  WHY
  the smallest: the endpoints of one model differ (deepseek-chat: 163840
  and 128000), and the routing selects one; the prompt must fit each.
* ChatGPT plan: the model list of the plan, ``context_window``
  (``chatgpt_plan.model_contexts``).
* openai_compatible: ``GET <url>/models`` for the names (the OpenAI
  format); no context.  A server without that list gives no names, and
  the static ``context`` alone applies.

Ollama is not here: ``/api/tags`` and ``/api/show`` of the Ollama client
give its names and the context of each model.

A list that was read stays for ``_CACHE_SECONDS``: a run and the setup ask
some times in a minute, and the MCP server runs for days, during which a
provider adds models.  A list that cannot be read is not kept: the next
call asks again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .. import net
from ..config.settings import LLMConfig
from .errors import LLMError

#: The providers of a profile whose model list this module reads.
LIST_PROVIDERS = ("deepseek", "openrouter", "chatgpt", "openai_compatible")

#: How long a list that was read stays in this process.  WHY not for the
#: life of the process: a model that the provider added after the start of
#: the MCP server, and that ``fw-context llm setup`` then selected, was "not
#: in the model list" until the client restarted.
_CACHE_SECONDS = 600.0

#: The lists of this process: (provider, URL, profile) → (time of the read, the models).
_LISTS: dict[tuple[str, str, str], tuple[float, list[ModelInfo]]] = {}
#: The smallest endpoint context of each OpenRouter model: (URL, model) → (time, tokens or None).
_ENDPOINT_CONTEXTS: dict[tuple[str, str], tuple[float, int | None]] = {}


@dataclass(frozen=True)
class ModelInfo:
    """One model of a list: its name for a request, and its context in tokens (None: the list gives none)."""

    name: str
    context: int | None


class ModelListError(LLMError):
    """The model list of the provider cannot be read: the network, the key, or an answer without models."""


class ModelContextError(LLMError):
    """The context of the chat model is not known: the model is not in the list, or nothing gives its context.

    The message tells the user what to change.  The analysis does not run
    without the context: a guess would let a prompt that does not fit reach
    the model, or skip a prompt that fits.  *not_listed* is True when the
    list was read and does not hold the model: a static ``context`` does
    not repair that, a model of the list does.  *unreadable* is True when
    the list or the endpoints of the model cannot be read (the network, the
    key): a static ``context`` does not repair that either, except for an
    ``openai_compatible`` API, which need not have a list.
    """

    def __init__(self, message: str, *, not_listed: bool = False, unreadable: bool = False) -> None:
        super().__init__(message)
        self.not_listed = not_listed
        self.unreadable = unreadable


def provider_models(llm: LLMConfig) -> list[ModelInfo]:
    """Return the models of the provider of the chat profile of *llm*.

    Raises:
        ModelListError: The list cannot be read.
        ValueError: The provider has no model list here (``ollama``).
    """
    if llm.provider not in LIST_PROVIDERS:
        raise ValueError(f"the provider {llm.provider!r} has no model list in llm.model_list")
    key = (llm.provider, _base(llm), llm.profile)
    cached = _LISTS.get(key)
    if cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
        models = _chatgpt_models(llm) if llm.provider == "chatgpt" else _api_models(llm)
        cached = _LISTS[key] = (time.monotonic(), models)
    return cached[1]


def model_context(llm: LLMConfig) -> int:
    """Return the context of the chat model of *llm* in tokens: from the model list, else the static ``context``.

    Raises:
        ModelContextError: The model is not in the list, the list cannot be
            read and the profile has no static ``context``, or neither gives
            a context.
    """
    profile, model = llm.profile or llm.provider, llm.model
    if llm.provider not in LIST_PROVIDERS:
        # A config without an API profile (a test, an older caller): no
        # list to read, and only a profile has the key context.
        if llm.context > 0:
            return llm.context
        raise ModelContextError(
            f"the context of model {model!r} is not known: the chat API of provider {llm.provider!r} has no "
            "model list; select an LLM profile of an API with 'fw-context llm setup'"
        )
    # An openai_compatible API with a static context: the user gave the
    # context, because the API gives none.  Its list need not exist, and
    # need not hold the name of a request (llama-server lists the name of
    # its model file and runs it for each name).
    static_only = llm.provider == "openai_compatible" and llm.context > 0
    try:
        models = {info.name: info for info in provider_models(llm)}
    except ModelListError as exc:
        if static_only:
            return llm.context
        raise ModelContextError(f"the context of model {model!r} of LLM profile {profile!r} is not known: {exc}",
                                unreadable=True) from exc
    if models and model not in models:
        if static_only:
            return llm.context
        raise ModelContextError(
            f"the model {model!r} of LLM profile {profile!r} is not in the model list of its provider "
            f"({_names(models)}): select a model with 'fw-context llm setup'",
            not_listed=True,
        )
    context = models[model].context if model in models else None
    if llm.provider == "openrouter" and model in models:
        # The endpoints give the context that applies; the context of the
        # list only when no endpoint gives one.
        context = _endpoint_context(llm) or context
    if context:
        return context
    if llm.context > 0:
        return llm.context
    raise ModelContextError(
        f"the API of LLM profile {profile!r} gives no context of model {model!r}: set context (the tokens of "
        f"the model) in [llm.profiles.{profile}] of the global config"
    )


def _base(llm: LLMConfig) -> str:
    """Return the URL that the model list of the API is under: the chat endpoint without ``/chat/completions``.

    WHY the endpoint of the chat and not ``chat_api_base``: the chat client
    adds ``/v1`` to a URL without it (``detect_chat_endpoint``), and the
    list must come from the API that runs the chat.  Measured 2026-10-22:
    ``https://api.deepseek.com/v1/models`` and
    ``https://openrouter.ai/api/v1/models`` answer.  The ChatGPT plan has
    its own list (``chatgpt_plan.model_contexts``): its base is the URL.
    """
    from .chat_router import detect_chat_endpoint

    if llm.provider == "chatgpt":
        return (llm.chat_api_base or "").rstrip("/")
    try:
        _fmt, endpoint = detect_chat_endpoint(llm)
    except ValueError:
        endpoint = (llm.chat_api_base or "").rstrip("/")
    return endpoint[: -len("/chat/completions")] if endpoint.endswith("/chat/completions") else endpoint


def _get(llm: LLMConfig, url: str) -> dict:
    """Send a GET with the key of the chat profile; return the JSON object of the answer.

    Raises:
        ModelListError: No answer, an HTTP error, or an answer that is not a JSON object.
    """
    from .chat_router import chat_api_key

    headers = {}
    key = chat_api_key(llm)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = net.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401 and llm.profile and not llm.chat_api_key:
            # As the chat client: the cached key goes, thus a key that the
            # user stored after the start of the process applies next time.
            from .secrets import forget

            forget(llm.profile)
        # The status only: the body of an error can echo the key.
        raise ModelListError(f"{url} gives HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelListError(f"{url} does not answer with a model list ({type(exc).__name__})") from None
    if not isinstance(body, dict):
        raise ModelListError(f"{url} does not answer with a model list")
    return body


def _api_models(llm: LLMConfig) -> list[ModelInfo]:
    """Return the models of ``GET <url>/models`` of an OpenAI-compatible API (DeepSeek, OpenRouter, another)."""
    url = f"{_base(llm)}/models"
    data = _get(llm, url).get("data")
    if not isinstance(data, list):
        raise ModelListError(f"{url} gives no list of models")
    models = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        # DeepSeek: context_window; OpenRouter: context_length (the
        # endpoints give the context that applies, see _endpoint_context).
        value = entry.get("context_window", entry.get("context_length"))
        models.append(ModelInfo(str(entry["id"]), tokens(value)))
    return models


def tokens(value: object) -> int | None:
    """Return *value* as a context in tokens: a positive integer, not a bool (``True`` is an int); else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _chatgpt_models(llm: LLMConfig) -> list[ModelInfo]:
    """Return the models of the ChatGPT plan of the profile, with ``context_window``."""
    from .chatgpt_plan import model_contexts

    try:
        contexts = model_contexts(llm.profile)
    except LLMError as exc:
        raise ModelListError(str(exc)) from exc
    return [ModelInfo(name, context) for name, context in contexts.items()]


def _endpoint_context(llm: LLMConfig) -> int | None:
    """Return the smallest context of the endpoints of the OpenRouter model of *llm*, or None when none gives one.

    Raises:
        ModelContextError: The endpoint list cannot be read.
    """
    key = (_base(llm), llm.model)
    cached = _ENDPOINT_CONTEXTS.get(key)
    if cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
        url = f"{_base(llm)}/models/{llm.model}/endpoints"
        try:
            data = _get(llm, url).get("data")
        except ModelListError as exc:
            raise ModelContextError(f"the endpoints of model {llm.model!r} cannot be read: {exc}",
                                    unreadable=True) from exc
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        found = [tokens(entry.get("context_length")) for entry in endpoints or [] if isinstance(entry, dict)]
        contexts = [value for value in found if value is not None]
        cached = _ENDPOINT_CONTEXTS[key] = (time.monotonic(), min(contexts) if contexts else None)
    return cached[1]


def _names(models: dict[str, ModelInfo]) -> str:
    """Return the names of a list for a message: all of a short list, the first ones of a long list."""
    names = sorted(models)
    return ", ".join(names) if len(names) <= 12 else f"{', '.join(names[:12])} and {len(names) - 12} more"
