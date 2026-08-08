"""Embedder abstraction — pluggable embedding backends.

Provides an ABC ``Embedder`` with two methods:
- ``embed_documents`` — used during indexing (chunk of symbol descriptions)
- ``embed_queries`` — used at query time (single query string)

WHY two methods (not one): Asymmetric embedding models (e.g. E5, BGE,
GTE) use different internal behaviour for queries vs documents.  A query
prefix (``embed_query_prompt``) triggers the model to produce a
retrieval-oriented vector; a document prefix (``embed_doc_prompt``)
produces a storage-oriented vector.  The interface enforces this
separation so backends always know the intent of the text they encode.

WHY ``dim`` is ``int | None``: Lazy backends (Ollama) don't know the
embedding dimension until the first batch returns.  Search pipelines
that need dimension info must handle ``None`` gracefully (delay dimension
check until after first embed call, or use a model-specific default).

WHY ``max_tokens`` is a property: Truncation decisions happen at the
caller (search pipeline, symbol-to-description converter), not inside
the embedder.  The embedder exposes its token limit so the caller can
truncate before sending.

Implementations:
- ``OllamaEmbedder`` — delegates to Ollama HTTP API (existing backend)
- ``SentenceTransformerEmbedder`` — local models via sentence-transformers (optional)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class EmbedderError(Exception):
    """Unified exception for all embedder backend failures.

    Used by search pipeline phases to catch embedder errors
    regardless of which backend (Ollama or SentenceTransformer) is active.

    WHY unified exception: Search pipeline code should not import
    backend-specific exception types.  A single ``except EmbedderError``
    handles all embedding failures; the pipeline can log and fall back
    without knowing which backend failed.
    """


class Embedder(ABC):
    """Pluggable embedding backend.

    Implementations must provide ``embed_documents``, ``embed_queries``,
    ``name``, ``dim``, and ``max_tokens``.  See module docstring for
    the rationale behind the two-method design and the lazy ``dim``.
    """

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of symbol descriptions (index-time).

        Called during ``fw-context index --analyze`` when a batch of
        symbol descriptions needs to be converted to vectors for storage
        in the embedding table.  Backends may prepend ``embed_doc_prompt``
        for asymmetric encoding.
        """

    @abstractmethod
    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Embed one or more query strings (query-time).

        Called during semantic search when a user query needs to be
        converted to a vector for cosine similarity comparison against
        stored symbol embeddings.  Backends may prepend
        ``embed_query_prompt`` for asymmetric encoding.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for cache disambiguation.

        Used as part of composite cache keys (e.g. ``embed_key()`` in
        llm.py) so that switching to a different embedding model
        invalidates cached embeddings — different models produce
        different vector spaces; a cosine search across mismatched
        dimensions would fail.
        """

    @property
    @abstractmethod
    def dim(self) -> int | None:
        """Embedding dimension, or None before the first embed call.

        See module docstring for why this can be None (lazy backends).
        """

    @property
    @abstractmethod
    def max_tokens(self) -> int:
        """Approximate max token count for truncation decisions.

        Truncation is applied BEFORE embedding so the model never sees
        text longer than its context window.  This avoids silent
        truncation by the model which would lose the end of long
        descriptions.
        """
