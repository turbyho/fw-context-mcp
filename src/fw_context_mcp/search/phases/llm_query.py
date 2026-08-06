"""Phase 2: LLM generates FTS5 search terms from query + sample symbols."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from fw_context_mcp.search.cache import keyword_cache
from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


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
            queries, understanding = cached
            return ctx.evolve(generated_queries=list(queries), llm_understanding=understanding)

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
                "CRITICAL OUTPUT RULES — plaintext only, NO markdown formatting:\n"
                "• Never use **bold**, __underline__, or ```code fences``` in your response.\n"
                "• QUERIES must be a valid JSON array of strings on ONE line.\n"
                "• Output EXACTLY two lines — nothing else before or after.\n\n"
                "Example correct output:\n"
                "UNDERSTANDING: The developer wants to find all DMA transfer functions.\n"
                'QUERIES: ["dma*", "transfer*", "channel*"]\n\n'
                "Now respond with the same format for the query above.\n"
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
                "CRITICAL OUTPUT RULES — plaintext only, NO markdown formatting:\n"
                "• Never use **bold**, __underline__, or ```code fences``` in your response.\n"
                "• QUERIES must be a valid JSON array of strings on ONE line.\n"
                "• Output EXACTLY two lines — nothing else before or after.\n\n"
                "Example correct output:\n"
                "UNDERSTANDING: The developer wants to find all DMA transfer functions.\n"
                'QUERIES: ["dma*", "transfer*", "channel*"]\n\n'
                "Now respond with the same format for the query above.\n"
            )

        try:
            raw = await call_ollama_async(prompt, ctx.config.llm)
            understanding, queries = _parse_understanding_response(raw)
            if queries:
                all_queries = queries[:5]
            else:
                all_queries = ctx.rough_queries
                log.warning("LLM response parse failed — falling back to rough queries")
            is_fallback = not queries  # don't cache LLM failures — retry next time
        except (OllamaModelNotFoundError, OllamaError) as e:
            return ctx.evolve(
                generated_queries=list(ctx.rough_queries),
                ollama_warning={"warning": str(e)},
            )

        if not is_fallback:
            keyword_cache.set(cache_key, all_queries, understanding)
        return ctx.evolve(
            generated_queries=list(all_queries),
            llm_understanding=understanding,
        )


def _parse_understanding_response(raw: str) -> tuple[str, list[str]]:
    """Parse Phase 2 LLM response into (understanding, queries)."""
    understanding = ""
    queries: list[str] = []
    # Strip ANSI escape sequences (ollama injects cursor-move codes) and markdown bold
    stripped = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw)
    stripped = re.sub(r"\*\*", "", stripped)
    stripped = re.sub(r"__", "", stripped)
    # Match UNDERSTANDING up to the QUERIES: line (handles multi-line understanding)
    und_match = re.search(
        r"UNDERSTANDING:\s*(.+?)\n(?=QUERIES:)", stripped, re.IGNORECASE | re.DOTALL
    )
    if und_match:
        understanding = " ".join(und_match.group(1).split())
    else:
        und_match = re.search(r"UNDERSTANDING:\s*(.+?)(?:\n|$)", stripped, re.IGNORECASE)
        if und_match:
            understanding = und_match.group(1).strip()
    try:
        start = stripped.index("[")
        end = stripped.rindex("]") + 1
        parsed = json.loads(stripped[start:end])
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    queries.append(item.strip())
    except (ValueError, json.JSONDecodeError):
        for match in re.finditer(r"[-*]\s*`?(\w+[*\w]*)`?", stripped):
            term = match.group(1).strip()
            if term and term not in queries:
                queries.append(term)
    return understanding, queries
