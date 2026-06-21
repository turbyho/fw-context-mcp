"""Phase 2b: LLM checks first-round results and refines queries if needed."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

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

    name = "refine"

    def should_run(self, ctx) -> bool:
        return (
            ctx.config.llm.enabled
            and bool(ctx.generated_queries)
            and ctx.ollama_warning is None
        )

    async def run(self, ctx: PipelineContext) -> PipelineContext:
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
            if refined:
                all_queries = ctx.generated_queries + refined
                cache_key = (ctx.query, ctx.config_hash)
                keyword_cache.set(cache_key, all_queries)
                return ctx.evolve(generated_queries=all_queries)
        except OllamaError:
            pass  # Refinement is optional; keep first-round results
        return ctx


def _parse_search_terms(raw: str) -> list[str]:
    """Parse LLM response into FTS5 keyword search terms.

    Tries JSON array first, falls back to line-by-line regex.
    """
    terms: list[str] = []
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    terms.append(item.strip())
    except (ValueError, json.JSONDecodeError):
        pass
    if not terms:
        for line in raw.splitlines():
            line = re.sub(r"\s*\(.*\)\s*$", "", line)
            cleaned = re.sub(r"^[\s\d\.\-\*]+", "", line).strip().strip("`'\"*")
            if cleaned and not cleaned.startswith("#"):
                terms.append(cleaned)
    _BOGUS = frozenset({"json", "[]"})
    return [t.replace("_", " ").strip() for t in terms
            if t and t.lower() not in _BOGUS]
