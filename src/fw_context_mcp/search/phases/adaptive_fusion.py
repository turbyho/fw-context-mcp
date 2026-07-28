"""Phase: Scoring — prefer embedding results, fall back to FTS5.

Replaces old ``rrf_fusion`` + ``rerank`` dual-phase.
Evaluation on zbox-ecb-fw (30 queries): Dense-only MRR 0.609 > any hybrid.
FTS5 adds noise for NL queries — best strategy is to trust the embedding model.

When embeddings produce too few results (below *min_dense_count* threshold),
falls back to FTS5 to avoid returning near-empty result sets on projects
where dense retrieval quality is poor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)

# ── Scoring constants (kept for test_feature_comparison.py) ──
# RRF-style boost parameters from old rrf_fusion.py — _boost() is the
# reference implementation of per-symbol scoring used by the comparison
# test suite to verify booster correctness.
PROJ_BOOST: float = 1.5
FUNC_BOOST: float = 1.2
PAGERANK_BOOST: float = 0.2

# Minimum number of embedding results to trust dense-only routing.
# Below this threshold, FTS5 is used as fallback — dense results are
# too sparse to be reliable (e.g. HA_Boiler: dense MRR=0.066 vs
# FTS5 MRR=0.335 at pre-desc-v4 embedding quality).
MIN_DENSE_COUNT: int = 3


def _boost(row: dict) -> float:
    """Compute per-symbol RRF boost factor from project/kind/pagerank fields."""
    b = 1.0
    if row.get("is_project") == 1:
        b *= PROJ_BOOST
    kind = row.get("kind", "")
    if kind in ("function", "method", "constructor", "destructor", "varglobal"):
        b *= FUNC_BOOST
    pr = row.get("pagerank", 0.0) or 0.0
    if pr > 0:
        b *= 1.0 + pr * PAGERANK_BOOST
    return b


class AdaptiveFusionPhase(Phase):
    """Route results: embedding if available and sufficient, otherwise FTS5.

    No RRF merging — evaluation shows any blend degrades MRR.
    The dense-result threshold is read from ``[index] min_dense_count``
    config (default 3).  When dense results are too sparse, falls back
    to FTS5 to maintain retrieval quality on projects with immature
    embedding models.
    """

    name = "adaptive_fusion"

    def _get_threshold(self, ctx: PipelineContext) -> int:
        """Return the minimum dense count from config or module default."""
        try:
            return max(1, ctx.config.index.min_dense_count)
        except Exception:
            return MIN_DENSE_COUNT

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.fts5_results) or bool(ctx.embedding_results)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        threshold = self._get_threshold(ctx)
        emb_results = list(ctx.embedding_results) if ctx.embedding_results else []
        if len(emb_results) >= threshold:
            results = emb_results
            source = "embedding"
        else:
            results = list(ctx.fts5_results) if ctx.fts5_results else emb_results
            source = "fts5" if ctx.fts5_results else "embedding"
            if emb_results:
                log.debug(
                    "adaptive_fusion: dense results (%d) < min (%d), falling back to %s",
                    len(emb_results), threshold, source,
                )

        log.debug("adaptive_fusion: %s (%d results)", source, len(results))
        return ctx.evolve(final_results=results[: ctx.limit])
