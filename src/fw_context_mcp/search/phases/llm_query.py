"""Phase 2: LLM generates FTS5 search terms from query + sample symbols."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from fw_context_mcp.search.cache import keyword_cache
from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class LLMQueryPhase(Phase):
    """Ask Ollama to generate FTS5 prefix search terms.

    Shows the LLM the original query + 12-20 sample symbols from the project
    so it can learn naming conventions (snake_case vs camelCase, prefixes)
    before generating search terms.

    Output format expected from LLM:
        UNDERSTANDING: <one sentence — what subsystem, what they really want>
        QUERIES: ["term1*", "term2*", ...]

    Falls back to rough terms when Ollama is unavailable.
    """

    name = "llm_query"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx) -> bool:
        """Only run when Ollama is enabled in config."""
        return ctx.config.llm.enabled

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Build an LLM prompt from query + sample symbols, call Ollama, parse FTS5 terms.

        Checks the keyword cache first.  Shows the LLM sample symbols so
        it learns naming conventions before generating search terms.
        Parses the ``UNDERSTANDING: ... QUERIES: [...]`` output format.
        Falls back to rough terms on Ollama errors.
        """
        from fw_context_mcp.llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async

        cache_key = (ctx.query, ctx.config_hash)
        cached = keyword_cache.get(cache_key)
        if cached is not None:
            return ctx.evolve(generated_queries=list(cached), llm_understanding="")

        query = ctx.query
        rough_samples = ctx.rough_samples

        if rough_samples:
            context_lines = [
                f"  {r['name']} ({r['kind']}) — {r['file_path']}"
                for r in rough_samples[:15]
            ]
            context_str = "\n".join(context_lines)
            prompt = (
                "You are a C/C++ code search assistant for an embedded firmware project.\n\n"
                "A developer asked:\n"
                f"  «{query}»\n\n"
                "Step 1 — Understand the question. Read the full sentence:\n"
                "- What subsystem/domain? (modem, BLE, storage, sensors, etc.)\n"
                '- Disambiguate words from context: e.g. "registers to the network"\n'
                "  → network registration (modem), NOT hardware register.\n"
                '  "modem connects" → network attach/init, NOT BLE onConnection.\n\n'
                "Step 2 — Generate 3-5 FTS5 prefix search terms.\n"
                "CRITICAL: Look at the actual symbol names in the samples.\n"
                "Use THEIR naming style — snake_case for snake_case code,\n"
                "camelCase for camelCase code. Copy real prefixes, don't\n"
                "invent names. If samples show 'network_registration', use\n"
                "'network_reg*', not 'NetworkRegistration*'.\n"
                "Rules: camelCase = ONE token (match BEGINNINGS), prefer short\n"
                "stems with *, both snake_case AND camelCase, no * except trailing.\n\n"
                "Note: Cortex-M exception handlers use the `_Handler` suffix (e.g.\n"
                "`HardFault_Handler`, `NMI_Handler`, `BusFault_Handler`). When the\n"
                "question is about interrupts, exceptions, or fault handlers, include\n"
                "`handler*` and `fault*` in your queries — these symbols don't contain\n"
                "'irq' or 'isr' in their names.\n\n"
                "Samples (use file paths to identify the right subsystem; samples\n"
                "from unrelated subsystems are noise):\n"
                f"{context_str}\n\n"
                "Output format:\n"
                "UNDERSTANDING: <one sentence — what subsystem, what they really want>\n"
                'QUERIES: ["term1*", "term2*", ...]\n'
            )
        else:
            prompt = (
                "You are a C/C++ code search assistant for an embedded firmware project.\n\n"
                "A developer asked:\n"
                f"  «{query}»\n\n"
                "Step 1 — Understand: what subsystem/domain? Disambiguate words.\n"
                "Step 2 — Generate 3-5 FTS5 prefix queries. Rules: camelCase = one\n"
                "token, match beginnings, short stems with *, both snake_case and\n"
                "camelCase, no * except trailing.\n\n"
                "Note: Cortex-M exception handlers use `_Handler` suffix (e.g.\n"
                "`HardFault_Handler`, `NMI_Handler`, `BusFault_Handler`). When the\n"
                "question is about interrupts or exception handlers, include\n"
                "`handler*` and `fault*` in your queries — they don't contain\n"
                "'irq' or 'isr' in their names.\n\n"
                "Output format:\n"
                "UNDERSTANDING: <one sentence>\n"
                'QUERIES: ["term1*", "term2*", ...]\n'
            )

        try:
            raw = await call_ollama_async(prompt, ctx.config.llm)
            understanding, queries = _parse_understanding_response(raw)
            all_queries = queries[:5] if queries else ctx.rough_queries
        except (OllamaModelNotFoundError, OllamaError) as e:
            keyword_cache.set(cache_key, ctx.rough_queries)
            return ctx.evolve(
                generated_queries=list(ctx.rough_queries),
                ollama_warning={"warning": str(e)},
            )

        keyword_cache.set(cache_key, all_queries)
        return ctx.evolve(
            generated_queries=list(all_queries),
            llm_understanding=understanding,
        )


def _parse_understanding_response(raw: str) -> tuple[str, list[str]]:
    """Parse Phase 2 LLM response into (understanding, queries)."""
    understanding = ""
    queries: list[str] = []
    und_match = re.search(r"UNDERSTANDING:\s*(.+?)(?:\n|$)", raw, re.IGNORECASE)
    if und_match:
        understanding = und_match.group(1).strip()
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    queries.append(item.strip())
    except (ValueError, json.JSONDecodeError):
        pass
    return understanding, queries
