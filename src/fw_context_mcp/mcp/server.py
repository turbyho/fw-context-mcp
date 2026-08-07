"""fw-context MCP server — build-aware code intelligence for embedded C/C++ projects.

Serves 35 MCP tools and 4 MCP resources via FastMCP (stdio transport).

**Concurrency:** The server processes at most ONE tool request at a time.
If a request arrives while another is already running, the server waits up
to 5 s for the running request to finish.  If it does not finish within
that window, the new request is rejected with a ``"server busy"`` error
that tells the client which tool is currently executing.  This prevents
query pile-up under parallel MCP load (40+ concurrent requests serialized
on the SQLite executor would otherwise cause client-side timeouts and
"Connection closed" errors).

**Search & lookup tools** (delegate to ``fw_context_mcp.search`` pipeline):
``search_code`` (FTS5), ``lookup_symbol`` (exact/prefix), ``smart_search``
(Ollama-driven), ``semantic_search`` (embeddings + cosine similarity),
``search_bodies`` (function body text), ``search_content`` (full file content).

**Symbol reading tools:** ``get_source``, ``get_file_map``, ``get_symbol_context``,
``explain_symbol``, ``read_file``.

**Call graph tools** (require ``--refs`` index): ``find_callers``,
``find_references``, ``find_call_path``, ``find_all_callers_recursive``,
``find_callees_recursive``, ``find_hotspots``, ``find_dead_code``,
``find_wrapper_callers``, ``find_indirect_call_sites``,
``find_indirect_targets``, ``trace_data_flow``.

**Inheritance tools:** ``get_inheritance_chain``, ``get_class_members``,
``get_template_instances``, ``get_method_overrides``.

**Variables:** ``find_variables``.

**Maintenance tools:** ``get_active_build``, ``list_projects``,
``get_project_info``, ``check_ollama``, ``configure_llm``,
``reindex_file``, ``reindex_file_impl``, ``reset_index``.

**MCP Resources:** ``fw-context://stats``, ``fw-context://projects``,
``fw-context://symbols/{name}``.
"""

import asyncio
import functools
import inspect
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import Context as MCPContext
from mcp.server.fastmcp import FastMCP

from ..utils import resolve_project_root
from .background import _ensure_daemon_running
from .handlers import callgraph, inheritance, maintenance, search, source, variables
from .shared.context import _check_server_ready, _integrity_checked

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: Ensures at most one tool handler runs at a time.  Without this, parallel
#: requests pile up on the serialized SQLite executor: 40 concurrent requests
#: × 3 s each = 120 s for request #40 → client timeout → "Connection closed".
#: With the lock, excess requests receive an immediate "server busy" error and
#: the client can retry.  Timeout 5 s — long enough for a just-finished
#: handler to release the lock, short enough to avoid client-side timeouts.
_SERVER_LOCK = asyncio.Lock()
_SERVER_LOCK_TIMEOUT = 5.0  # seconds



