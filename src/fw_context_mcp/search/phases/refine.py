"""Phase: LLM checks first-round results and refines queries if needed.

Why refine?
    The initial LLM-generated FTS5 terms (Phase 2) are based on the raw
    query and sample symbols — before any actual search results exist.
    The LLM may have misidentified the subsystem: "register to the network"
    might generate hardware register terms when the user meant modem
    network registration.

    Refinement lets the LLM see the actual FTS5 results and course-correct.
    If the top results are clearly from the wrong subsystem (e.g. BLE
    connection code for a modem query), the LLM generates better terms
    targeting the correct subsystem.

Why skip when Ollama already failed?
    If Phase 2 (llm_query) already got an OllamaError, refinement would
    hit the same error.  Skipping avoids a second failed API call and
    lets the pipeline continue with first-round results.

Why update the cache on refinement?
    The refined terms (original + new) replace the cached entry.  The
    next search with the same query gets the corrected terms directly
    from cache, avoiding the wrong-subsystem detour entirely.

Skipped when Ollama is disabled, when there are no generated queries,
or when an Ollama error occurred in Phase 2a.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fw_context_mcp.search._llm_parse import _SAFE_FTS5_TOKEN
from fw_context_mcp.search._llm_parse import parse_llm_search_terms as _parse_search_terms
from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class RefinePhase(Phase):
    """Let the LLM see actual search results and course-correct.

    What it does:
        Shows the top 10 FTS5 results to the LLM along with the original
        query and first-round terms.  Asks the LLM: "are these from the
        right subsystem?"  If misaligned, the LLM generates 3-5 better
        terms targeting the correct subsystem.

    Why union (not replace) first-round terms?
        First-round terms may still be partially correct — the subsystem
        was wrong but the domain was right.  Union preserves the original
        terms while adding corrected ones, giving FTS5 more signal.

    Returns an empty array if results already look correct.

    Skipped when Ollama is disabled, when there are no generated queries,
    or when an Ollama error occurred in Phase 2a.
    """

    name = "refine"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx) -> bool:
        """Only run when LLM is enabled, we have generated queries, and no Ollama warning.

        Why check generated_queries?
            SEARCH_CODE pipelines never run LLMQueryPhase, so there are no
            generated_queries to refine.  This check skips refinement for
            the fast FTS5-only path.

        Why check ollama_warning?
            If Phase 2 already failed, Phase 3 would fail the same way.
            Skipping avoids a redundant error.
        """
        return (
            ctx.config.llm is not None
            and ctx.config.llm.enabled
            and bool(ctx.generated_queries)
            and ctx.ollama_warning is None
        )

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Show FTS5 results to the LLM and let it course-correct query terms.

        Builds a prompt with the top 10 FTS5 results and asks the LLM
        whether they match the original intent.  Returns refined query
        terms when results are misaligned, or an empty list when correct.
        Updates the keyword cache on success so future identical queries
        skip the wrong-subsystem detour.
        """
        from fw_context_mcp.llm.ollama import OllamaError, call_ollama_async
        from fw_context_mcp.search.cache import keyword_cache

        if not ctx.fts5_results:
            return ctx  # No results to refine against

        top_lines = []
        for r in ctx.fts5_results[:10]:
            name = r.get("name") or "?"
            kind = r.get("kind") or "?"
            path = r.get("file_path") or "?"
            top_lines.append(f"  {name} ({kind}) — {path}")

        result_note = "Top results from those queries:\n" + "\n".join(top_lines) + "\n\n"

        refine_prompt = (
            "A developer searched for:\n"
            f"  «{ctx.query}»\n\n"
            f"First-round FTS5 queries: {json.dumps(ctx.generated_queries)}\n\n"
            f"{result_note}"
            "Are these results from the RIGHT subsystem? If the results look\n"
            "misaligned (e.g. BLE connection code for a modem query, hardware\n"
            "register code for a network registration query), generate 3-5\n"
            "BETTER FTS5 prefix queries that target the CORRECT subsystem.\n\n"
            "If the results already look correct, return an empty array: []\n\n"
            "CRITICAL: Use naming patterns from the original samples. If the\n"
            "project uses snake_case (modem_init, network_registration), your\n"
            "queries MUST be snake_case (modem_init*, network_reg*).\n\n"
            'Return ONLY a JSON array: ["better1*", "better2*"] or []\n'
        )

        try:
            raw = await call_ollama_async(refine_prompt, ctx.config.llm)
            refined = _parse_search_terms(raw)[:5]
            # Sanitize LLM-generated tokens — same safe pattern as fts5_search.py
            refined = [t for t in refined if _SAFE_FTS5_TOKEN.match(t)]
            if refined:
                all_queries = ctx.generated_queries + refined
                cache_key = (ctx.query, ctx.config_hash)
                # Update the cache with refined terms so future identical
                # queries skip the wrong-subsystem detour entirely
                keyword_cache.set(cache_key, all_queries)
                return ctx.evolve(generated_queries=all_queries)
        except OllamaError:
            pass  # Refinement is optional; keep first-round results
        return ctx


