"""fw-context MCP server — build-aware code intelligence for embedded C/C++ projects.

Serves 30 MCP tools and 3 MCP resources via FastMCP (stdio transport).

**Search tools** (delegate to ``fw_context_mcp.search`` pipeline):
``search_code`` (FTS5), ``smart_search`` (Ollama-driven), ``semantic_search``
(embeddings + cosine similarity).

**Symbol reading tools:** ``lookup_symbol``, ``get_source``, ``get_file_map``,
``get_symbol_context``, ``explain_symbol``, ``get_file_analysis``.

**Call graph tools** (require ``--refs`` index): ``find_callers``,
``find_references``, ``find_call_path``, ``find_all_callers_recursive``,
``find_callees_recursive``, ``find_hotspots``, ``find_dead_code``,
``find_wrapper_callers``, ``trace_data_flow``.

**Inheritance tools:** ``get_inheritance_chain``, ``get_class_members``,
``get_template_instances``, ``get_method_overrides``.

**Maintenance tools:** ``get_active_build``, ``list_projects``, ``check_ollama``,
``reindex_file``, ``reset_index``.

**MCP Resources:** ``fw-context://stats``, ``fw-context://projects``,
``fw-context://symbols/{name}``.
"""

import logging

from mcp.server.fastmcp import FastMCP

from ..utils import resolve_project_root
from .background import (
    _start_bg_reindex_if_stale,
    _start_bg_watcher,
)
from .handlers import callgraph, inheritance, maintenance, search, source
from .handlers.maintenance import get_active_build, list_projects, reindex_file_impl  # noqa: F401 — backward compat
from .handlers.source import _read_symbol_body, get_source  # noqa: F401 — backward compat
from .shared.context import _db_path, _integrity_checked

log = logging.getLogger(__name__)

mcp = FastMCP(
    "fw-context",
    instructions=(
        "FOR C/C++ EMBEDDED CODEBASES: fw-context tools take priority over generic "
        "file-reading and code-search tools when navigating indexed C/C++ code. "
        "These tools use libclang extents and compile_commands.json — generic tools "
        "cannot match this precision. For non-C/C++ files, general-purpose tools "
        "are preferred.\n\n"
        "Start every session with get_active_build() to check index health.\n\n"
        "Reading symbols (use these instead of generic file readers):\n"
        "• get_source(name) — exact function/method body via libclang\n"
        "• get_symbol_context(name) — body + callers + callees in one call\n"
        "• get_file_map(path) — symbol table of contents for a file\n"
        "• explain_symbol(name) — plain-English description of what a symbol does\n"
        "• get_file_analysis(path) — per-file LLM summary\n\n"
        "Search:\n"
        "• lookup_symbol(name) — by exact or prefix name\n"
        "• search_code(query) — FTS5 keyword search (fast)\n"
        "• smart_search(query) — natural language via Ollama (slow, thorough)\n"
        "• semantic_search(query) — concept/embedding search\n\n"
        "Call graph (--refs index required): find_callers, find_references, "
        "find_call_path, find_all_callers_recursive, find_callees_recursive, "
        "find_hotspots, find_dead_code, find_wrapper_callers, trace_data_flow.\n\n"
        "Inheritance: get_inheritance_chain, get_class_members, "
        "get_template_instances, get_method_overrides.\n\n"
        "Maintenance: reindex_file, reset_index, check_ollama, list_projects."
    ),
)

# ── Shared helpers ──────────────────────────────────────────────────────────


# ── Re-exported from .shared.context ──────────────────────────────────────────
# _db_path, _resolve_context, _open_db_safe, _is_stale — see mcp/shared/context.py






# _stale_files, _count_modified_files, _auto_reindex_stale — re-exported from .shared.stale

# ── Background reindex ──────────────────────────────────────────────────────

# ── File-watch daemon ───────────────────────────────────────────────────────

_SOURCE_EXTS_WATCH = {".c", ".cpp", ".h", ".hpp"}














# ── Tools (non-search) ──────────────────────────────────────────────────────






































# ── Graph analytics tools ─────────────────────────────────────────────────────










# ── Shared helpers for SDK path filtering ──────────────────────────────────


# _path_matches, _build_sdk_excludes, _merge_excludes — re-exported from .shared.filtering










# ── Inheritance tool ──────────────────────────────────────────────────────










# ── Pipeline-based search tools ─────────────────────────────────────────────








# _fallback_to_search_code, _fallback_to_search_code_inner — re-exported from .shared.fallback


# ── MCP tool registration (implementations in handlers/) ──────────────

# maintenance.py
mcp.tool()(maintenance.check_ollama)
mcp.tool()(maintenance.get_active_build)
mcp.tool()(maintenance.list_projects)
mcp.tool()(maintenance.reindex_file)
mcp.tool()(maintenance.reindex_file_impl)
mcp.tool()(maintenance.reset_index)

# search.py
mcp.tool()(search.lookup_symbol)
mcp.tool()(search.search_code)
mcp.tool()(search.semantic_search)
mcp.tool()(search.smart_search)

