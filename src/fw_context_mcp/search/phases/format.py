"""Phase 6: Format results for MCP tool output."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.utils import abs_path


class FormatPhase(Phase):
    """Convert final results to the MCP tool output format.

    Adds metadata entries: ``_generated_queries``, ``_rough_queries``,
    ``_translated_from`` / ``_translated_to``, warnings, etc.
    """

    name = "format"  #: Phase identifier used in pipeline configuration.

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Convert final scored results to MCP tool output dicts.

        Prepends metadata entries (``_generated_queries``, ``_rough_queries``,
        ``_translated_from``, warnings), converts relative paths to absolute,
        and includes optional fields (``summary``, ``inputs``, ``outputs``).
        """
        project_root = ctx.project_root

        def _fmt(r: dict) -> dict:
            item: dict = {
                "name": r.get("name", ""),
                "qualified_name": r.get("qualified_name", ""),
                "kind": r.get("kind", ""),
                "file": abs_path(project_root, r.get("file_path", "")),
                "line": r.get("line", 0),
                "is_definition": bool(r.get("is_definition", False)),
                "signature": r.get("signature", ""),
                "docstring": r.get("docstring", ""),
            }
            for field in ("summary", "inputs", "outputs"):
                val = r.get(field, "") or ""
                if val:
                    item[field] = val
            return item

        results: list[dict] = []

        # Metadata entries (always first)
        if ctx.generated_queries:
            results.append({"_generated_queries": ctx.generated_queries})
        if ctx.rough_queries:
            results.append({"_rough_queries": ctx.rough_queries})
        if ctx.translated_from:
            results.append({
                "_translated_from": ctx.translated_from,
                "_translated_to": ctx.query,
            })
        if ctx.ollama_warning:
            results.append(ctx.ollama_warning)
        for w in ctx.warnings:
            results.append({"warning": w})

        # Symbol results
        results += [_fmt(r) for r in ctx.final_results]

        if not ctx.final_results:
            results.append({"info": "No results found for the generated queries."})

        return ctx.evolve(formatted_results=results)
