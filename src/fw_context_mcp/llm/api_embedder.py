"""Embeddings through an OpenAI-compatible API: OpenRouter, or another API with ``POST <url>/embeddings``.

WHY (decision of the user, 2026-10-22): on a machine without a GPU, or
with little memory, an external service is the only way to embed a
project, as for the LLM analysis.  The profile of ``[llm] embed_profile``
names the server and the key; ``[llm] embed_model`` names the model.  The
code of each symbol and each search query then go to that host: the
selection of the profile is the consent.

The request is the one of the OpenAI API: ``{"model", "input": [...]}``,
the answer ``{"data": [{"index", "embedding"}]}``.  OpenRouter also gets
``provider`` (``LLMConfig.embed_routing``): by default only hosts that keep
no data (``zdr``).  Measured 2026-10-22 on OpenRouter: such a host of
gemini-embedding-2 takes one input only, and gives HTTP 404 for a batch;
the embedder then sends one input for each request.
"""

from __future__ import annotations

import logging

import httpx

from .. import net
from ..config.settings import LLMConfig
from .embedder import Embedder, EmbedderContextUnknown, EmbedderError, EmbedderInputTooLong

log = logging.getLogger(__name__)

#: The context of each model of an OpenRouter model list, for this process:
#: ``max_tokens`` reads the list one time.  URL → model → tokens.
_CONTEXTS: dict[str, dict[str, int]] = {}


class ApiEmbedder(Embedder):
    """An embedding model of an API profile (``LLMConfig.embed_provider`` not ``"ollama"``)."""

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._dim: int | None = None
        # True after a batch got "no endpoint" and one input passed.
        self._one_input = False

    def _key(self) -> str | None:
        """Return the API key of the profile of the embeddings (``llm.secrets``); None: no key."""
        from .providers import provider_spec
        from .secrets import get_api_key

        cfg = self._cfg
        spec = provider_spec(cfg.embed_provider)
        # The key of the provider only for its own URL (as the chat).
        shared = spec.name if spec is not None and cfg.embed_api_base.rstrip("/") == spec.base_url.rstrip("/") else ""
        return get_api_key(cfg.embed_profile, cfg.embed_api_key_env, shared)

    def _post(self, texts: list[str]) -> list[list[float]]:
        """Send one request; return the vectors in the order of *texts*.

        Raises:
            EmbedderInputTooLong: The API names an input longer than the context.
            EmbedderError: Any other failure of the request or of the answer.
        """
        cfg = self._cfg
        payload: dict[str, object] = {"model": cfg.embed_model, "input": texts}
        if cfg.embed_routing:
            payload["provider"] = dict(cfg.embed_routing)
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = cfg.embed_api_base.rstrip("/") + "/embeddings"
        try:
            # Twice the timeout of the profile, as the embeddings of Ollama:
            # a batch of long bodies takes longer than one chat answer.
            resp = net.post(url, json=payload, headers=headers, timeout=cfg.timeout * 2)
        except httpx.HTTPError as exc:
            raise ApiEmbedderError(f"the embedding API at {url} does not answer: {exc}") from exc
        if resp.status_code != 200:
            text = _error_message(resp)
            if resp.status_code == 400 and ("context" in text.lower() or "too long" in text.lower()):
                raise EmbedderInputTooLong(f"an input is longer than the context of {cfg.embed_model}: {text}")
            raise _HttpError(resp.status_code, f"the embedding API at {url} gives HTTP {resp.status_code}: {text}")
        try:
            data = resp.json()["data"]
            vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiEmbedderError(f"the embedding API at {url} gives an answer without vectors: {exc}") from exc
        if len(vectors) != len(texts):
            raise ApiEmbedderError(f"the embedding API at {url} gives {len(vectors)} vectors for {len(texts)} inputs")
        if self._dim is None and vectors:
            self._dim = len(vectors[0])
        return vectors

    def _embed(self, texts: list[str], prompt: str | None) -> list[list[float]]:
        """Embed *texts* with *prompt* in front of each; one input for each request after a refused batch."""
        if prompt:
            texts = [prompt + text for text in texts]
        if self._one_input and len(texts) > 1:
            return [vector for text in texts for vector in self._post([text])]
        try:
            return self._post(texts)
        except _HttpError as exc:
            if exc.status != 404 or len(texts) == 1:
                raise
            vectors = [vector for text in texts for vector in self._post([text])]
            log.info("Embeddings: %s takes one input for each request; the batches go one input at a time",
                     self._cfg.embed_model)
            self._one_input = True
            return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, self._cfg.embed_doc_prompt)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, self._cfg.embed_query_prompt)

    @property
    def name(self) -> str:
        """The provider and the model: a model of OpenRouter is not the Ollama model of a similar name."""
        return f"{self._cfg.embed_provider}:{self._cfg.embed_model}"

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def document_settings(self) -> str:
        prompt = self._cfg.embed_doc_prompt
        return f"doc_prompt={prompt}" if prompt else ""

    @property
    def max_tokens(self) -> int:
        """The context of the model: ``embed_context`` of the profile, else the model list of OpenRouter.

        Raises:
            EmbedderContextUnknown: Another API without ``embed_context``,
                or a model that the list does not name.  No value is
                guessed: it sets the chunks and the content hash of each
                long body (``Embedder.max_tokens``).
        """
        cfg = self._cfg
        if cfg.embed_context > 0:
            return cfg.embed_context
        if cfg.embed_provider != "openrouter":
            raise EmbedderContextUnknown(
                f"the API of profile {cfg.embed_profile!r} gives no context of {cfg.embed_model}: set "
                f"embed_context in [llm.profiles.{cfg.embed_profile}]")
        base = cfg.embed_api_base.rstrip("/")
        if base not in _CONTEXTS:
            try:
                resp = net.get(f"{base}/models", params={"output_modalities": "embeddings"}, timeout=30.0)
                resp.raise_for_status()
                _CONTEXTS[base] = {m["id"]: int(m["context_length"]) for m in resp.json()["data"]
                                   if isinstance(m, dict) and m.get("context_length")}
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise EmbedderContextUnknown(f"the model list of {base} cannot be read: {exc}") from exc
        context = _CONTEXTS[base].get(cfg.embed_model)
        if context is None:
            raise EmbedderContextUnknown(f"the model list of {base} has no embedding model {cfg.embed_model!r}")
        return context


class ApiEmbedderError(EmbedderError, RuntimeError):
    """A failure of the embedding API: no answer, an HTTP error, an answer without vectors.

    A ``RuntimeError`` too, as ``EmbedderInputTooLong``: the embedding step
    (``SAFE_EXCEPT``), ``reindex_file`` and the search phases handle a
    failure of a backend as a ``RuntimeError``.  Before, one HTTP 502 of
    the API stopped the whole index run instead of one failed batch.
    """


def _error_message(resp: httpx.Response) -> str:
    """Return the ``error.message`` of an answer of the API, or its status only.

    WHY not the body: a proxy can echo a part of the request, the key too,
    and the message goes to the log and to the manifest of the build.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else None
    return str(message)[:300] if message else f"HTTP {resp.status_code} without an error message"


class _HttpError(ApiEmbedderError):
    """An HTTP status of the embedding API that is not 200: the status for the fallback to one input."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
