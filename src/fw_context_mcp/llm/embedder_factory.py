"""Embedder factory — resolves backend from LLMConfig."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fw_context_mcp.config.settings import LLMConfig
    from fw_context_mcp.llm.embedder import Embedder

log = logging.getLogger(__name__)


def get_embedder(cfg: LLMConfig) -> Embedder:
    """Return the appropriate :class:`Embedder` for *cfg*.

    Auto-detects the backend from ``cfg.embed_model``:
    - Empty string (default) → auto-detect best model for this system
      (GPU available → qwen3-embedding:8b, CPU-only → qwen3-embedding:0.6b)
    - ``ft://latest`` or ``ft://<path>`` — resolve a fine-tuned local model
      (loads via sentence-transformers from ~/.fw-context/models/)
    - ``ibm-granite/*``, ``lightonai/*``, ``BAAI/*``, ``cross-encoder/*`` →
      sentence-transformers (requires optional ``[st]`` extra)
    - Everything else → OllamaEmbedder
    """
    from .auto_model import resolve_embed_model

    resolve_embed_model(cfg)  # empty → auto-detect, explicit → no-op
    model = cfg.embed_model.lower()
    try:
        if model.startswith("ft://"):
            return _get_ft_embedder(cfg)
        _st_prefixes = ("ibm-granite/", "lightonai/", "baai/", "cross-encoder/", "sentence-transformers/")
        if model.startswith(_st_prefixes):
            return _get_st_embedder(cfg)
        from .ollama import OllamaEmbedder
        return OllamaEmbedder(cfg)
    except (OSError, ImportError, RuntimeError, ValueError) as e:
        from .embedder import EmbedderError
        raise EmbedderError(str(e)) from e


def _get_st_embedder(cfg: LLMConfig) -> Embedder:
    """Lazy-import the sentence-transformers backend."""
    from .st_embedder import SentenceTransformerEmbedder
    return SentenceTransformerEmbedder(cfg)


_FT_EMBEDDER_CACHE: dict[str, Embedder] = {}

def _get_ft_embedder(cfg: LLMConfig) -> Embedder:
    """Resolve a fine-tuned local model path.

    ``ft://latest`` — use the most recent model for the current project
    and description version.  ``ft://<absolute-path>`` — use directly.

    Results are cached by model path to avoid directory traversal on
    every call.
    """
    from .st_embedder import SentenceTransformerEmbedder

    model = cfg.embed_model
    if model in _FT_EMBEDDER_CACHE:
        return _FT_EMBEDDER_CACHE[model]

    if model == "ft://latest":
        models_root = Path.home() / ".fw-context" / "models"
        if not models_root.is_dir():
            raise FileNotFoundError(
                f"No fine-tuned models found at {models_root}. "
                "Run 'fw-context finetune' first."
            )
        best: Path | None = None
        best_mtime = 0.0
        # Limit scan to 100 project dirs to avoid hanging on slow filesystems
        for proj_dir in list(models_root.iterdir())[:100]:
            if not proj_dir.is_dir():
                continue
            try:
                ft_items = list(proj_dir.iterdir())
            except OSError:
                continue
            for ft_dir in ft_items:
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
        _validate_model_dir(best, "ft://latest resolved to " + str(best))
        log.info("Resolved ft://latest → %s", best)
        resolved_cfg = copy.copy(cfg)
        resolved_cfg._ft_model_path = str(best)  # type: ignore[attr-defined]
        embedder = SentenceTransformerEmbedder(resolved_cfg)
        _FT_EMBEDDER_CACHE[model] = embedder
        return embedder

    # ft://<path>
    resolved = model.removeprefix("ft://")
    model_path = Path(resolved)
    if not model_path.is_dir():
        raise FileNotFoundError(
            f"Fine-tuned model not found at {model_path}. "
            "Check the path or run 'fw-context finetune' first."
        )
    _validate_model_dir(model_path, str(model_path))
    resolved_cfg = copy.copy(cfg)
    resolved_cfg._ft_model_path = resolved  # type: ignore[attr-defined]
    embedder = SentenceTransformerEmbedder(resolved_cfg)
    _FT_EMBEDDER_CACHE[model] = embedder
    return embedder


def _validate_model_dir(path: Path, label: str) -> None:
    """Check that *path* contains a loadable sentence-transformers model."""
    if not (path / "config.json").exists():
        raise FileNotFoundError(
            f"Model directory {label} exists but is missing config.json. "
            "The model may be corrupted — re-run 'fw-context finetune' to regenerate it."
        )
