"""Phase 5: Merge FTS5 + embedding results, score, deduplicate."""

from __future__ import annotations

from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.scoring import score_result, stems_from_queries


class DeduplicatePhase(Phase):
    """Merge FTS5 and embedding results, deduplicate by (name, file_path),
    prefer definitions, and sort by score.
    """

    name = "deduplicate"

    async def run(self, ctx):
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
                s = score_result(r, stems)
                seen[key] = r
                scored.append((s, r))
            elif r.get("is_definition") and not prev.get("is_definition"):
                # Replace declaration with definition
                seen[key] = r
                # Rescore
                for i, (_, existing) in enumerate(scored):
                    if existing.get("name") == name and existing.get("file_path") == r.get("file_path"):
                        scored[i] = (score_result(r, stems), r)
                        break

        scored.sort(key=lambda x: -x[0])
        final = [r for _, r in scored[:ctx.limit]]

        return ctx.evolve(final_results=final)
