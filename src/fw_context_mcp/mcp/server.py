"""fw-context MCP server — build-aware code intelligence for embedded C/C++ projects.

Serves 34 MCP tools and 4 MCP resources via FastMCP (stdio transport).

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
(Ollama-driven), ``semantic_search`` (embeddings + cosine similarity).

**Symbol reading tools:** ``get_source``, ``get_file_map``, ``get_symbol_context``,
``explain_symbol``, ``read_file``.

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
                        except Exception:
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
                    except Exception:
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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_instructions() -> str:
    path = _DATA_DIR / "instructions.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.warning("Could not read instructions from %s", path)
        return "fw-context — C/C++ code intelligence server."


mcp = FastMCP(
    "fw-context",
    instructions=_load_instructions(),
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
mcp.tool()(_wrap_tool(maintenance.check_ollama))
mcp.tool()(_wrap_tool(maintenance.get_active_build))
mcp.tool()(_wrap_tool(maintenance.get_project_info))
mcp.tool()(_wrap_tool(maintenance.list_projects))
mcp.tool()(_wrap_tool(maintenance.reindex_file))
mcp.tool()(_wrap_tool(maintenance.reindex_file_impl))
mcp.tool()(_wrap_tool(maintenance.reset_index))

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
    1. Pre-populates the project-ready cache (validates init + index existence).
       Tools will re-evaluate periodically (30 s TTL) so that running
       ``fw-context init`` / ``fw-context index`` while the server is active
       is picked up automatically without a restart.
    2. Pre-marks the database as integrity-checked.
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
        project_id = derive_project_id(root)
        cfg = load_config(project_root=root)
        db_path = cfg.index.db_dir / project_id / "index.db"
        if db_path.exists():
            _integrity_checked.add(str(db_path.resolve()))
    except (OSError, ValueError):
        log.warning("Failed to pre-mark integrity check — will run on first query", exc_info=True)

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

