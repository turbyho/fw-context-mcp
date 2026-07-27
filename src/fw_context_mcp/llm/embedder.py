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
    - ``ft://latest`` or ``ft://<path>`` — resolve a fine-tuned local model
      (loads via sentence-transformers from ~/.fw-context/models/)
    - ``ibm-granite/*``, ``lightonai/*``, ``BAAI/*``, ``cross-encoder/*`` →
      sentence-transformers (requires optional ``[st]`` extra)
    - Everything else → OllamaEmbedder
    """
    model = cfg.embed_model.lower()
    if model.startswith("ft://"):
        return _get_ft_embedder(cfg)
    _st_prefixes = ("ibm-granite/", "lightonai/", "baai/", "cross-encoder/", "sentence-transformers/")
    if model.startswith(_st_prefixes):
        return _get_st_embedder(cfg)
    from .ollama import OllamaEmbedder
    return OllamaEmbedder(cfg)


def _get_st_embedder(cfg: LLMConfig) -> Embedder:
    """Lazy-import the sentence-transformers backend."""
    from .st_embedder import SentenceTransformerEmbedder
    return SentenceTransformerEmbedder(cfg)


def _get_ft_embedder(cfg: LLMConfig) -> Embedder:
    """Resolve a fine-tuned local model path.

    ``ft://latest`` — use the most recent model for the current project
    and description version.  ``ft://<absolute-path>`` — use directly.
    """
    import logging
    from pathlib import Path

    from .st_embedder import SentenceTransformerEmbedder

    log = logging.getLogger(__name__)
    model = cfg.embed_model

    if model == "ft://latest":
        models_root = Path.home() / ".fw-context" / "models"
        if not models_root.is_dir():
            raise FileNotFoundError(
                f"No fine-tuned models found at {models_root}. "
                "Run 'fw-context finetune' first."
            )
        best: Path | None = None
        best_mtime = 0.0
        for proj_dir in models_root.iterdir():
            if not proj_dir.is_dir():
                continue
            for ft_dir in proj_dir.iterdir():
                metadata = ft_dir / "metadata.json"
                if metadata.exists():
                    try:
                        mtime = metadata.stat().st_mtime
                        if mtime > best_mtime:
                            best_mtime = mtime
                            best = ft_dir / "final"
                    except OSError:
                        continue
        if best is None or not best.is_dir():
            raise FileNotFoundError(
                f"No fine-tuned model found under {models_root}. "
                "Run 'fw-context finetune' first."
            )
        log.info("Resolved ft://latest → %s", best)
        import copy as _copy

        resolved_cfg = _copy.copy(cfg)
        resolved_cfg.embed_model = str(best)
        return SentenceTransformerEmbedder(resolved_cfg)

    # ft://<path>
    import copy as _copy

    resolved = model.removeprefix("ft://")
    resolved_cfg = _copy.copy(cfg)
    resolved_cfg.embed_model = resolved
    return SentenceTransformerEmbedder(resolved_cfg)