def _wrap_debug(msg: str) -> None:
    """Write to /tmp/fw-context-debug.log when FW_CONTEXT_DEBUG_WRAP=1."""
    if os.environ.get("FW_CONTEXT_DEBUG_WRAP") != "1":
        return
    try:
        with open("/tmp/fw-context-debug.log", "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def _wrap_tool(fn):
    """Wrap a tool handler: error boundary + async dispatch + MCP progress.

    Sync handlers are dispatched to a background thread via
    ``asyncio.to_thread``.  The event loop sends MCP progress
    notifications every 5 s to keep the client alive during long-running
    queries (BFS call-graph traversal, etc.).  The 5 s interval is
    deliberate: some clients (e.g. OpenCode) kill the connection after
    ~15 s without progress, so notifications at t=0/5/10 s provide
    coverage — do NOT lengthen the interval.

    All handlers get an error boundary — no unhandled exception can crash
    the server process.
    """
    if inspect.iscoroutinefunction(fn):
        # Manual wrap: copy __name__/__doc__/__module__/__qualname__ but NOT
        # __annotations__ — functools.wraps would overwrite the wrapper's
        # annotations (which include ctx) with fn's annotations (which
        # don't), causing find_context_parameter to miss ctx.
        @functools.wraps(fn, assigned=("__module__", "__name__", "__qualname__", "__doc__"))
        async def _wrapper(*a, ctx: MCPContext | None = None, **kw):
            start = time.monotonic()
            _wrap_debug(f"[async] {fn.__name__} ctx={'P' if ctx is not None else 'N'} ann={list(_wrapper.__annotations__.keys())}")
            try:
                await asyncio.wait_for(_SERVER_LOCK.acquire(), timeout=_SERVER_LOCK_TIMEOUT)
            except TimeoutError:
                _wrap_debug(f"[async] {fn.__name__} BUSY — lock timeout")
                return [{"error": f"The MCP server is currently processing another query ({fn.__name__} cannot run right now because a different tool is already executing). Wait for the active query to complete, then retry. If this error persists, the active query may be stuck — restart the server with `fw-context watch`."}]
            try:
                task = asyncio.ensure_future(fn(*a, **kw))

                while not task.done():
                    done, pending = await asyncio.wait([task], timeout=5.0)
                    if task in done:
                        break
                    elapsed = int(time.monotonic() - start)
                    _wrap_debug(f"[async-loop] {fn.__name__} t={elapsed}s task_done={task.done()}")
                    if ctx is not None:
                        try:
                            await ctx.info(f"{fn.__name__}: {elapsed}s")
                            _wrap_debug(f"[async-loop] {fn.__name__} ctx.info() OK at {elapsed}s")
                        except Exception as e:
                            _wrap_debug(f"[async-loop] {fn.__name__} ctx.info() FAIL: {e}")
                    if elapsed > 300:
                        task.cancel()
                        try:
                            from .shared.context import interrupt_all

                            interrupt_all()
                        except Exception:  # nosec B110 — best-effort interrupt on timeout
                            pass
                        return [{"error": f"Tool {fn.__name__} timed out after {elapsed}s"}]

                exception = task.exception()
                if exception is not None:
                    if isinstance(exception, BrokenPipeError):
                        sys.exit(0)
                    log.exception("Tool %s crashed", fn.__name__)
                    return [{"error": f"Internal server error in {fn.__name__}: {exception}"}]
                return task.result()
            finally:
                _SERVER_LOCK.release()
        return _wrapper

    @functools.wraps(fn, assigned=("__module__", "__name__", "__qualname__", "__doc__"))
    async def _wrapper(*a, ctx: MCPContext | None = None, **kw):
        start = time.monotonic()
        _wrap_debug(f"[sync] {fn.__name__} ctx={'P' if ctx is not None else 'N'} ann={list(_wrapper.__annotations__.keys())}")
        try:
            await asyncio.wait_for(_SERVER_LOCK.acquire(), timeout=_SERVER_LOCK_TIMEOUT)
        except TimeoutError:
            _wrap_debug(f"[sync] {fn.__name__} BUSY — lock timeout")
            return [{"error": f"Server busy — {fn.__name__} cannot run, another query is in progress. Retry in a few seconds."}]
        try:
            task = asyncio.ensure_future(
                asyncio.to_thread(functools.partial(fn, *a, **kw))
            )

            while not task.done():
                done, pending = await asyncio.wait([task], timeout=5.0)
                if task in done:
                    break
                elapsed = int(time.monotonic() - start)
                _wrap_debug(f"[sync-loop] {fn.__name__} t={elapsed}s task_done={task.done()}")
                if ctx is not None:
                    try:
                        await ctx.info(f"{fn.__name__}: {elapsed}s")
                        _wrap_debug(f"[sync-loop] {fn.__name__} ctx.info() OK at {elapsed}s")
                    except Exception as e:
                        _wrap_debug(f"[sync-loop] {fn.__name__} ctx.info() FAIL: {e}")
                if elapsed > 300:
                    task.cancel()
                    try:
                        from .shared.context import interrupt_all

                        interrupt_all()
                    except Exception:  # nosec B110 — best-effort interrupt on timeout
                        pass
                    return [{"error": f"Tool {fn.__name__} timed out after {elapsed}s"}]

            exception = task.exception()
            if exception is not None:
                if isinstance(exception, BrokenPipeError):
                    sys.exit(0)
                log.exception("Tool %s crashed", fn.__name__)
                return [{"error": f"Internal server error in {fn.__name__}: {exception}"}]
            return task.result()
        finally:
            _SERVER_LOCK.release()
    return _wrapper

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
        "• Read a complete file ______________ → read_file\n"
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
        "• Use external file readers for C/C++ files → use read_file (returns\n"
        "  ifdef-filtered content)\n"
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
        "  Fallback: get_source (body only). For whole-file reads: read_file.\n"
        "→ Trace(find_references/find_callers) — skip if context already from get_symbol_context.\n"
        "→ For body patterns use search_bodies.\n"
        "→ DECISION after get_active_build():\n"
        '  • status="ready" or "reindexing" — fw-context is fully operational.\n'
        "    bg_reindex_running does NOT mean the index is unavailable. Continue.\n"
        '  • status="reindex_needed" — queries still work, but schedule fw-context index.\n'
        '  • status="not_initialized" — project not set up. ASK the operator:\n'
        '    "Initialize fw-context? Runs `fw-context init` — creates project ID,\n'
        "    config files (.fw-context/config.toml), and registers with AI tools.\"\n"
        "    Do NOT run without operator confirmation. On approval → run\n"
        "    `fw-context init` via bash, then call get_active_build() again\n"
        "    (returns no_index). init and index are SEPARATE steps — after init,\n"
        "    ask AGAIN before indexing (see no_index below).\n"
        '  • status="no_index" — project initialized but no symbol index. ASK the\n'
        '    operator: "Build the symbol index? Runs `fw-context index --build` which\n'
        "    compiles and parses ALL C/C++ source with libclang — takes several minutes\n"
        "    and uses gigabytes of disk.\" Do NOT run without operator confirmation.\n"
        "    On approval → run `fw-context index --build` via bash (or\n"
        "    `fw-context index <compile_commands.json>` if one exists), then call\n"
        '    get_active_build() again to confirm status="ready".\n'
        "    NOTE: init and index are NEVER combined — each requires its own\n"
        "    operator confirmation before execution.\n"
        '  • status="error" — DB corruption or access error. Use other tools.\n\n'
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
        "Source: get_source, get_symbol_context, get_file_map, explain_symbol, read_file.\n"
        "Maintenance: get_active_build, list_projects, get_project_info,\n"
        "check_ollama, configure_llm, reindex_file, reindex_file_impl,\n"
        "reset_index.\n\n"
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
        "For non-C/C++ files, general-purpose tools are preferred.\n\n"
        "LLM SETUP — after get_active_build returns status='ready', call\n"
        "check_ollama to verify the LLM backend:\n"
        "  • status='ok' — LLM ready. Continue.\n"
        "  • status='not_configured' — Ollama not running, no chat API. ASK:\n"
        "    'Use local Ollama or a cloud API?'\n"
        "    - Local: guide operator to install from https://ollama.com.\n"
        "      For intranet: download installer on an internet machine,\n"
        "      copy to local. Do NOT install Ollama yourself.\n"
        "      After operator confirms Ollama is running, call check_ollama.\n"
        "    - Cloud: ask for URL, key, model. Call configure_llm.\n"
        "      If URL is external, ALWAYS warn: source code will be sent\n"
        "      to that endpoint — ensure compliance with data security.\n"
        "  • status='model_missing' — Chat model not installed.\n"
        "    YOU estimate download size from model name (your knowledge).\n"
        "    Report to operator with caveat: size may be inaccurate.\n"
        "    ALWAYS ASK: 'Download this model, or configure a separate\n"
        "    cloud chat API (no download needed)?'\n"
        "    - Download by me: run `ollama pull {model}` via bash.\n"
        "      On failure, check stderr for network errors (timeout,\n"
        "      unreachable, no such host). If network issue: suggest\n"
        "      offline install — pull on internet machine, copy\n"
        "      ~/.ollama/models/, or download GGUF + ollama create.\n"
        "    - Download manually: tell operator to run `ollama pull\n"
        "      {model}` (NOTE: outside normal flow). Include offline\n"
        "      instructions for intranet.\n"
        "    - Cloud API: ask URL/key/model, call configure_llm.\n"
        "  • status='model_missing' + suggest_cloud=true — No model\n"
        "    ≥7B parameters installed (too small for code tasks).\n"
        "    Check model_details for installed models. Emphasize cloud\n"
        "    API option.\n"
        "  • status='embedding_unavailable' — Chat works (cloud) but\n"
        "    embedding needs Ollama. Warn: semantic search disabled.\n"
        "    Offer to guide Ollama setup.\n"
        "  • chat_api.compliance_warning present — ALWAYS show to operator.\n"
        "    Recommend local or internal API first.\n"
        "Model downloads are LARGE (multi-GB). ALWAYS ask before pulling.\n"
        "auto_pull defaults to false — no automatic downloads on 404.\n"
        "Model size estimates are YOUR judgment — always caveat to operator.\n"
        "configure_llm writes ONLY to <project>/.fw-context/local.toml\n"
        "(gitignored). It does NOT modify global or shared config files.\n"
        "configure_llm returns status='error' — report the error message\n"
        "to the operator. If the test call failed, check API URL/key/model.\n"
        "The config was still written to local.toml — fix the issue and\n"
        "call configure_llm again, or call check_ollama to verify.\n"
        "configure_llm returns status='error' with 'not initialized' —\n"
        "tell operator to run 'fw-context init' in the project root first.\n"
        "Stream option: when the chat API is behind a reverse proxy (nginx,\n"
        "Cloudflare, corporate proxy) that kills idle connections, set\n"
        "stream=true via configure_llm. This sends stream:true and consumes\n"
        "SSE chunks, keeping the connection alive. Default false — non-streaming\n"
        "is sufficient for local Ollama. When false, SSE responses are still\n"
        "auto-detected and parsed as a fallback."
    ),
)

