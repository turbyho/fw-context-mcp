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

WHY ``max_tokens`` is a property: the caller (the symbol-to-description
converter) makes the chunks of a long body, not the embedder.  The
embedder gives its token limit, thus the caller can make chunks that fit
before it sends them.

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


class EmbedderInputTooLong(EmbedderError, RuntimeError):
    """An input of the batch is longer than the context of the model.

    The Ollama backend asks the server not to cut a document text: a cut
    input gives a vector without the tail of the text, and nothing tells
    the caller.  The caller makes the input shorter and asks again
    (``_embedding._embed_in_smaller_chunks``).  The request gives no index
    of the input, thus the caller asks for each input of the batch on its
    own.  The sentence-transformers backend cuts each input at the
    ``max_seq_length`` of its model, and never raises this.

    A ``RuntimeError`` too, so that a caller that handles each failure of a
    backend as a ``RuntimeError`` still handles it.
    """


class EmbedderContextUnknown(EmbedderError, RuntimeError):
    """The backend cannot give the context length of the model (``Embedder.max_tokens``).

    The value sets the chunk size of a long body, and the chunk size is in
    the content hash of its embedding.  A guessed value thus stores other
    vectors, and the next run with the real value embeds the body again.
    The embedding step stops instead, and the build gets the "partial"
    stamp; the next run tries again.

    A ``RuntimeError`` too, for the reason of ``EmbedderInputTooLong``.
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
    def document_settings(self) -> str:
        """The settings of the backend that change the vector of a document text.

        The text and ``name`` are not in it.  The index run puts this string
        into the content hash of each embedding (``_embed_content_hash``),
        thus a change of a setting computes the embeddings again.  Before,
        the run compared the text only, and the vectors of the old setting
        stayed.

        Empty when the backend has no such setting set: the content hash of
        an index without one then stays as it was.
        """
        return ""

    @property
    @abstractmethod
    def max_tokens(self) -> int:
        """The number of tokens of one input that the model embeds.

        The caller makes chunks of a long body from it (``max_tokens * 3``
        characters) before it embeds the texts.  The Ollama backend reads
        the value from the server, and raises ``EmbedderContextUnknown``
        when the server gives none.  The sentence-transformers backend
        gives a value from its table of model names, and 32768 for an
        unknown model; the model cuts a longer input at its
        ``max_seq_length`` without an error, and the tail is not in the
        vector.
        """
