"""Phase 3: LLM checks first-round results and refines queries if needed."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from fw_context_mcp.search._llm_parse import parse_llm_search_terms as _parse_search_terms
from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class RefinePhase(Phase):
    """Let the LLM see actual search results and course-correct.

    If the results look misaligned (e.g. BLE connection code for a modem
    query), the LLM generates refined queries targeting the correct subsystem.
    Returns an empty array if results already look correct.

    Skipped when Ollama is disabled, when there are no generated queries,
    or when an Ollama error occurred in Phase 2a.
    """

    name = "refine"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx) -> bool:
        """Only run when LLM is enabled, we have generated queries, and no Ollama warning."""
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
        Updates the keyword cache on success.
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
            # Sanitize LLM-generated tokens — same pattern as H9 in fts5_search.py
            _SAFE_TOKEN = re.compile(r"^[\w*]+$")
            refined = [t for t in refined if _SAFE_TOKEN.match(t)]
            if refined:
                all_queries = ctx.generated_queries + refined
                cache_key = (ctx.query, ctx.config_hash)
                keyword_cache.set(cache_key, all_queries)
                return ctx.evolve(generated_queries=all_queries)
        except OllamaError:
            pass  # Refinement is optional; keep first-round results
        return ctx