# ── Shared helpers ──────────────────────────────────────────────────────────


# ── Re-exported from .shared.context ──────────────────────────────────────────
# _db_path, _resolve_context, _quick_open_readonly, _is_stale, get_executor,
# invalidate_executor, interrupt_all — see mcp/shared/context.py


# _stale_files, _count_modified_files — re-exported from .shared.stale

# ── Background reindex ──────────────────────────────────────────────────────

# ── File-watch daemon ───────────────────────────────────────────────────────

_SOURCE_EXTS_WATCH = {".c", ".cpp", ".h", ".hpp"}


# ── Tools (non-search) ──────────────────────────────────────────────────────


# ── Graph analytics tools ─────────────────────────────────────────────────────


# ── Shared helpers for SDK path filtering ──────────────────────────────────


# _path_matches, _build_sdk_excludes, _normalize_path_pattern — re-exported from .shared.filtering


# ── Inheritance tool ──────────────────────────────────────────────────────


# ── Pipeline-based search tools ─────────────────────────────────────────────


# _fallback_to_search_code, _fallback_to_search_code_inner — re-exported from .shared.fallback


# ── MCP tool registration (implementations in handlers/) ──────────────

# maintenance.py
mcp.tool()(maintenance.check_ollama)
mcp.tool()(maintenance.configure_llm)
mcp.tool()(maintenance.get_active_build)
mcp.tool()(maintenance.get_project_info)
mcp.tool()(maintenance.list_projects)
mcp.tool()(maintenance.reindex_file)
mcp.tool()(maintenance.reindex_file_impl)
mcp.tool()(maintenance.reset_index)

