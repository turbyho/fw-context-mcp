"""Phase: Cross-encoder rerank — second-stage refinement of RRF candidates.

Consumes ``ranked_candidates`` from RRF fusion, re-ranks them via a
cross-encoder model, and populates ``final_results``.  When no reranker
is configured, truncates ``ranked_candidates`` to ``ctx.limit`` as a
no-op fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.reranker import get_reranker

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


class RerankPhase(Phase):
    """Second-stage cross-encoder rerank.

    Runs when ``ranked_candidates`` is non-empty and a reranker model is
    configured.  Falls back to truncation when the reranker is disabled.
    """

    name = "rerank"

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        candidates = ctx.ranked_candidates
        if not candidates:
            return ctx.evolve(final_results=[])

        model_name = ctx.config.llm.reranker_model
        reranker = get_reranker(model_name)

        if reranker is not None:
            try:
                final = reranker.rank(ctx.query, candidates, ctx.limit)
                return ctx.evolve(final_results=final)
            except Exception:
                log.warning("Reranker failed — falling back to rank-truncation", exc_info=True)

        # Fallback: truncate to limit
        return ctx.evolve(final_results=list(candidates[: ctx.limit]))
