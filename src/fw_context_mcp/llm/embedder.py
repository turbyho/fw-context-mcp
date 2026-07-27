"""Embedder abstraction — pluggable embedding backends.

Provides an ABC ``Embedder`` with two methods:
- ``embed_documents`` — used during indexing (chunk of symbol descriptions)
- ``embed_queries`` — used at query time (single query string)

Implementations:
- ``OllamaEmbedder`` — delegates to Ollama HTTP API (existing backend)
- ``SentenceTransformerEmbedder`` — local models via sentence-transformers (optional)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.settings import LLMConfig


class Embedder(ABC):
    """Pluggable embedding backend."""

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of symbol descriptions (index-time)."""

    @abstractmethod
    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed one or more query strings (query-time)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for cache disambiguation."""

    @property
    @abstractmethod
    def dim(self) -> int | None:
        """Embedding dimension, or None before the first embed call."""

    @property
    @abstractmethod
    def max_tokens(self) -> int:
        """Approximate max token count for truncation decisions."""


def get_embedder(cfg: LLMConfig) -> Embedder:
    """Return the appropriate :class:`Embedder` for *cfg*.

    Auto-detects the backend from ``cfg.embed_model``:
    - ``ibm-granite/*``, ``lightonai/*``, ``BAAI/*``, ``cross-encoder/*`` →
      sentence-transformers (requires optional ``[st]`` extra)
    - Everything else → OllamaEmbedder
    """
    model = cfg.embed_model.lower()
    _st_prefixes = ("ibm-granite/", "lightonai/", "baai/", "cross-encoder/", "sentence-transformers/")
    if model.startswith(_st_prefixes):
        return _get_st_embedder(cfg)
    from .ollama import OllamaEmbedder
    return OllamaEmbedder(cfg)


def _get_st_embedder(cfg: LLMConfig) -> Embedder:
    """Lazy-import the sentence-transformers backend."""
    from .st_embedder import SentenceTransformerEmbedder
    return SentenceTransformerEmbedder(cfg)