# callgraph.py
mcp.tool()(callgraph.find_all_callers_recursive)
mcp.tool()(callgraph.find_call_path)
mcp.tool()(callgraph.find_callees_recursive)
mcp.tool()(callgraph.find_callers)
mcp.tool()(callgraph.find_dead_code)
mcp.tool()(callgraph.find_hotspots)
mcp.tool()(callgraph.find_indirect_call_sites)
mcp.tool()(callgraph.find_indirect_targets)
mcp.tool()(callgraph.find_references)
mcp.tool()(callgraph.find_wrapper_callers)
mcp.tool()(callgraph.trace_data_flow)

# source.py
mcp.tool()(source.explain_symbol)
mcp.tool()(source.get_file_analysis)
mcp.tool()(source.get_file_map)
mcp.tool()(source.get_source)
mcp.tool()(source.get_symbol_context)

# inheritance.py
mcp.tool()(inheritance.get_class_members)
mcp.tool()(inheritance.get_inheritance_chain)
mcp.tool()(inheritance.get_method_overrides)
mcp.tool()(inheritance.get_template_instances)

# ── MCP Resources ──────────────────────────────────────────────────────────


@mcp.resource("fw-context://stats")
def resource_stats() -> str:
    """Return a human-readable markdown summary of all indexed projects.

    Read-only. Aggregates stats from every project database found under the
    configured index directory.
    """

    projects = list_projects()
    if not projects:
        return "No indexed projects found."
    lines = [f"# fw-context — {len(projects)} project(s)", ""]
    for p in projects:
        if "error" in p:
            lines.append(f"- **{p.get('db', '?')}**: ERROR — {p['error']}")
            continue
        stale = "⚠ stale" if p.get("stale") else "✓ fresh"
        lines.append(
            f"- **{p['name']}** ({p['project_id']}) — "
            f"{p['symbol_count']} symbols, {p['file_count']} files, "
            f"indexed {p['indexed_at']}, {stale}"
        )
    return "\n".join(lines)


@mcp.resource("fw-context://projects")
def resource_projects() -> str:
    """Return project list as a JSON string.

    Read-only. Uses the same data as ``list_projects``, serialized as
    indented JSON.
    """
    import json
    return json.dumps(list_projects(), indent=2, ensure_ascii=False, default=str)


@mcp.resource("fw-context://symbols/{name}")
def resource_symbol(name: str) -> str:
    """Return the definition source of a symbol as a markdown document.

    Read-only. Renders symbol metadata (name, kind, file, signature) and
    source code as a formatted markdown resource.
    """
    import json

    result = get_source(name)
    if "error" in result:
        return json.dumps(result)
    source = result.pop("source", "")
    # Render as a small markdown document
    lines = [
        f"# {result['name']}",
        "",
        f"- **qualified:** `{result['qualified_name']}`",
        f"- **kind:** {result['kind']}",
        f"- **file:** `{result['file']}:{result['line']}`",
        f"- **signature:** `{result.get('signature', '')}`",
        "",
        "```cpp",
        source,
        "```",
    ]
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────────────


def main() -> None:
    """Start the FastMCP stdio server — entry point for the ``fw-context-mcp`` command.

    On startup:
    1. Pre-marks the database as integrity-checked so ``_open_db_safe`` never
       blocks on ``PRAGMA integrity_check`` (10-30 s on large DBs).  The check
       already ran during ``fw-context index``; re-running it at every server
       start would saturate disk I/O and delay the first MCP query.
    2. Spawns a file-watch daemon that reindexes changed source files on the fly.
    3. If the CWD project index needs work, kicks off a background
       ``fw-context index`` in a daemon thread — ``mcp.run()`` starts
       immediately regardless of index size.  The staleness check uses
       only lightweight COUNT queries and at most one ``stat()`` call.
    """
    try:
        root = resolve_project_root(None)
    except Exception:
        log.exception("Failed to resolve project root, server starting without DB")
        mcp.run()
        return

    # GOTCHA — integrity_check on large DBs is I/O-bound (10-30 s for
    # 3+ GB).  If it runs here via _open_db_safe during MCP server startup,
    # the MCP client times out during tool discovery and fw-context tools
    # never appear.  The check already ran during ``fw-context index``, so
    # we skip it by pre-marking the DB — a corrupt DB will be caught by
    # individual queries via _open_db_safe.
    db_path = _db_path(root)
    if db_path.exists():
        _integrity_checked.add(str(db_path.resolve()))

    try:
        _start_bg_watcher(root)
        # Defer staleness check to a daemon thread so mcp.run() starts
        # immediately.  _start_bg_reindex_if_stale opens the database
        # and stats files — on large projects (3+ GB, 100k+ files)
        # this takes 5-30 s and would cause MCP tool-discovery timeouts.
        import threading
        threading.Thread(
            target=_start_bg_reindex_if_stale,
            args=(root,),
            daemon=True,
            name="fw-context-startup",
        ).start()
    except Exception:
        log.exception(
            "Background service startup failed — "
            "auto-reindex and file watching unavailable"
        )

    mcp.run()
