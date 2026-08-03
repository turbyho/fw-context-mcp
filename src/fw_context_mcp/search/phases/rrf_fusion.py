"""Phase: Reciprocal Rank Fusion — merge FTS5 and vector results with RRF.

Replaces the old deduplicate-based merge with a mathematically grounded fusion
that preserves ranking signal from both retrieval sources.  Each result list
contributes independently, with configurable weight, and a project-local boost
rewards results from the application codebase.

Default parameters confirmed by experiment (8.8–8.10): w_fts=1.8, w_vec=0.2,
k=30, project boost ×1.5, function/method boost ×1.2.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


class RRFFusionPhase(Phase):
    """Merge FTS5 and embedding result lists via Reciprocal Rank Fusion.

    Runs when both ``fts5_results`` and ``embedding_results`` are non-empty.
    Equivalent in quality to the previous deduplicate-based merge but faster
    (no Python scoring loop) and better at rewarding project-code results.
    """

    name = "rrf_fusion"

    # ── Parameters (confirmed by experiments 8.8–8.10) ──────────────
    W_FTS: float = 1.8
    W_VEC: float = 0.2
    K: int = 30
    PROJ_BOOST: float = 1.5
    FUNC_BOOST: float = 1.2
    PAGERANK_BOOST: float = 0.2
    OVERFETCH_FTS: int = 50
    OVERFETCH_VEC: int = 50

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.fts5_results) and bool(ctx.embedding_results)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        fts5_rows = ctx.fts5_results
        vec_rows = ctx.embedding_results

        scores: dict[tuple, float] = {}
        all_rows: dict[tuple, dict] = {}

        for rank, r in enumerate(fts5_rows[: self.OVERFETCH_FTS], start=1):
            key = (r["name"], r.get("file_path"))
            boost = self._boost(r)
            scores[key] = scores.get(key, 0) + boost * self.W_FTS / (self.K + rank)
            if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
                all_rows[key] = dict(r)

        for rank, r in enumerate(vec_rows[: self.OVERFETCH_VEC], start=1):
            key = (r["name"], r.get("file_path"))
            boost = self._boost(r)
            scores[key] = scores.get(key, 0) + boost * self.W_VEC / (self.K + rank)
            if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
                all_rows[key] = dict(r)

        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0][0]))
        final = [dict(all_rows[key]) for key, _ in ranked[: ctx.limit]]

        return ctx.evolve(final_results=final)

    def _boost(self, row: dict) -> float:
        b = 1.0
        if row.get("is_project") == 1:
            b *= self.PROJ_BOOST
        kind = row.get("kind", "")
        if kind in ("function", "method", "constructor", "destructor", "varglobal"):
            b *= self.FUNC_BOOST
        pr = row.get("pagerank", 0.0) or 0.0
        if pr > 0:
            b *= 1.0 + pr * self.PAGERANK_BOOST
        return b