# search.py
mcp.tool()(_wrap_tool(search.lookup_symbol))
mcp.tool()(_wrap_tool(search.search_code))
mcp.tool()(_wrap_tool(search.search_bodies))
mcp.tool()(_wrap_tool(search.search_content))
mcp.tool()(_wrap_tool(search.semantic_search))
mcp.tool()(_wrap_tool(search.smart_search))

# callgraph.py
mcp.tool()(_wrap_tool(callgraph.find_all_callers_recursive))
mcp.tool()(_wrap_tool(callgraph.find_call_path))
mcp.tool()(_wrap_tool(callgraph.find_callees_recursive))
mcp.tool()(_wrap_tool(callgraph.find_callers))
mcp.tool()(_wrap_tool(callgraph.find_dead_code))
mcp.tool()(_wrap_tool(callgraph.find_hotspots))
mcp.tool()(_wrap_tool(callgraph.find_indirect_call_sites))
mcp.tool()(_wrap_tool(callgraph.find_indirect_targets))
mcp.tool()(_wrap_tool(callgraph.find_references))
mcp.tool()(_wrap_tool(callgraph.find_wrapper_callers))
mcp.tool()(_wrap_tool(callgraph.trace_data_flow))

# source.py
mcp.tool()(_wrap_tool(source.explain_symbol))
mcp.tool()(_wrap_tool(source.get_file_map))
mcp.tool()(_wrap_tool(source.get_source))
mcp.tool()(_wrap_tool(source.get_symbol_context))
mcp.tool()(_wrap_tool(source.read_file))

# inheritance.py
mcp.tool()(_wrap_tool(inheritance.get_class_members))
mcp.tool()(_wrap_tool(inheritance.get_inheritance_chain))
mcp.tool()(_wrap_tool(inheritance.get_method_overrides))
mcp.tool()(_wrap_tool(inheritance.get_template_instances))

# variables.py
mcp.tool()(_wrap_tool(variables.find_variables))

# ── MCP Resources ──────────────────────────────────────────────────────────


@mcp.resource("fw-context://stats")
def resource_stats() -> str:
    """Return a human-readable markdown summary of all indexed projects.

    Read-only. Aggregates stats from every project database found under the
    configured index directory.
    """

    projects = maintenance.list_projects()
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

    return json.dumps(maintenance.list_projects(), indent=2, ensure_ascii=False, default=str)


