"""Phase: Reciprocal Rank Fusion — merge FTS5 and vector results with RRF.

Replaces the old deduplicate-based merge with a mathematically grounded fusion
that preserves ranking signal from both retrieval sources.  Each result list
contributes independently, with configurable weight, and a project-local boost
rewards results from the application codebase.

Default parameters confirmed by experiment (8.8–8.10): w_fts=1.8, w_vec=0.2,
k=30, project boost ×1.5, function/method boost ×1.2.

When ``weights="adaptive"``, per-query FTS5/vec weights are computed from
FTS5 term IDF statistics — rare terms get higher FTS weight (exact match),
common terms get higher vec weight (semantic disambiguation).
"""

from __future__ import annotations

import logging
import math
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

    def __init__(self, weights: str = "fixed") -> None:
        self._weights_mode = weights

    def _adaptive_weights(
        self, conn, config_hash: str, query: str, queries: list[str] | None = None
    ) -> tuple[float, float]:
        """Compute per-query FTS5/vec weights from term frequency statistics.

        Estimates IDF by counting distinct symbols whose name matches each
        query term.  High-IDF (rare) terms → more FTS weight; low-IDF
        (common) → more vec weight.  Falls back to ``W_FTS / W_VEC``.
        """
        try:
            total_docs = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash = ? AND is_definition = 1",
                (config_hash,),
            ).fetchone()[0]
            if total_docs == 0:
                return self.W_FTS, self.W_VEC
        except Exception:
            return self.W_FTS, self.W_VEC

        terms = (queries or []).copy() if queries else query.lower().split()

        avg_idf = 0.0
        counted = 0
        for term in terms:
            term = term.strip().replace("*", "")
            if not term or len(term) < 2:
                continue
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT name) FROM symbols WHERE config_hash = ? AND name LIKE ?",
                    (config_hash, f"%{term}%"),
                ).fetchone()
                df = row[0] if row else 1
                if df > 0:
                    idf = math.log(total_docs / max(df, 1))
                    avg_idf += idf
                    counted += 1
            except Exception:
                pass

        if counted == 0:
            return self.W_FTS, self.W_VEC

        avg_idf /= counted
        w_fts = 0.8 + (avg_idf / 10.0) * 1.2
        w_fts = max(0.8, min(2.0, w_fts))
        w_vec = 2.0 - w_fts
        return w_fts, w_vec

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.fts5_results) and bool(ctx.embedding_results)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        fts5_rows = ctx.fts5_results
        vec_rows = ctx.embedding_results

        # Compute weights — adaptive when configured or explicitly requested
        weights_mode = self._weights_mode
        if weights_mode == "fixed":
            try:
                weights_mode = ctx.config.index.rrf_weights
            except Exception:
                pass

        if weights_mode == "adaptive":
            from fw_context_mcp.indexer.db import open_db as _open_db
            conn = _open_db(ctx.db_path)
            try:
                w_fts, w_vec = self._adaptive_weights(
                    conn, ctx.config_hash, ctx.query, queries=ctx.generated_queries or None
                )
            except Exception:
                w_fts, w_vec = self.W_FTS, self.W_VEC
            finally:
                conn.close()
            log.debug("Adaptive RRF: w_fts=%.2f w_vec=%.2f (query=%r)", w_fts, w_vec, ctx.query[:60])
        else:
            w_fts, w_vec = self.W_FTS, self.W_VEC

        scores: dict[tuple, float] = {}
        all_rows: dict[tuple, dict] = {}

        for rank, r in enumerate(fts5_rows[: self.OVERFETCH_FTS], start=1):
            key = (r["name"], r.get("file_path"))
            boost = self._boost(r)
            scores[key] = scores.get(key, 0) + boost * w_fts / (self.K + rank)
            if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
                all_rows[key] = dict(r)

        for rank, r in enumerate(vec_rows[: self.OVERFETCH_VEC], start=1):
            key = (r["name"], r.get("file_path"))
            boost = self._boost(r)
            scores[key] = scores.get(key, 0) + boost * w_vec / (self.K + rank)
            if key not in all_rows or (r.get("is_definition") and not all_rows[key].get("is_definition")):
                all_rows[key] = dict(r)

        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0][0]))
        all_ranked = [dict(all_rows[key]) for key, _ in ranked]
        # Store top OVERFETCH (50) for reranker; fallback truncated to limit
        top_candidates = all_ranked[: max(self.OVERFETCH_FTS, self.OVERFETCH_VEC)]
        final = all_ranked[: ctx.limit]

        return ctx.evolve(ranked_candidates=top_candidates, final_results=final)

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
