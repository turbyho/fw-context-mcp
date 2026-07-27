"""Sentence-Transformers embedder — local model inference.

Lazy-imports ``sentence_transformers`` inside ``__init__`` to avoid
a hard dependency on a heavy optional package.

Supports Matryoshka representation (MRL) — when ``embed_dim`` is set
in config, output vectors are truncated to that dimension before
normalization.  Truncation happens on the encoder side so the stored
vector is the smaller dimension; this avoids the 6× storage cost.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .embedder import Embedder

if TYPE_CHECKING:
    from ..config.settings import LLMConfig

log = logging.getLogger(__name__)

# Model-specific max token limits (fallback when model card not loaded)
_MODEL_MAX_TOKENS: dict[str, int] = {
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
        self._native_dim: int | None = None  # model's full dim before MRL truncation

    def _ensure_model(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise ImportError(
                "sentence-transformers is required for this model. "
                "Install it with: pip install fw-context-mcp[st]"
            ) from err
        model_path = self._cfg.embed_model
        log.info("Loading embedding model: %s ...", model_path)
        self._model = SentenceTransformer(model_path)
        self._native_dim = self._model.get_sentence_embedding_dimension()
        if self._cfg.embed_dim and self._cfg.embed_dim < self._native_dim:
            log.info("MRL: truncating %d → %d (model=%s)", self._native_dim, self._cfg.embed_dim, model_path)
            self._dim = self._cfg.embed_dim
        else:
            self._dim = self._native_dim

    def _encode(self, texts: list[str]) -> list[list[float]]:
        """Encode and optionally MRL-truncate + normalize."""
        assert self._model is not None and self._native_dim is not None
        result = self._model.encode(texts, normalize_embeddings=True)
        if self._cfg.embed_dim and self._cfg.embed_dim < len(result[0]):
            result = result[:, : self._cfg.embed_dim]
            import numpy as np
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
        return self._dim

    @property
    def max_tokens(self) -> int:
        model_lower = self._cfg.embed_model.lower()
        for prefix, limit in _MODEL_MAX_TOKENS.items():
            if model_lower.startswith(prefix):
                return limit
        return 32768
