"""Cross-encoder reranker for second-stage retrieval refinement.

Re-ranks first-pass candidates by scoring query-candidate pairs together,
producing higher precision than bi-encoder-only retrieval.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

log = logging.getLogger(__name__)


class Reranker(Protocol):
    """Protocol for second-stage reranking of retrieval candidates."""

    def rank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """Re-rank *candidates* by query relevance, returning *top_k* results."""
        ...


class CrossEncoderReranker:
    """Cross-encoder reranker using sentence-transformers ``CrossEncoder``.

    Scores query-candidate pairs jointly via a fine-tuned cross-encoder
    model (e.g. ``cross-encoder/ms-marco-MiniLM-L6-v2``).  The model is
    lazily loaded on first use.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as err:
                raise ImportError(
                    "sentence-transformers is required for the reranker. "
                    "Install it with: pip install fw-context-mcp[st]"
                ) from err
            log.info("Loading reranker model: %s ...", self._model_name)
            self._model = CrossEncoder(self._model_name)

    def rank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        if not candidates:
            return []

        self._ensure_model()

        # Build (query, candidate_text) pairs for the cross-encoder
        pairs: list[tuple[str, str]] = []
        for c in candidates:
            desc = self._describe(c)
            pairs.append((query, desc))

        scores = self._model.predict(pairs)  # type: ignore[union-attr]

        # Attach scores and sort descending
        scored = list(zip(candidates, scores, strict=True))
        scored.sort(key=lambda x: -x[1])

        results = []
        for c, score in scored[:top_k]:
            d = dict(c)
            d["_rerank_score"] = round(float(score), 4)
            results.append(d)

        return results

    @staticmethod
    def _describe(candidate: dict) -> str:
        """Build a text description for a candidate symbol."""
        parts = [candidate.get("kind", ""), candidate.get("name", "")]
        if sig := candidate.get("signature"):
            parts.append(sig)
        if summary := candidate.get("summary"):
            parts.append(summary)
        if source := candidate.get("source"):
            snip = source[:500]
            parts.append(snip)
        return " : ".join(p for p in parts if p)


def get_reranker(model_name: str | None) -> Reranker | None:
    """Return a :class:`Reranker` for *model_name*, or None when disabled."""
    if not model_name:
        return None
    return CrossEncoderReranker(model_name)
