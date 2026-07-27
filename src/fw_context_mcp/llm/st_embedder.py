"""Sentence-Transformers embedder — local model inference.

Lazy-imports ``sentence_transformers`` inside ``__init__`` to avoid
a hard dependency on a heavy optional package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .embedder import Embedder

if TYPE_CHECKING:
    from ..config.settings import LLMConfig

log = logging.getLogger(__name__)


class SentenceTransformerEmbedder(Embedder):
    """Embedding backend using HuggingFace/ST models loaded in-process."""

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._model = None
        self._dim: int | None = None

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
        self._dim = self._model.get_sentence_embedding_dimension()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        prompt = self._cfg.embed_doc_prompt
        if prompt:
            texts = [prompt + t for t in texts]
        result = self._model.encode(texts, normalize_embeddings=True)
        return [vec.tolist() for vec in result]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        prompt = self._cfg.embed_query_prompt
        if prompt:
            texts = [prompt + t for t in texts]
        result = self._model.encode(texts, normalize_embeddings=True)
        return [vec.tolist() for vec in result]

    @property
    def name(self) -> str:
        return self._cfg.embed_model

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def max_tokens(self) -> int:
        return 32768  # Granite R2 typical; conservative default
