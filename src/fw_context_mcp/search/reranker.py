"""Cross-encoder reranker for second-stage retrieval refinement.

Why a cross-encoder instead of a bi-encoder?
    Bi-encoders (like the embedding model) encode query and candidates
    independently, then compare via cosine similarity.  This is fast but
    loses interaction between query and candidate terms — "modem init"
    may rank "timer_init" above "modem_attach" because cosine similarity
    can't distinguish which words matter most.

    A cross-encoder encodes query and candidate TOGETHER, attending to
    both simultaneously.  This catches semantic nuances: "modem attach"
    vs "callback attach" produce very different cross-encoder scores
    even though both contain "attach".  The cost is 10-50× slower per
    candidate, so it's used only as second-stage refinement on top-N
    first-pass results, not on the full candidate set.

Why a Protocol instead of a concrete class?
    Different reranker backends may be added (API-based, cached, hybrid).
    The ``Reranker`` Protocol defines the interface without coupling
    callers to the cross-encoder implementation.

Re-ranks first-pass candidates by scoring query-candidate pairs together,
producing higher precision than bi-encoder-only retrieval.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

log = logging.getLogger(__name__)


class Reranker(Protocol):
    """Protocol for second-stage reranking of retrieval candidates.

    Why a Protocol?
        Enables swapping reranker backends (cross-encoder, API reranker,
        cached reranker) without changing caller code.  Type checkers
        validate the interface; runtime doesn't require subclassing.
    """

    def rank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """Re-rank *candidates* by query relevance, returning *top_k* results."""
        ...

    async def rank_async(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """Async wrapper — delegates to :meth:`rank` via ``asyncio.to_thread``.

        Why async?
            Cross-encoder inference is CPU-bound (PyTorch).  Running it
            in ``asyncio.to_thread`` prevents blocking the event loop
            while the model scores candidates.
        """
        ...


class CrossEncoderReranker:
    """Cross-encoder reranker using sentence-transformers ``CrossEncoder``.

    Why lazy loading?
        The cross-encoder model (~90 MB for ms-marco-MiniLM-L6-v2) takes
        0.5-2 s to load.  Loading at import time would make every search
        slow, even ones that never use the reranker.  ``_ensure_model()``
        loads on first use only.

    Why thread-safe loading?
        Multiple concurrent search requests may trigger ``_ensure_model()``
        simultaneously.  The double-checked lock ensures the model loads
        exactly once.

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

    async def rank_async(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """Async wrapper — delegates to :meth:`rank` via ``asyncio.to_thread``."""
        import asyncio
        return await asyncio.to_thread(self.rank, query, candidates, top_k)

    def rank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        if not candidates:
            return []

        self._ensure_model()

        # Build (query, candidate_text) pairs for the cross-encoder.
        # Each candidate is described as "kind: name : signature : summary : source"
        # — the cross-encoder attends to all this context jointly with the query.
        pairs: list[tuple[str, str]] = []
        for c in candidates:
            desc = self._describe(c)
            pairs.append((query, desc))

        scores = self._model.predict(pairs)  # type: ignore[attr-defined]

        # Attach scores and sort descending
        if len(scores) != len(candidates):
            log.warning("Reranker: score/candidate count mismatch (%d vs %d)", len(scores), len(candidates))
            min_len = min(len(scores), len(candidates))
            candidates = candidates[:min_len]
            scores = scores[:min_len]
        scored = list(zip(candidates, scores, strict=False))
        scored.sort(key=lambda x: -x[1])

        results = []
        for c, score in scored[:top_k]:
            d = dict(c)
            d["_rerank_score"] = round(float(score), 4)
            results.append(d)

        return results

    @staticmethod
    def _describe(candidate: dict) -> str:
        """Build a text description for a candidate symbol.

        Why include signature and summary?
            The cross-encoder benefits from all available context.  A
            bare name like "send" is ambiguous — the signature "send(const
            char*, size_t)" and summary "sends data over the modem" help
            the cross-encoder distinguish modem::send from UART::send.
            Source text (first 500 chars) adds implementation detail.
        """
        parts = [candidate.get("kind", ""), candidate.get("name", "")]
        if sig := candidate.get("signature"):
            parts.append(sig)
        # A formatted result holds the summary under `llm_analysis`.  A raw
        # symbol row still carries it flat.  Both shapes reach this method.
        analysis = candidate.get("llm_analysis") or {}
        if summary := (analysis.get("summary") or candidate.get("summary")):
            parts.append(summary)
        if source := candidate.get("source"):
            snip = source[:500]
            parts.append(snip)
        return " : ".join(p for p in parts if p)


def get_reranker(model_name: str | None) -> Reranker | None:
    """Return a :class:`Reranker` for *model_name*, or None when disabled.

    Why None instead of raising?
        The reranker is optional — searches work correctly without it.
        Returning None lets callers skip reranking gracefully rather
        than requiring error handling.
    """
    if not model_name:
        return None
    return CrossEncoderReranker(model_name)
