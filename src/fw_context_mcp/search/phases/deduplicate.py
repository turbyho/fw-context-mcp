"""Phase 6: Merge FTS5 + embedding results, score, deduplicate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.scoring import score_result, stems_from_queries

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext


class DeduplicatePhase(Phase):
    """Merge FTS5 and embedding results, deduplicate by (name, file_path),
    prefer definitions, and sort by score.

    Only runs when ``final_results`` is still empty — i.e. ``RRFFusionPhase``
    did not produce output (e.g. no vec0 embeddings available).
    """

    name = "deduplicate"  #: Phase identifier used in pipeline configuration.

    def should_run(self, ctx: PipelineContext) -> bool:
        """Run only when ``final_results`` is still empty.

        In ``SMART_SEARCH``, ``RRFFusionPhase`` runs first and populates
        ``final_results`` — deduplication is then redundant.  Only run
        when no earlier phase produced final results (e.g. embedding-only
        pipelines or fallback paths).
        """
        return not ctx.final_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Merge FTS5 and embedding results, deduplicate, score, and sort.

        Merges both result sets, filters noise (very short variable/field
        names, names starting with ``(``), deduplicates by ``(name,
        file_path)`` preferring definitions, scores each result, and sorts
        by score descending.  Limits to ``ctx.limit`` results.
        """
        queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
        stems = stems_from_queries(queries)

        seen: dict[tuple, dict] = {}
        all_rows = ctx.fts5_results + ctx.embedding_results

        # Score all rows and keep best per (name, file_path)
        scored: list[tuple[int, dict]] = []
        for r in all_rows:
            name = r.get("name") or ""
            # Filter noise
            if name.startswith("("):
                continue
            if len(name) <= 2 and r.get("kind") in ("variable", "field"):
                continue

            key = (name, r.get("file_path"))
            prev = seen.get(key)
            if prev is None:
                source_roots = ctx.config.index.source_roots if ctx.config else None
                s = score_result(r, stems, source_roots=source_roots)
                seen[key] = r
                scored.append((s, r))
            elif r.get("is_definition") and not prev.get("is_definition"):
                # Replace declaration with definition
                seen[key] = r
                # Rescore
                source_roots = ctx.config.index.source_roots if ctx.config else None
                for i, (_, existing) in enumerate(scored):
                    if (existing.get("name") == name
                            and (existing.get("file_path") or "") == (r.get("file_path") or "")):
                        scored[i] = (score_result(r, stems, source_roots=source_roots), r)
                        break

        scored.sort(key=lambda x: -x[0])
        final = [r for _, r in scored[:ctx.limit]]

        return ctx.evolve(final_results=final)
