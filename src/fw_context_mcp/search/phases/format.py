"""Phase: Format results for MCP tool output.

Why metadata entries before symbol results?
    MCP tool consumers (LLMs, IDE plugins) parse the result list linearly.
    Metadata like ``_generated_queries``, ``_translated_from``, and warnings
    provide context for interpreting the symbol results that follow.  Placing
    them first lets consumers see the search intent before the matches.

Why absolute paths?
    Symbol results must be usable by downstream tools (file readers, editors)
    without resolving relative paths.  The ``abs_path()`` conversion ensures
    every result has a fully qualified path regardless of the project root.

Why omit empty optional fields?
    Fields like ``summary``, ``inputs``, ``outputs`` are LLM-generated and
    absent for most symbols.  Including them as empty strings would bloat
    the MCP response (20 results × 3 fields = 60 empty strings).  Omitting
    them keeps responses compact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.utils import abs_path


class FormatPhase(Phase):
    """Convert final results to the MCP tool output format.

    Why the last phase?
        All upstream phases work with raw database dicts — adding metadata
        or formatting paths earlier would require every phase to handle
        these fields.  Formatting once at the end keeps phase code clean.

    Adds metadata entries: ``_generated_queries``, ``_rough_queries``,
    ``_translated_from`` / ``_translated_to``, warnings, etc.
    """

    name = "format"  #: Phase identifier used in pipeline configuration.

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Convert final scored results to MCP tool output dicts.

        Prepends metadata entries (``_generated_queries``, ``_rough_queries``,
        ``_translated_from``, warnings), converts relative paths to absolute,
        and includes optional LLM analysis fields (``summary``, ``inputs``,
        ``outputs``) only when present.
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

        # Metadata entries (always first — consumers parse these before symbols)
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