@mcp.resource("fw-context://symbols/{name}")
def resource_symbol(name: str) -> str:
    """Return the definition source of a symbol as a markdown document.

    Read-only. Renders symbol metadata (name, kind, file, signature) and
    source code as a formatted markdown resource.
    """
    import json

    try:
        result = source.get_source(name)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return json.dumps({"error": f"Failed to read symbol '{name}': {exc}"})
    if "error" in result:
        return json.dumps(result)
    source_code = result.pop("source", "")
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
        source_code,
        "```",
    ]
    return "\n".join(lines)


# ── Embedded review skill ───────────────────────────────────────────────────


def _load_skill_md() -> str | None:
    """Return the fw-review SKILL.md content, or None."""
    skill_path = _DATA_DIR / "skills" / "fw-review" / "SKILL.md"
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
    1. Resolves the project root and validates initialization state.
       - If project root cannot be resolved: sets a sentinel so all tools
         return a clear error (fatal — server cannot operate).
       - If project is not initialized (``fw-context init`` not run): server
         starts normally — ``get_active_build()`` returns ``not_initialized``
         so the agent can ask the operator and run init via bash.  Other
         tools raise ``ProjectNotInitializedError`` (fail-fast).
       - If project is initialized but no index exists: server starts
         normally — ``get_active_build()`` returns ``no_index`` so the
         agent can ask the operator and run index via bash.
    2. When ready, pre-marks the database as integrity-checked.
    3. Ensures the persistent watcher daemon is running for the project
       (spawns it if this is the first MCP server).
    4. Starts a ping thread that keeps the daemon alive.
    """
    log.info("fw-context MCP server starting")

    # Pre-populate the project-ready cache with a one-shot check.
    # _check_server_ready() raises RuntimeError when the project isn't
    # ready — we catch it here so the server can still start (tools will
    try:
        _check_server_ready()
    except RuntimeError as exc:
        log.info("Project not ready at startup: %s", exc)

    # Fast dependency pre-flight — warn on degraded deps, never exit
    try:
        from ..deps import run_preflight
        for r in run_preflight():
            if r.status != "ok":
                log.warning("[%s] %s: %s", r.status.upper(), r.name, r.message)
                if r.fix_cmd:
                    log.warning("  fix: %s", r.fix_cmd)
    except Exception:
        log.debug("Dependency pre-flight skipped", exc_info=True)

    # If the project IS ready, pre-mark integrity check and ensure daemon.
    try:
        root = resolve_project_root(None)
    except OSError:
        log.exception("Failed to resolve project root for daemon setup")
        mcp.run()
        return

    try:
        from ..config import derive_project_id
        from ..config import load as load_config
        from ..config.settings import ProjectNotInitializedError
        project_id = derive_project_id(root)
    except ProjectNotInitializedError:
        log.info("Project not initialized at %s — get_active_build() will report not_initialized", root)
        mcp.run()
        return

    # Check 2: does the index database exist?
    # If not, we DON'T set the sentinel — the server starts normally so
    # that get_active_build() can return status="no_index", allowing the
    # agent to ask the operator and run `fw-context index` via bash.
    cfg = load_config(project_root=root)
    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        log.info("No index found at %s — get_active_build() will report no_index", db_path)
        mcp.run()
        return

    # Project is ready — pre-mark integrity check and start normally.
    # GOTCHA — integrity_check on large DBs is I/O-bound (10-30 s for
    # 3+ GB).  If it runs here via _open_db_safe during MCP server startup,
    # the MCP client times out during tool discovery and fw-context tools
    # never appear.  The check already ran during ``fw-context index``, so
    # we skip it by pre-marking the DB — a corrupt DB will be caught by
    # individual queries via _open_db_safe.
    _integrity_checked.add(str(db_path.resolve()))

    try:
        _ensure_daemon_running(root)
        _start_ping_thread(root)
    except (RuntimeError, OSError):
        log.exception("Background service startup failed — auto-reindex and file watching unavailable")

    mcp.run()


def _start_ping_thread(root: Path) -> None:
    """Start a daemon thread that pings the watcher daemon every 15 s."""
    import threading
    import time

    from .daemon import PING_INTERVAL, ping_daemon

    def _ping_loop() -> None:
        while True:
            time.sleep(PING_INTERVAL)
            try:
                alive = ping_daemon(root)
                if not alive:
                    # Revival handled by _ensure_daemon_running in background.py
                    log.debug("Daemon ping failed — daemon may have exited")
            except OSError:
                log.debug("Daemon ping error", exc_info=True)

    # daemon=True: killed on process exit — no explicit stop mechanism needed
    t = threading.Thread(target=_ping_loop, daemon=True, name="fw-context-ping")
    t.start()
    log.debug("Daemon ping thread started for %s", root)

