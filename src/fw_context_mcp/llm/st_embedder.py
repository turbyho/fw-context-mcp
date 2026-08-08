"""Sentence-Transformers embedder — local model inference.

Lazy-imports ``sentence_transformers`` inside ``__init__`` to avoid
a hard dependency on a heavy optional package.

Supports Matryoshka representation (MRL) — when ``embed_dim`` is set
in config, output vectors are truncated to that dimension before
normalization.  Truncation happens on the encoder side so the stored
vector is the smaller dimension; this avoids the 6× storage cost.

WHY Matryoshka: Full-dimension embeddings (1024 or 4096 floats) consume
significant disk space in SQLite.  MRL-trained models produce vectors
where the first N dimensions alone are a valid, normalized embedding —
truncating from 4096 to 768 produces a usable vector at ~1/5 the size.
The cosine similarity of truncated vectors is slightly degraded but
still meaningful for semantic search.  This is a space/accuracy
trade-off configurable per deployment via ``embed_dim``.

WHY double-checked locking for model load: ``SentenceTransformer``
constructor downloads and loads a multi-GB model.  Multiple threads
calling ``embed_*`` concurrently would all try to load the model;
the lock ensures only one thread pays the load cost, all others
wait on the already-loaded instance.

WHY Lock() not RLock(): ``_ensure_model()`` does NOT call back into
the embedder's own properties — no recursion risk.  Lock is cheaper
and sufficient.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from .embedder import Embedder

if TYPE_CHECKING:
    from ..config.settings import LLMConfig

log = logging.getLogger(__name__)

# Model-specific max token limits (fallback when model card not loaded)
_MODEL_MAX_TOKENS: dict[str, int] = {
    # Ollama-hosted models (included for documentation — resolved via OllamaEmbedder)
    "qwen3-embedding:0.6b": 32768,
    "qwen3-embedding:4b": 32768,
    "qwen3-embedding:8b": 8192,
    # HuggingFace SentenceTransformer models
    "ibm-granite/granite-embedding-311m-multilingual-r2": 32768,
    "ibm-granite/granite-embedding-97m-multilingual-r2": 32768,
    "ibm-granite/granite-embedding-278m-multilingual": 8192,
    "lightonai/lateon-code-edge": 2048,
    "baai/bge-small-en-v1.5": 512,
    "baai/bge-large-en-v1.5": 512,
}


class SentenceTransformerEmbedder(Embedder):
    """Embedding backend using HuggingFace/ST models loaded in-process.

    When ``cfg.embed_dim`` is set, enables Matryoshka truncation:
    vectors are embedded at full model dimension then truncated to
    ``embed_dim`` and re-normalized before storage/query.
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._model = None
        self._dim: int | None = None
        # Model's full dimension BEFORE MRL truncation.
        # Used to compute the actual stored dimension from embed_dim config.
        self._native_dim: int | None = None
        # Double-checked locking: _ensure_model() reads _model outside the lock
        # for the fast path (already loaded), acquires lock for the slow path
        # (first load).
        self._lock = threading.Lock()

    def _ensure_model(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as err:
                raise ImportError(
                    "sentence-transformers is required for this model. "
                    "Install it with: pip install fw-context-mcp[st]"
                ) from err
            model_path = getattr(self._cfg, "_ft_model_path", None) or self._cfg.embed_model
            log.info("Loading embedding model: %s ...", model_path)
            try:
                self._model = SentenceTransformer(model_path)
                self._native_dim = self._model.get_sentence_embedding_dimension()
            except (OSError, RuntimeError, ValueError) as err:
                from .embedder import EmbedderError
                raise EmbedderError(
                    f"Failed to load SentenceTransformer model '{model_path}': {err}"
                ) from err
            if self._cfg.embed_dim and self._cfg.embed_dim < self._native_dim:
                log.info("MRL: truncating %d → %d (model=%s)", self._native_dim, self._cfg.embed_dim, model_path)
                self._dim = self._cfg.embed_dim
            else:
                self._dim = self._native_dim

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """Encode and optionally MRL-truncate + normalize."""
        if self._model is None:
            raise RuntimeError("_encode called before _ensure_model()")
        if not texts:
            return []
        # SentenceTransformer.encode(normalize_embeddings=True) returns
        # L2-normalized vectors as a numpy array of shape (N, native_dim).
        result = self._model.encode(texts, normalize_embeddings=True)
        # MRL truncation: slice to the first embed_dim dimensions, then
        # re-normalize.  Matryoshka-trained models guarantee that the first
        # N dimensions form a nearly-orthonormal subspace — re-normalization
        # restores unit norm after truncation.
        if self._cfg.embed_dim and self._cfg.embed_dim < len(result[0]):
            result = result[:, : self._cfg.embed_dim]
            norms = np.linalg.norm(result, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            result = result / norms
        return [vec.tolist() for vec in result]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        prompt = self._cfg.embed_doc_prompt
        if prompt:
            texts = [prompt + t for t in texts]
        return self._encode(texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        prompt = self._cfg.embed_query_prompt
        if prompt:
            texts = [prompt + t for t in texts]
        return self._encode(texts)

    @property
    def name(self) -> str:
        return self._cfg.embed_model

    @property
    def dim(self) -> int | None:
        with self._lock:
            return self._dim

    @property
    def model(self):
        """Return the loaded :class:`SentenceTransformer` model, loading it lazily.

        This is the public accessor for ``_model`` — use this instead of
        reaching into the private attribute.  Returns None before the first
        embed call.
        """
        self._ensure_model()
        return self._model

    @property
    def max_tokens(self) -> int:
        model_lower = self._cfg.embed_model.lower()
        for prefix, limit in _MODEL_MAX_TOKENS.items():
            if model_lower.startswith(prefix):
                return limit
        # Unknown model — return 32768 (largest known limit) to err on the
        # side of NOT truncating.  A too-small limit loses content; a too-large
        # limit causes the model to truncate internally (message logged at
        # WARNING in sentence-transformers).
        return 32768
