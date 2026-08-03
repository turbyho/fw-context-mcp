"""Search MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...config import derive_project_id
from ...config import load as load_config
from ...indexer.db import _expand_query, get_active_config
from ...llm._diag import check_setup
from ...utils import abs_path, resolve_project_root
from ..shared.context import _db_path, _is_stale, _open_db_or_return
from ..shared.fallback import _fallback_to_search_code
from ..shared.stale import _with_stale_recovery
from ._lookup import lookup_symbol
from ._search_fallbacks import (
    _SEARCH_CODE_FALLBACKS,
    _fmt_symbol_rows,
    _search_code_docstring,
    _search_code_fts5_kind,
    _search_code_macros_fts,
    _search_code_name_tokens,
)

log = logging.getLogger(__name__)

__all__ = [
    "_SEARCH_CODE_FALLBACKS",
    "_fmt_symbol_rows",
    "_search_code_docstring",
    "_search_code_fts5_kind",
    "_search_code_macros_fts",
    "_search_code_name_tokens",
    "lookup_symbol",
    "search_code",
    "semantic_search",
    "smart_search",
    "search_bodies",
    "search_content",
]

def _with_search_context(root: Path, tool_name: str, do_search) -> list[dict]:
    """Shared setup/error wrapper for search tools — DRY the resolve→db→stale→error pattern."""
    try:
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
        return _with_stale_recovery(root, db_path, do_search)
    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.exception("%s failed: %s", tool_name, e)
        return [{"error": f"{tool_name} failed: {e}"}]


# Directories considered "application code" by project_only filters.
# REMOVED: _PROJECT_DIRS, _PROJECT_PATH_FILTER, _PROJECT_PATH_FILTER_FILES
# Replaced by s.is_project = 1 / f.is_project = 1 using the DB column.


# ── moved from server.py ──

def _append_staleness_warning(
    results: list[dict], db_path: Path, project_root: Path,
) -> list[dict]:
    """Check compile_commands staleness and append warning if stale."""
    # Connection stays in cache — managed by TTL eviction (same as _with_stale_recovery)
    conn, err_result = _open_db_or_return(db_path)
    if err_result is not None:
        return err_result
    assert conn is not None
    with conn:
        cfg_data = get_active_config(conn, derive_project_id(project_root))
        if cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"])[0]:
            results.append({
                "warning": "Index may be stale — compile_commands.json changed since last index.",
                "hint": "Call reindex_file() on modified files or run 'fw-context index' to update.",
            })
    return results


def search_code(
    query: Annotated[str, Field(description="FTS5 search terms. 1-3 words, omit underscores. E.g. 'modem init' not 'modem_init'. Supports trailing wildcard 'modem*'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
     kind: Annotated[str | None, Field(description="Optional kind filter: function, method, constructor, destructor, class, struct, union, enum, enum_constant, typedef, varglobal, varlocal, variable, field, namespace.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
    project_only: Annotated[bool, Field(description="Exclude vendor SDK code. When True, only application code. Default False.")] = False,
) -> list[dict]:
    """Find C/C++ symbols by name — searches function/class/enum NAMES.

    Searches symbol names, qualified names, signatures, docstrings, and
    pre-computed name tokens (CamelCase/snake_case split).  Does NOT search
    function bodies — for patterns in code like ``.attach(``,
    interrupt handler registrations, callback attachments use ``search_bodies`` instead.

    Use when you know the concept but not the exact name
    (``"interrupt handler"``, ``"modem init"``).  Prefer ``lookup_symbol``
    when you already know the exact or prefix name.

    Results include names, file locations, signatures, and docstrings —
    the metadata about each symbol, not the symbol's implementation code.

    **FTS5 syntax:**
    - ``init*`` matches init, init_uart, initialize (trailing wildcard)
    - ``"spi init"`` matches the exact phrase "spi init"
    - Do NOT use underscore in queries — ``modem_init`` is split into
      ``modem AND init``. Write ``modem init`` instead.

    **Progressive relaxation:** when the initial FTS5 search returns nothing,
    the tool automatically broadens the search in up to six steps:

    1. *FTS5 with kind filter* — the original query with the user-provided
       ``kind`` constraint.
    2. *FTS5 without kind filter* — drops the ``kind`` constraint (users often
       guess the wrong kind for a symbol).
    3. *name_tokens substring match* — searches the pre-computed CamelCase/
       snake_case token column (e.g. ``BuildType`` is indexed as
       ``"build type"``).  Requires at least N‑1 of N query terms to match.
    4. *Single-term docstring LIKE* — when only one query term was given and
       the token-based steps found nothing, does a raw LIKE over the docstring
       column to catch terms the FTS5 tokeniser may have missed.
    5. *Individual term FTS5* — searches each query word separately and merges
       the results.
    6. *Macro FTS5 fallback* — searches the ``macros_fts`` table for matching
       ``#define`` names and values (kind="macro", ``_fallback="macros_fts"``).

    Results from fallback steps carry ``_fallback`` indicating which method
    succeeded (``"fts5"``, ``"name_tokens_like"``, ``"docstring_like"``,
    ``"individual_terms"``, ``"macros_fts"``).

    **Kind filter values:** ``function``, ``method``, ``constructor``,
    ``destructor``, ``class``, ``struct``, ``union``, ``enum``, ``enum_constant``,
    ``typedef``, ``varglobal``, ``varlocal``, ``variable``, ``field``,
    ``namespace``.

    Each result may include ``summary``, ``inputs``, ``outputs``
    when LLM analysis has been generated (``fw-context index --analyze``).
    These provide structured descriptions: what the symbol does, what
    parameters/data it receives, and what it returns/produces.

    Read-only. No side effects.

    Args:
        query: FTS5 search terms. Keep queries short — 1–3 words.
        project_root: Project root directory. Auto-detected from CWD if omitted.
        kind: Optional filter to return only symbols of this kind.
        limit: Maximum results (default 20, max 100).
        project_only: When True, exclude vendor SDK directories and return only
            application code. Default False.

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        is_definition, signature, docstring, is_template, is_virtual,
        is_pure_virtual. Enum constants include ``enum_value`` with the
        integer value. May also include ``template_usr``, ``parent_usr``,
        ``summary``, ``inputs``, ``outputs`` when available. Fallback
        results include ``_fallback`` with the method name.
    """
    root = resolve_project_root(project_root)
    limit = max(0, min(limit, 100))

    def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        result = _search_code_fts5_kind(c, query, config_hash, limit, kind, project_only, root)
        if result is not None:
            return result[0]  # Each fallback returns (data, method_name); data is the result list

        for strategy in _SEARCH_CODE_FALLBACKS:
            # Each fallback returns (data, method_name) tuple — method_name is
            # already in each dict via _fmt_symbol_rows → _fallback key.
            result = strategy(c, query, config_hash, limit, kind, project_only, root)
            if result is not None:
                return result[0]

        return []

    return _with_search_context(root, "search_code", _do_search)



async def smart_search(
    query: Annotated[str, Field(description="Natural language description, 5-15 words. E.g. 'how does the modem connect?' or 'handle BLE pairing failure'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
) -> list[dict]:
    """Natural-language search: an LLM generates FTS5 keywords, then searches
    the libclang index. Finds concepts by meaning rather than exact text
    match. Prefer this when you don't know the exact keywords and want to
    describe what you're looking for ("how does the modem connect?",
    "handle BLE pairing failure").

    Read-only. No side effects. Slow (10-30 s) — delegates to the full
    ``SMART_SEARCH`` pipeline (translate → rough_search → llm_query →
    fts5_search → refine → embedding → adaptive_fusion → deduplicate →
    expand_context → format).

    Multi-phase approach:
    1) Translate non-English queries
    2) Rough search to gather sample symbols for naming conventions
    3) LLM sees those samples + query and generates FTS5 terms
    4) FTS5 search with generated terms
    5) Refine: LLM checks results and course-corrects query terms
    6) Semantic embedding search (cosine similarity re-rank)
    7) Deduplicate, score, and format results

    **When to prefer over search_code:** When you don't know the exact keywords
    and want to describe what you're looking for ("how does the modem connect?",
    "handle BLE pairing failure").

    **Fallback:** When LLM is unavailable, falls back to direct FTS5 search
    with word-split terms from the query.

    Args:
        query: Natural language description of what you're looking for.
               Be specific — 5–15 words works best.
        project_root: Project root directory. Auto-detected from CWD if omitted.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts with metadata entries (_generated_queries, _rough_queries,
        _translated_from) followed by symbol results with name, qualified_name,
        kind, file, line, is_definition, signature, docstring.
    """
    from fw_context_mcp.search.context import PipelineContext
    from fw_context_mcp.search.pipeline import PipelineRunner, _build_smart_search

    try:
        ctx = PipelineContext.create(query=query, project_root=project_root, limit=limit)
    except ValueError as e:
        return [{"error": str(e)}]

    config = _build_smart_search()
    runner = PipelineRunner(config)

    try:
        from fw_context_mcp.config.settings import load as load_settings
        cfg = load_settings()
        timeout = cfg.llm.timeout if cfg and cfg.llm else 120.0
    except (OSError, ValueError):
        timeout = 120.0

    try:
        ctx = await asyncio.wait_for(runner.run(ctx), timeout=timeout)
    except TimeoutError:
        results = list(ctx.formatted_results) if ctx.formatted_results else []
        results.insert(0, {
            "warning": f"Smart search timed out after {timeout:.0f}s.",
            "hint": "Results below are partial (FTS5-only). Try a more specific query or increase LLM timeout.",
            "_partial": True,
        })
        return results

    # Add staleness warning if applicable
    results = list(ctx.formatted_results)
    if ctx.ollama_warning is None:
        results = _append_staleness_warning(results, ctx.db_path, ctx.project_root)
    return results

# ── moved from server.py ──
async def semantic_search(
    query: Annotated[str, Field(description="Natural language description, 5-15 words. E.g. 'parcel locker state machine' or 'how does the modem connect?'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    threshold: Annotated[float, Field(description="Minimum cosine similarity (0.0-1.0). Default 0.60. Use 0.55 for exploratory, 0.50 for broad search.")] = 0.60,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
) -> list[dict]:
    """Semantic search using pre-computed libclang symbol embeddings. Finds
    symbols by meaning, not by text — matches concepts even when query
    words don't appear literally in the code. Uses cosine similarity over
    variable-dimension embeddings generated during ``fw-context index``.
    Dimensions vary by model: mxbai-embed-large → 1024,
    qwen3-embedding → 4096.

    **When to prefer over search_code:** When you're describing a *concept*
    rather than searching for a known keyword.  Examples:
    - ``"parcel locker state"`` finds door-state and shipment methods even
      though "parcel" and "locker" don't appear in their names.
    - ``"cell modem"`` finds ``_socket_t`` and ``ModemMsg*`` classes.
    - ``"delivery box"`` finds ``set_shipment`` and ``get_zrtdata``.
    - ``"power consumption"`` finds ``get_load_power`` and INA260 class.

    **When to prefer search_code instead:** When you know the exact keyword
    or symbol name (``"fram_write"``, ``"cbor encode"``).  FTS5 is faster
    and more precise for lexical matches.

    **Threshold guidance (mxbai-embed-large model):**
    - ``0.50`` — exploratory: more results, lower precision
    - ``0.55`` — balanced (~1000 results)
    - ``0.60`` — precise: ~175 avg, high precision (default)
    - ``0.65`` — strict: few results, may miss relevant symbols

    **Source-aware ranking:** Project code boosted 1.2×, library code
    1.1×, vendored SDK code 0.85×.

    **Requires an LLM** with an embedding model.
    Falls back to ``search_code`` with a warning if the LLM is unavailable.

    Read-only. No side effects.

    Args:
        query: Natural language description of what you're looking for.
               Be specific — 5–15 words works best.
        project_root: Project root. Auto-detected if omitted.
        threshold: Minimum cosine similarity (0.0-1.0). Default 0.60.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        is_definition, signature, docstring, plus ``_similarity`` (cosine
        similarity score) and ``_method`` (``"embedding"`` or
        ``"search_code_fallback"``).
    """

    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = max(0, min(limit, 100))
        threshold = max(0.0, min(1.0, threshold))

        # Check Ollama availability
        cfg = load_config(project_root=root)
        if not cfg.llm.enabled:
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning="LLM is disabled in config. "
                        "Enable it with `[llm] enabled = true` to use semantic search.",
            )

        try:
            setup = check_setup(cfg.llm)
        except (RuntimeError, OSError):
            setup = {"ollama_running": False}

        if not setup.get("ollama_running"):
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning="LLM is not running. Start it to use semantic search.",
            )

        # Delegate to the semantic search pipeline (EmbeddingPhase + FormatPhase).
        # The pipeline handles embedding generation, KNN search, and source-aware
        # boosting internally.
        from fw_context_mcp.search.context import PipelineContext

        try:
            ctx = PipelineContext.create(
                query=query, project_root=str(root), limit=limit
            )
        except ValueError as e:
            return [{"error": str(e)}]

        from fw_context_mcp.search.pipeline import PipelineRunner, _build_semantic_search

        config = _build_semantic_search(threshold, limit * 10)
        runner = PipelineRunner(config)
        ctx = await runner.run(ctx)

        # Extract formatted results
        results: list[dict] = list(ctx.formatted_results) if ctx.formatted_results else []

        # Fallback to search_code when embedding fails or returns nothing
        if ctx.ollama_warning is not None:
            if isinstance(ctx.ollama_warning, dict):
                warning_msg = ctx.ollama_warning.get("detail", "LLM embedding failed.")
            else:
                warning_msg = str(ctx.ollama_warning)
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning=warning_msg,
            )

        if not results:
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning=f"No symbols matched with similarity > {threshold}. "
                        "Try lowering the threshold or rephrasing the query.",
            )

        # Apply reranker when configured
        if cfg.llm.reranker_model and results:
            try:
                from fw_context_mcp.search.reranker import get_reranker
                reranker = get_reranker(cfg.llm.reranker_model)
                if reranker is not None:
                    results = reranker.rank(
                        query, results,
                        min(cfg.index.rerank_top_k, len(results)),
                    )
            except (RuntimeError, ValueError) as e:
                log.warning("Reranker failed, returning unranked results: %s", e)

        # Add staleness warning if applicable
        results = _append_staleness_warning(results, ctx.db_path, ctx.project_root)
        return results

    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.exception("semantic_search failed: %s", e)
        return [{"error": f"semantic_search failed: {e}"}]



def search_bodies(
    query: Annotated[str, Field(description="FTS5 search terms for function bodies. 1-3 words. E.g. 'attach', 'callback', 'rise'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    kind: Annotated[str | None, Field(description="Optional kind filter: function, method, class, etc.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
    project_only: Annotated[bool, Field(description="Exclude vendor SDK code. When True, only application code. Default False.")] = False,
) -> list[dict]:
    """Find patterns in C/C++ function BODIES — the implementation code inside ``{ }``.

    Searches ONLY the text between ``{`` and ``}`` of function/method
    definitions.  Does NOT search file-scope constructs (see Limitations
    below).

    **When to use ``search_bodies`` vs ``search_code``:**

    - ``search_bodies`` — patterns in function BODIES (what the code DOES):
      function call patterns (``.attach(``, ``.rise(``, ``.fall(``,
      ``callback(&``), ISR registration code.
    - ``search_code`` — find symbols by NAME (what the code IS):
      ``modem init``, ``interrupt handler``, ``uart send``.

    **Limitations — what ``search_bodies`` CANNOT find:**

    Only function/method definition bodies are indexed (``is_definition=1``
    symbols with source text).  The following are at FILE SCOPE and are
    NEVER in the ``source`` column:

    - ``extern "C"`` — linkage specifier at file scope
    - Type declarations in headers — ``InterruptIn _pin;`` in class bodies
    - ``#include``, ``#define``, ``#ifdef`` — preprocessor directives
    - Global/static variable definitions outside functions
    - Namespace declarations
    - Any code outside ``{ }`` of a function definition

    **LIMITATION — search_bodies ONLY searches function bodies ({ }):**
    If your pattern might be at file scope (class member declarations like
    ``InterruptIn _pin``, function declarations, ``#define``, ``extern "C"``,
    global variables), use ``search_content`` instead.  search_bodies returns
    empty for any pattern outside function bodies.

    For these patterns, use ``search_content`` which indexes full file
    content (not limited to function bodies).

    **When to set ``project_only=True``:**
    Your project contains two kinds of code:
    - Application code — code your team wrote.
    - Vendor SDK — framework/OS code shipped by a vendor, NOT written by
      your team.

    Set ``project_only=True`` when the question is about YOUR code
    (``"where do we register interrupt handlers?"``,
    ``"which functions call .attach()?"``).  Leave it ``False`` (default)
    when the vendor code is also relevant.

    Results include ``_match_snippet`` — a highlighted excerpt showing
    each match in context (e.g. ``_timeout.<b>attach</b>(callback(...))``).
    Project code sorts before vendor code in the output.

    Read-only. No side effects. Requires the FTS5 index.

    Args:
        query: FTS5 search terms. 1-3 words. Bare multi-word queries are
            OR-joined (each term prefixed with ``*``).  Prefer single-word
            queries for broad matching: ``'attach'`` finds ``.attach(...)``
            patterns including callback attachments, timer registrations, etc.
            For exact phrases wrap in double quotes: ``'\"attach callback\"'``.
        project_root: Project root. Auto-detected if omitted.
        kind: Optional filter to return only symbols of this kind.
        limit: Maximum results (default 20, max 100).
        project_only: When True, exclude vendor SDK directories and return only
            application code. Default False.

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        is_definition, signature, _match_snippet (excerpt around match),
        source (function body, truncated at 2000 chars).
    """
    root = resolve_project_root(project_root)
    limit = max(0, min(limit, 100))
    expanded = _expand_query(query, for_body_search=True)

    def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        kind_filter = ""
        project_filter = ""
        params: list = [expanded, config_hash]
        if kind:
            kind_filter = "AND s.kind = ?"
            params.append(kind)
        if project_only:
            project_filter = "AND s.is_project = 1"
        params.append(limit * 3 if not project_only else limit)

        rows = c.execute(
            f"""SELECT s.*, snippet(symbols_fts, 9, '<b>', '</b>', '…', 60) AS _match_snippet
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = ? AND s.is_definition = 1
                 AND s.source != '' {kind_filter} {project_filter}
                ORDER BY rank
               LIMIT ?""",
            params,
        ).fetchall()

        results = []
        for r in rows:
            d = {
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file": abs_path(root, r["file_path"]),
                "line": r["line"],
                "is_definition": bool(r["is_definition"]),
                "signature": r["signature"],
                "_match_snippet": r["_match_snippet"],
                "_is_project": bool(r["is_project"]),
            }
            source = r["source"]
            if source:
                d["source"] = source[:2000] if len(source) > 2000 else source
            results.append(d)

        if project_only:
            final = results
        else:
            project_results = [r for r in results if r["_is_project"]]
            vendor_results = [r for r in results if not r["_is_project"]]
            final = project_results + vendor_results
        for r in final:
            del r["_is_project"]
        return final[:limit]

    return _with_search_context(root, "search_bodies", _do_search)



def search_content(
    query: Annotated[str, Field(description="FTS5 search terms for full file content. 1-3 words. E.g. 'InterruptIn', 'extern C'. Bare multi-word = OR-joined.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
    project_only: Annotated[bool, Field(description="Exclude vendor SDK code. When True, only application code. Default False.")] = False,
) -> list[dict]:
    """Find patterns in FULL file content — not limited to function bodies.

    Searches **ifdef-filtered** file text — only code that actually compiles
    for the current build configuration.  Inactive ``#ifdef`` branches are
    replaced with blank lines (preserving original line numbers).

    Covers file-scope constructs that ``search_bodies`` cannot see:
    ``extern "C"``, type declarations in headers, ``#include``, ``#define``,
    global variables, namespace blocks.  Also covers function bodies, but
    ``search_bodies`` is preferred for body-level patterns (per-function
    context, snippet highlights per match).

    **When to use ``search_content`` vs ``search_bodies`` vs ``search_code``:**

    - ``search_content`` — patterns anywhere in FILES (file scope + bodies):
      ``extern "C"``, ``InterruptIn``, ``#define``, type declarations.
    - ``search_bodies`` — patterns in function BODIES only:
      ``.attach(``, ``callback(&``, ISR registration patterns.
    - ``search_code`` — find symbols by NAME:
      ``interrupt handler``, ``modem init``.

    **project_only=True** filters to files with ``is_project = 1`` (project code, excluding vendor/SDK).
    Default False includes vendor SDK files.

    Results are file-level (one entry per matching file) — use
    ``search_bodies`` for per-function granularity.

    When ``files_fts`` is missing (legacy index), falls back to LIKE
    search on ``files.content`` — results include ``_fallback: "like"``
    and no snippet highlighting. Run ``fw-context index`` to upgrade.

    Read-only. No side effects. Requires the FTS5 index with file content.

    Args:
        query: FTS5 search terms. 1-3 words. Bare multi-word queries are
            OR-joined (prefix-wildcarded). Prefer single-word queries.
            E.g. ``'InterruptIn'``, ``'extern C'``, ``'#define'``.
        project_root: Project root. Auto-detected if omitted.
        limit: Maximum results (default 20, max 100).
        project_only: When True, filter to project code only (files with is_project = 1).

    Returns:
        list of dicts, each with: file, language, mtime,
        _match_snippet (highlighted excerpt around the match).
    """
    root = resolve_project_root(project_root)
    limit = max(0, min(limit, 100))
    expanded = _expand_query(query)

    def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        project_filter = ""
        if project_only:
            project_filter = "AND f.is_project = 1"

        table_row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files_fts'"
        ).fetchone()

        if table_row is not None:
            rows = c.execute(
                f"""SELECT f.*, snippet(files_fts, 1, '<b>', '</b>', '…', 80) AS _match_snippet
                   FROM files_fts
                   JOIN files f ON f.id = files_fts.rowid
                   WHERE files_fts MATCH ? AND f.config_hash = ?
                     AND f.content != '' {project_filter}
                    ORDER BY rank
                   LIMIT ?""",
                (expanded, config_hash, limit * 3),
            ).fetchall()
        else:
            terms = [t.strip() for t in query.replace("_", " ").split() if t.strip()]
            if not terms:
                return []
            escaped_terms = [t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for t in terms]
            like_clauses = " AND ".join(["f.content LIKE ? ESCAPE '\\'" for _ in escaped_terms])
            like_params = [f"%{t}%" for t in escaped_terms]
            rows = c.execute(
                f"""SELECT f.*,
                           substr(f.content, 0, 200) || '…' AS _match_snippet
                      FROM files f
                     WHERE f.config_hash = ?
                       AND f.content != ''
                       AND {like_clauses}
                       {project_filter}
                     LIMIT ?""",
                (config_hash, *like_params, limit * 3),
            ).fetchall()

        results = []
        for r in rows:
            d = {
                "file": abs_path(root, r["path"]),
                "language": r["language"],
                "mtime": r["mtime"],
                "_match_snippet": r["_match_snippet"],
            }
            if table_row is None:
                d["_fallback"] = "like"
            results.append(d)
        return results[:limit]

    return _with_search_context(root, "search_content", _do_search)
