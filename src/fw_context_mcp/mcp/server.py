"""fw-context MCP server — build-aware code intelligence for embedded C/C++ projects.

Serves 31 MCP tools and 3 MCP resources via FastMCP (stdio transport).

**Search & lookup tools** (delegate to ``fw_context_mcp.search`` pipeline):
``search_code`` (FTS5), ``lookup_symbol`` (exact/prefix), ``smart_search``
(Ollama-driven), ``semantic_search`` (embeddings + cosine similarity).

**Symbol reading tools:** ``get_source``, ``get_file_map``, ``get_symbol_context``,
``explain_symbol``.

**Call graph tools** (require ``--refs`` index): ``find_callers``,
``find_references``, ``find_call_path``, ``find_all_callers_recursive``,
``find_callees_recursive``, ``find_hotspots``, ``find_dead_code``,
``find_wrapper_callers``, ``find_indirect_call_sites``,
``find_indirect_targets``, ``trace_data_flow``.

**Inheritance tools:** ``get_inheritance_chain``, ``get_class_members``,
``get_template_instances``, ``get_method_overrides``.

**Maintenance tools:** ``get_active_build``, ``list_projects``, ``check_ollama``,
``reindex_file``, ``reindex_file_impl``, ``reset_index``.

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
        "CRITICAL: C/C++ questions → fw-context ONLY. This is NOT optional.\n\n"
        "For C/C++ code, use fw-context instead of ANY external search or\n"
        "file-reading tool.\n\n"
        "SELF-CORRECT: the moment you reach for a tool that is NOT fw-context\n"
        "for C/C++ code, stop and use the fw-context equivalent instead.\n\n"
        "TOOL SELECTION (pick the right one):\n"
        '• Symbol by exact/prefix name _____ → lookup_symbol (e.g. "uart_", "main")\n'
        '• Symbols by concept/topic _________ → search_code (e.g. "interrupt handler")\n'
        '• Patterns in function BODIES ______ → search_bodies (e.g. "attach", "rise")\n'
        '• Patterns in full FILE content _____ → search_content (e.g. "extern C", "InterruptIn")\n'
        "• Read function body + callers/callees → get_symbol_context (preferred) / get_source\n"
        "• Function pointer assignments/calls _ → find_indirect_call_sites / find_indirect_targets\n"
        "• Natural-language question ________ → smart_search (slow, thorough)\n\n"
        "IMPORTANT: search_code searches symbol NAMES (what the code IS).\n"
        "search_bodies searches function BODIES (what the code DOES — inside {}).\n"
        "search_content searches FULL FILE content — file-scope declarations,\n"
        "type definitions in headers, preprocessor directives, namespace blocks.\n"
        'For patterns like extern "C", InterruptIn declarations, #define —\n'
        "use search_content, NOT search_bodies.\n\n"
        "FTS5 QUERY TIPS:\n"
        '• Multi-word queries are OR-joined: "attach callback" becomes attach* OR callback*\n'
        "  (matches functions containing EITHER word, not both).\n"
        '• Prefer SINGLE-WORD queries for broad matching: "attach" not "attach callback".\n'
        "• For exact phrases use double quotes: '\"interrupt handler\"'.\n"
        '• Underscores are word separators: "modem_init" → modem AND init.\n'
        '  Write "modem init" instead.\n\n'
        "EMPTY RESULT STRATEGY — if a fw-context tool returns nothing:\n"
        "1. Try a simpler/single-word query in the SAME tool first.\n"
        "2. Switch to a DIFFERENT fw-context tool (search_bodies → search_code, etc.).\n"
        "3. Use lookup_symbol for known symbol names.\n"
        "4. If search_bodies returns empty, switch to search_content — it covers\n"
        '   file scope (type declarations, #define, extern "C") that search_bodies\n'
        "   cannot reach.\n"
        "5. find_callers empty → callers exist through member-field accesses\n"
        "   (obj.method()) or base-class pointers. Fall back to\n"
        '   search_bodies("function_name") which text-searches function bodies\n'
        "   independently of the call graph. If still empty, try\n"
        '   search_content("function_name").\n'
        "6. If all fw-context tools return empty, simplify query or use different tool.\n"
        "7. Only AFTER exhausting fw-context — use other available tools.\n\n"
        "project_only=True (on search_code, search_bodies, search_content, and\n"
        "callgraph tools) excludes vendor SDK code — use when asking about YOUR code.\n\n"
        "ANTI-PATTERNS — do NOT:\n"
        "• Use external search tools for C/C++ symbols → use lookup_symbol or search_code\n"
        "• Use external search tools for code patterns → use search_bodies (function\n"
        "  bodies) or search_content (full file content)\n"
        "• Use external tools for callbacks, ISRs → use find_references or\n"
        "  search_bodies(project_only=True)\n"
        "• Use file readers for function bodies → use get_source (libclang exact extents)\n"
        "• Call get_source + find_callers separately → use get_symbol_context for body,\n"
        "  callers, and callees in one call (fewer round-trips, richer data)\n"
        "• Run external search tools in parallel with fw-context\n"
        "• Give up on fw-context after one empty result → try simpler query or\n"
        "  different fw-context tool first\n"
        "• search_code for a SINGLE KNOWN symbol → use lookup_symbol(exact=true).\n"
        '  search_code FTS5-tokenizes names: "kb_open_disp" → "kb"+"open"+"disp",\n'
        "  causing false matches on unrelated symbols containing those tokens.\n"
        "  search_code is for concept/keyword DISCOVERY only.\n"
        "• Use generic review agents/skills for C/C++ code → use\n"
        "  fw-review skill (see REVIEW SKILL section below).\n\n"
        "AGENT LOOP: Check(get_active_build) → Find(search_code/lookup_symbol)\n"
        "→ Read(get_symbol_context) ← preferred (body+callers+callees in one call).\n"
        "  Fallback: get_source (body only).\n"
        "→ Trace(find_references/find_callers) — skip if context already from get_symbol_context.\n"
        "→ For body patterns use search_bodies.\n"
        "→ DECISION after get_active_build():\n"
        '  • status="ready" or "reindexing" — fw-context is fully operational.\n'
        "    bg_reindex_running does NOT mean the index is unavailable. Continue.\n"
        '  • status="reindex_needed" — queries still work, but schedule fw-context index.\n'
        '  • status="no_index" or "error" — use other available tools.\n\n'
        "REVIEW WORKFLOW — when reviewing C/C++ code changes (per changed symbol):\n"
        "0. find_hotspots(project_only=True) — identify highest-impact functions FIRST.\n"
        "   Prioritize review of hotspots (20+ callers) over leaf functions.\n"
        "1. get_symbol_context(name) — body + direct callers + callees + LLM analysis\n"
        "   in one call. Replaces lookup_symbol + find_callers + find_callees_recursive.\n"
        '2. If get_symbol_context callers are empty → search_bodies("name") and\n'
        "   find_indirect_call_sites / find_indirect_targets (function pointers,\n"
        "   callbacks, ISRs invisible to the call graph).\n"
        "3. find_all_callers_recursive() → transitive upstream impact (full call tree).\n"
        "4. find_callees_recursive() → transitive downstream check.\n"
        "   After a logic change, verify compatibility with everything this function\n"
        "   calls — arguments, init order, error paths may have changed.\n"
        '6. find_references("SymbolName") → all reads/writes/calls of changed types.\n'
        '7. search_content("PATTERN") → confirm removal of #define/#ifdef/board names.\n'
        "8. find_dead_code() → detect newly dead functions after removal.\n"
        "9. trace_data_flow(type_name, to_symbol) → cross-module data dependencies.\n"
        "   Use when a changed function produces or consumes typed data that flows\n"
        '   through other modules. E.g. trace_data_flow("SlotPin", "InventoryWriter").\n'
        "10. For each search_bodies result set: scan ALL results — the 3rd match\n"
        "   may reveal an implementation the diff didn't touch (e.g. duplicate CRC\n"
        "   in a private method).\n"
        "11. When analyzing BOTH a diff AND fw-context results: diff shows SCOPE\n"
        "   (what changed), fw-context verifies CORRECTNESS in full project context.\n"
        "   ALWAYS verify diff discoveries recursively with fw-context.\n\n"
        "DIFF → FW-CONTEXT VERIFICATION RULE:\n"
        "→ When you analyze code via diff (git diff, file diff, patch review),\n"
        "  diff shows ONLY what changed — it cannot reveal the impact across\n"
        "  the full codebase.\n"
        "→ After inspecting a diff: verify your findings with fw-context:\n"
        '  • find_references("<symbol>") — all callers/readers, not just diff context\n'
        '  • search_bodies("<pattern>") — pattern consistency across entire codebase\n'
        "  • find_call_path / find_all_callers_recursive — cross-module impact\n"
        '  • trace_data_flow("<type>", "<target>") — cross-module data dependencies\n'
        "  • find_dead_code / find_hotspots — structural effects of changes\n"
        "→ Do NOT draw conclusions from diff results alone — diff is for SCOPE\n"
        "  discovery, fw-context is for IMPACT verification. They complement each\n"
        "  other; neither replaces the other.\n\n"
        "Search: lookup_symbol, search_code, search_bodies (body text),\n"
        "search_content (full file content), smart_search, semantic_search.\n"
        "Call graph: find_callers, find_references, find_call_path,\n"
        "find_all_callers_recursive, find_callees_recursive, find_hotspots,\n"
        "find_dead_code, find_wrapper_callers, trace_data_flow,\n"
        "find_indirect_call_sites, find_indirect_targets.\n"
        "Inheritance: get_inheritance_chain, get_class_members,\n"
        "get_template_instances, get_method_overrides.\n"
        "Source: get_source, get_symbol_context, get_file_map, explain_symbol.\n"
        "Maintenance: reindex_file, reset_index, check_ollama, list_projects.\n\n"
        "REVIEW SKILL — MANDATORY (not optional): When reviewing C/C++ firmware\n"
        "code (diffs, commits, PRs, changed files), your FIRST action MUST be:\n"
        "  skill(name=\"fw-review\")\n"
        "Do NOT use generic review agents (code-explorer, general, etc.) for\n"
        "C/C++ firmware reviews — they do not know fw-context tool selection rules.\n"
        "Do NOT start inline review without the skill. The skill provides:\n"
        "  • Phase 0: mandatory review plan creation from diff stat\n"
        "  • Phase 1: structural verification (callers, callees, types, inheritance)\n"
        "  • Phase 2: logic & memory review (deep code reading — ODR, null deref,\n"
        "    call ordering, truncation, state reset, watchdog, edge cases)\n"
        "  • Anti-pattern checklist (7 common mistakes with fw-context tools)\n"
        "  • Tool selection decision tree (search_code vs lookup_symbol, etc.)\n"
        "Trigger phrases — this skill applies to ALL review types regardless\n"
        "of what the user calls it: review, code review, PR review, diff\n"
        "review, deep review, recursive review, exhaustive review,\n"
        "comprehensive review, safety review, audit, change analysis, commit\n"
        "analysis, impact analysis, examine changes, inspect this code, look\n"
        "at this diff, check this PR, check these changes, analyze this\n"
        "commit, verify this change.\n"
        "If the project has a LOCAL fw-review skill, that overrides\n"
        "the global default — the user has intentionally customized it.\n\n"
        "Start every session with get_active_build().\n"
        "For non-C/C++ files, general-purpose tools are preferred."
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
mcp.tool()(search.search_bodies)
mcp.tool()(search.search_content)
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
        if "info" in p:
            lines.append(p["info"])
            continue
        if "error" in p:
            lines.append(f"- **{p.get('db', '?')}**: ERROR — {p['error']}")
            continue
        stale = "⚠ reindex_needed" if p.get("reindex_needed") else "✓ ready"
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


# ── Embedded review skill ───────────────────────────────────────────────────


def _load_skill_md() -> str | None:
    """Return the fw-review SKILL.md content, or None."""
    from pathlib import Path

    from .. import __file__ as _pkg_init

    pkg_dir = Path(_pkg_init).parent
    skill_path = pkg_dir / "data" / "skills" / "fw-review" / "SKILL.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except OSError:
        return None


@mcp.resource("fw-context://skills/fw-review")
def resource_embedded_review_skill() -> str:
    """Return the fw-review SKILL.md as an MCP resource.

    Read-only. Makes the embedded firmware review methodology available
    to any MCP client via a well-known resource URI."""
    content = _load_skill_md()
    if content is None:
        return "Skill not found — reinstall with 'fw-context init --force'."
    return content


# @mcp.tool()
# def fw_embedded_review() -> str:
#     """Embedded C/C++ firmware review methodology.
#
#     Read-only. Three-phase review workflow: plan → structural verification
#     (callers/callees/types) → logic review across 9 embedded-specific
#     domains (memory, concurrency, RTOS, peripherals, etc.). Call when the
#     user asks to review embedded firmware changes."""
#     return resource_embedded_review_skill()


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
        log.exception("Background service startup failed — auto-reindex and file watching unavailable")

    mcp.run()
