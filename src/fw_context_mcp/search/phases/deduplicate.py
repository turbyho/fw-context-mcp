"""Phase: Merge FTS5 + embedding results, score, deduplicate.

Why deduplicate by (name, file_path)?
    The same symbol can appear in both FTS5 and embedding result sets.
    Simply concatenating would produce duplicates.  The ``(name, file_path)``
    key identifies the same symbol across result sets — different from
    USR-based deduplication because results may come as raw database rows
    without a USR field.

Why prefer definitions over declarations?
    A declaration (``void init();`` in a header) and its definition
    (``void init() { ... }`` in a source file) have the same ``(name,
    file_path)`` key but different ``is_definition`` values.  Definitions
    are more useful — they show the implementation, not just the signature.

Why filter short variable/field names?
    Two-character variable names (``i``, ``j``, ``rc``) are overwhelmingly
    loop counters and local temporaries.  Including them in search results
    adds noise without signal.  Filtering by kind (varlocal, variable, field)
    with length ≤ 2 removes these while keeping short function names
    (``init``, ``run``, ``go``) which are legitimate search targets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.scoring import score_result, stems_from_queries

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class DeduplicatePhase(Phase):
    """Merge FTS5 and embedding results, deduplicate by (name, file_path),
    prefer definitions, and sort by score.

    Why only run when ``final_results`` is still empty?
        In SMART_SEARCH, ``AdaptiveFusionPhase`` runs first and populates
        ``final_results`` — deduplication is then redundant (the fusion
        phase already picked one source).  This phase only activates
        when no earlier phase produced final results (e.g. embedding-only
        pipelines or SEARCH_CODE fallback paths).

    Only runs when ``final_results`` is still empty — i.e.
    ``AdaptiveFusionPhase`` did not produce output (e.g. no embeddings
    available).
    """

    name = "deduplicate"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx: PipelineContext) -> bool:
        """Run only when no prior phase produced final results.

        In ``SMART_SEARCH``, ``AdaptiveFusionPhase`` runs first and populates
        ``final_results`` — deduplication is then redundant.  Only run
        when no earlier phase produced final results (e.g. embedding-only
        pipelines or fallback paths).
        """
        return not ctx.final_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Merge FTS5 and embedding results, deduplicate, score, and sort.

        Why scored_index for O(1) replacement?
            When a declaration is found before its definition, replacing
            it requires finding the old entry.  ``scored_index[key]`` maps
            the ``(name, file_path)`` key to the list index, enabling O(1)
            replacement instead of O(n) scanning.

        Merges both result sets, prevents duplicates, prefers definitions,
        and filters noise.  Scores via ``score_result()`` and sorts
        by score descending.
        """
        queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
        stems = stems_from_queries(queries)

        seen: dict[tuple, dict] = {}
        all_rows = ctx.fts5_results + ctx.embedding_results

        # Score all rows and keep best per (name, file_path)
        scored: list[tuple[int, dict]] = []
        scored_index: dict[tuple, int] = {}
        for r in all_rows:
            name = r.get("name") or ""
            # Filter noise: names starting with '(' are malformed
            # (usually lambda or anonymous types that slipped through)
            if name.startswith("("):
                continue
            # Short variable/field names (≤2 chars) are noise —
            # loop counters, temps, single-letter locals
            if len(name) <= 2 and r.get("kind") in ("varlocal", "variable", "field"):
                continue

            key = (name, r.get("file_path"))
            prev = seen.get(key)
            if prev is None:
                s = score_result(r, stems)
                seen[key] = r
                scored_index[key] = len(scored)
                scored.append((s, r))
            elif r.get("is_definition") and not prev.get("is_definition"):
                # Replace declaration with definition — O(1) via scored_index
                seen[key] = r
                idx = scored_index.get(key)
                if idx is not None:
                    scored[idx] = (score_result(r, stems), r)

        scored.sort(key=lambda x: -x[0])
        final = [r for _, r in scored[:ctx.limit]]

        return ctx.evolve(final_results=final)
