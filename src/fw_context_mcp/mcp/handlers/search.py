"""Search handler functions for the MCP tool server.

Provides the search tools exposed through the MCP interface:

- ``search_code`` — lexical symbol-name search with progressive relaxation;
  searches symbol names, signatures, and docstrings via FTS5 with six
  fallback strategies when the primary query returns nothing.
- ``search_bodies`` — full-text search over the stored text of every
  definition, callables and types alike.  Project code sorts before vendor
  code.
- ``search_content`` — full-text search over complete ifdef-filtered file
  content, including the text that belongs to no definition and that
  ``search_bodies`` cannot see (preprocessor directives, ``extern "C"``).
- ``smart_search`` — LLM-powered natural-language to FTS5 translation
  pipeline; async because it calls external chat and embedding APIs.
- ``semantic_search`` — embedding-based conceptual search using pre-computed
  vector embeddings and cosine similarity; async because embedding generation
  is I/O-bound.
- ``lookup_symbol`` — exact/prefix symbol lookup (re-exported from
  ``._lookup``); synchronous — only local DB queries.

Also exports shared fallback utilities from ``._search_fallbacks`` for
use by other modules that need to run search-fallback chains independently
(e.g. ``semantic_search`` falls back to ``search_code`` on embedding
failure).

Architecture: every search handler delegates to ``_with_search_context``
which resolves the project root, verifies index existence, and wraps the
actual query in ``_with_stale_recovery`` — a retry-on-stale-index
decorator that triggers per-file reparsing when compile-commands
mismatches cause stale-symbol errors.
"""

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
from ..shared.context import _db_path, _is_stale, _quick_open_readonly
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


def _resolve_scopes_for_search(root: Path, db_path: Path, variant: str, image: str):
    """Resolve ``(variant, image)`` → ``(scopes, multi, error)`` for search tools.

    Loads the project config, reads the project ID, and delegates to the shared
    ``resolve_scopes`` helper on a short-lived read-only connection.
    """
    from ..shared.variants import resolve_scopes

    project_id = derive_project_id(root)
    cfg = load_config(root)
    conn = _quick_open_readonly(db_path)
    try:
        return resolve_scopes(conn, project_id, cfg, variant or "", image or "")
    finally:
        conn.close()


__all__ = [
    "_SEARCH_CODE_FALLBACKS",
    "_fmt_symbol_rows",
    "_search_code_docstring",
    "_search_code_fts5_kind",
    "_search_code_macros_fts",
    "_search_code_name_tokens",
    "lookup_symbol",
    "search_bodies",
    "search_code",
    "search_content",
    "semantic_search",
    "smart_search",
]

def _with_search_context(root: Path, tool_name: str, do_search, variant: str = "", image: str = "") -> list[dict]:
    """Resolve project root, verify index existence, and execute search with stale recovery.

    Every search handler follows the same setup sequence:
    1. Locate the SQLite index database file from the project root.
    2. Verify the index exists — fail early with a clear message if not.
    3. Run the actual search inside ``_with_stale_recovery``, which catches
       stale-index errors and retries once after triggering per-file
       re-parsing behind the scenes.

    This wrapper eliminates five repeated lines from every search function
    and ensures consistent error formatting. The ``tool_name`` parameter
    appears in error messages so the operator knows which operation failed.

    Why stale-recovery is used: the compile-commands database can change
    between index runs (files added, removed, re-ordered). When a query
    hits a symbol whose source position is outdated, the stale error
    propagates as an empty or partial result. ``_with_stale_recovery``
    detects this, triggers ``reindex_file_impl`` on the affected file, and
    re-runs ``do_search`` — transparent to the caller.

    Args:
        root: Resolved project root directory.
        tool_name: Human-readable name for the tool (used in error messages).
        do_search: Callable ``(sqlite3.Connection, str) -> list[dict]`` that
            receives an open read-only connection and the config_hash value,
            performs the search, and returns results.

    Returns:
        List of result dicts on success, or a single-element list with an
        ``"error"`` key and error description on failure.
    """
    try:
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        # Resolve (variant, image) selection → build scopes (fail-closed).
        scopes, multi, err = _resolve_scopes_for_search(root, db_path, variant, image)
        if err:
            return [{"error": err}]
        if len(scopes) == 1:
            return _with_stale_recovery(root, db_path, do_search, config_hash=scopes[0]["config_hash"])

        # Multi-scope: run per build and annotate each result with its identity.
        merged: list[dict] = []
        for scope in scopes:
            part = _with_stale_recovery(root, db_path, do_search, config_hash=scope["config_hash"])
            for r in part:
                if isinstance(r, dict) and "error" not in r and "warning" not in r:
                    r = dict(r)
                    r["variant"] = scope["variant"]
                    r["image"] = scope["image"]
                merged.append(r)
        return merged
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
    """Check compile_commands staleness and append a warning if the index is stale.

    When ``compile_commands.json`` changes after indexing (files added,
    removed, or re-ordered), the indexed source positions, symbol extents,
    and conditional-compilation state may be outdated. This function detects
    that condition and appends a warning dict so the LLM can inform the
    operator that a reindex is needed.

    Uses ``_quick_open_readonly`` instead of the full ``open_db`` path
    because ``open_db`` runs ``ensure_schema`` — a write transaction —
    which is unnecessary overhead for reading two config columns.

    Args:
        results: Existing result list to append the warning to (mutated in place).
        db_path: Path to the project's SQLite index database file.
        project_root: Project root directory, used to derive the project_id
            for the config lookup.

    Returns:
        The results list, possibly extended with a ``warning`` dict.
    """
    conn = _quick_open_readonly(db_path)
    try:
        cfg_data = get_active_config(conn, derive_project_id(project_root))
        if cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"])[0]:
            results.append({
                "warning": "Index may be stale — compile_commands.json changed since last index.",
                "hint": "Call reindex_file() on modified files or run 'fw-context index' to update.",
            })
    finally:
        conn.close()
    return results


def search_code(
    query: Annotated[str, Field(description="FTS5 search terms. 1-3 words, omit underscores. E.g. 'modem init' not 'modem_init'. Supports trailing wildcard 'modem*'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
     kind: Annotated[str | None, Field(description="Optional kind filter: function, method, constructor, destructor, class, struct, union, enum, enum_constant, typedef, varglobal, varlocal, variable, field, namespace.")] = None,
     limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
     project_only: Annotated[bool, Field(description="Exclude vendor SDK code. When True, only application code. Default False.")] = False,
     variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
     image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
 ) -> list[dict]:
    """Find C/C++ symbols by name — searches function/class/enum NAMES.

    Searches symbol names, qualified names, signatures, docstrings, and
    pre-computed name tokens (CamelCase/snake_case split).  Does NOT search
    function bodies — for patterns in code like ``.attach(``,
    interrupt handler registrations, callback attachments use ``search_bodies`` instead.

    Use when you know the concept but not the exact name
    (``"interrupt handler"``, ``"modem init"``).  Prefer ``lookup_symbol``
    when you already know the exact or prefix name.

    Results hold the metadata of each symbol — name, location, signature,
    docstring — not its implementation code.

    **FTS5 syntax:**
    - ``init*`` matches init, init_uart, initialize (trailing wildcard)
    - ``"spi init"`` matches the exact phrase "spi init"
    - Do NOT use underscore in queries — ``modem_init`` is split into
      ``modem AND init``. Write ``modem init`` instead.

    **Progressive relaxation:** when FTS5 finds nothing, the search widens
    in up to six steps, and every result carries the ``_fallback`` method
    that found it:

    1. FTS5 with the ``kind`` filter — ``_fallback="fts5"``.
    2. FTS5 without it; operators often guess the wrong kind.
    3. ``name_tokens`` substring match over the pre-computed CamelCase /
       snake_case tokens (``BuildType`` is indexed as ``"build type"``).
       Needs N−1 of N query terms — ``"name_tokens_like"``.
    4. LIKE over the docstring column, for a single-term query that the
       token steps missed — ``"docstring_like"``.
    5. FTS5 per query word, results merged — ``"individual_terms"``.
    6. ``macros_fts`` for ``#define`` names and values, kind="macro" —
       ``"macros_fts"``.

    **Kind filter values:** ``function``, ``method``, ``constructor``,
    ``destructor``, ``class``, ``struct``, ``union``, ``enum``, ``enum_constant``,
    ``typedef``, ``varglobal``, ``varlocal``, ``variable``, ``field``,
    ``namespace``.

    **Precision — a local variable matches through its parent.**  FTS5
    indexes the qualified name, thus a query for ``battery`` also returns
    ``V``, ``ret``, and ``tmp_value`` when they live inside
    ``get_battery_voltage``: their qualified name holds the term, their own
    name does not.  Measured on one project, 4 of 20 results for one query
    were locals of a single function.  Pass ``kind`` to exclude them, or
    read ``kind`` on each result before you act on it.

    After ``fw-context index --analyze``, a result also holds
    ``llm_analysis`` — ``{summary, inputs, outputs}``.  A model wrote that
    text, and the code did not.  Treat it as a hint that points you at a
    symbol, never as a fact to quote.  Quote ``source`` from
    ``get_source``, ``signature``, or ``docstring``.

    Read-only. No side effects.

    Args:
        query: FTS5 search terms. Keep queries short — 1–3 words.
        project_root: Project root directory. Auto-detected from CWD if omitted.
        kind: Optional filter to return only symbols of this kind.
        limit: Maximum results (default 20, max 100).
        project_only: When True, exclude vendor SDK directories and return only
            application code. Default False.
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        is_definition, signature, docstring, is_template, is_virtual,
        is_pure_virtual. Enum constants include ``enum_value`` with the
        integer value. May also include ``template_usr``, ``parent_usr``,
        and ``llm_analysis`` (``{summary, inputs, outputs}`` — written by a
        model, not by the code.  ``get_active_build().analysis.model`` names
        it). Fallback results include ``_fallback`` with the method name.

        No match gives ``[]``.  A dict with ``error`` means the query
        failed.  A stale index prepends a dict with ``warning`` + ``hint``.
    """
    root = resolve_project_root(project_root)
    limit = max(0, min(limit, 100))

    def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        """Execute the six-step progressive relaxation search for ``search_code``.

        Step 1 — FTS5 with kind filter: the fastest and most accurate path.
        When the operator specifies a ``kind`` (e.g. ``"function"``,
        ``"method"``), the query is run with both the text match and the
        kind constraint. If this returns nothing, the kind filter is
        dropped because operators often guess the wrong kind for a symbol.

        Steps 2–6 — progressive fallbacks (imported from ``_search_fallbacks``):
        2. FTS5 without kind — drops the kind constraint entirely.
        3. ``name_tokens`` LIKE — searches pre-computed CamelCase/snake_case
           token splits (e.g. ``BuildType`` → ``"build type"``). Requires
           at least N‑1 of N query terms to match.
        4. Single-term docstring LIKE — catches terms the FTS5 tokenizer
           may have missed (only when one query term was given and earlier
           steps returned nothing).
        5. Individual term FTS5 — searches each query word separately and
           merges the result sets.
        6. Macro FTS5 — searches the ``macros_fts`` table for matching
           ``#define`` names and values (kind="macro").

        Each fallback returns a ``(data, method_name)`` tuple. The method
        name is embedded in each result dict via the ``_fallback`` key by
        ``_fmt_symbol_rows`` (called by each fallback), so the caller only
        needs to extract ``result[0]`` — the result list.

        The cascade stops at the first non-None result. Earlier strategies
        are faster and more precise; later ones are broader but slower.
        This ordering means the operator gets the best match with minimal
        overhead when the query is well-formed, and progressively broader
        searches when the initial query misses.

        Why progressive relaxation instead of returning empty: operators
        often search with incomplete information ("I think it's called
        something like 'uart init'"). Returning empty on a near-match is
        worse than returning slightly noisy results with a ``_fallback``
        marker that the LLM can use to qualify its confidence.
        """
        result = _search_code_fts5_kind(c, query, config_hash, limit, kind, project_only, root)
        if result is not None:
            # Each fallback returns (data, method_name) tuple — data is the
            # result list, method_name is already embedded in each dict via
            # _fmt_symbol_rows → _fallback key.
            return result[0]

        # Try each progressive fallback in order — first non-None result wins.
        # _SEARCH_CODE_FALLBACKS is a list of callables ordered from
        # fastest/most-precise to slowest/broadest.
        for strategy in _SEARCH_CODE_FALLBACKS:
            result = strategy(c, query, config_hash, limit, kind, project_only, root)
            if result is not None:
                return result[0]

        return []

    return _with_search_context(root, "search_code", _do_search, variant or "", image or "")



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

        When the LLM stalls, the tool gives the FTS5 results that it has.
        The leading dict then holds ``_partial: True``, a ``warning`` with
        the timeout, and a ``hint``.  The result is incomplete: make the
        query more specific, or increase the LLM timeout.

        When the index is stale, a leading dict holds a ``warning`` and a
        ``hint`` to reindex.

        No match gives ``[]``.  One dict with ``error`` means the query
        failed — check that key first.
    """
    from fw_context_mcp.search.context import PipelineContext
    from fw_context_mcp.search.pipeline import PipelineRunner, _build_smart_search

    try:
        ctx = PipelineContext.create(query=query, project_root=project_root, limit=limit)
    except ValueError as e:
        return [{"error": str(e)}]

    config = _build_smart_search()
    runner = PipelineRunner(config)

    # Load pipeline timeout from config — smart_search involves multiple
    # LLM round-trips (translate, rough_search, llm_query, refine, embedding)
    # each of which can stall. A hard timeout prevents runaway sessions.
    try:
        from fw_context_mcp.config.settings import load as load_settings
        cfg = load_settings()
        timeout = cfg.llm.timeout if cfg and cfg.llm else 120.0
    except (OSError, ValueError):
        timeout = 120.0

    try:
        # await is required because the pipeline calls external chat and
        # embedding APIs — synchronous calls would block the event loop
        # for 10–30 seconds per invocation.
        ctx = await asyncio.wait_for(runner.run(ctx), timeout=timeout)
    except TimeoutError:
        # On timeout, return whatever FTS5 results were collected before the
        # LLM stalled — partial results are better than nothing. The warning
        # tells the operator the results are incomplete.
        results = list(ctx.formatted_results) if ctx.formatted_results else []
        results.insert(0, {
            "warning": f"Smart search timed out after {timeout:.0f}s.",
            "hint": "Results below are partial (FTS5-only). Try a more specific query or increase LLM timeout.",
            "_partial": True,
        })
        return results

    # Flatten the pipeline's formatted results iterator into a list.
    results = list(ctx.formatted_results)
    # Append staleness warning unless the LLM setup itself failed — in that
    # case ctx.ollama_warning is set and the results already carry a warning.
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

        When the best similarity is below the relevance floor (0.68), the
        result is one dict with ``warning``, ``_best_similarity`` (float),
        ``_fallback_suggestion`` (``"search_code"``), and ``_results`` (the
        low-similarity results).  Treat those results as noise, and use
        ``search_code`` instead.

        When the LLM is not running, or the embedding fails, this tool falls
        back to ``search_code``.  The results then carry
        ``_method: "search_code_fallback"``, and a leading dict holds a
        ``warning`` with the reason.

        No match gives ``[]``.  One dict with ``error`` means the query
        failed — check that key first.
    """

    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = max(0, min(limit, 100))
        threshold = max(0.0, min(1.0, threshold))

        # Verify LLM setup — embedding generation runs against the configured
        # Ollama instance or cloud API. Without an active LLM, embedding-based
        # search cannot produce vectors, so we fall back to search_code
        # (lexical FTS5) with a warning.
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

        # Build the semantic pipeline with an oversampling factor of 10×.
        # The KNN query fetches limit * 10 candidates from the vector DB,
        # then cosine-similarity filtering at the requested threshold and
        # source-aware boosting prune to the final limit. Oversampling
        # compensates for the post-filter — many candidates will be vendor
        # code that gets down-weighted by source-aware scoring.
        config = _build_semantic_search(threshold, limit * 10)
        runner = PipelineRunner(config)
        # await is used because embedding generation requires an HTTP call
        # to the embedding model — it is I/O-bound, not CPU-bound.
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
        # Apply LLM-based reranker when configured.
        # The reranker re-scores the top-N embedding results using a
        # cross-encoder model, producing a relevance-ranked list. This
        # is optional — without it, cosine similarity is the sole signal.
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

        # Relevance floor: when the best cosine-similarity score is below
        # 0.68, the results are likely unrelated to the query. Embedding
        # models can return low-similarity matches for any input — this
        # threshold prevents the LLM from treating noise as signal.
        # The warning suggests search_code (lexical) as a better alternative.
        _RELEVANCE_FLOOR = 0.68
        if results and results[0].get("_similarity", 1.0) < _RELEVANCE_FLOOR:
            best_score = results[0].get("_similarity", 0)
            return [
                {
                    "warning": (
                        f"Best result similarity ({best_score:.3f}) is below relevance "
                        f"floor ({_RELEVANCE_FLOOR}). These results are likely unrelated. "
                        "Try search_code for keyword-based search."
                    ),
                    "_fallback_suggestion": "search_code",
                    "_best_similarity": best_score,
                    "_results": results,
                },
            ]

        # Add staleness warning if applicable
        results = _append_staleness_warning(results, ctx.db_path, ctx.project_root)
        return results

    except (sqlite3.Error, OSError, RuntimeError) as e:
        log.exception("semantic_search failed: %s", e)
        return [{"error": f"semantic_search failed: {e}"}]



# Body text budget for one search_bodies result.  A callable body is the
# answer to "what does this code do", thus it gets the larger share.  The
# body of a type is mostly members that have nothing to do with the match —
# measured on one project, a match on a single bitfield returned about 1900
# characters of unrelated enum — and `_match_snippet` already carries the
# match in context, thus the head of the declaration is enough to identify
# the type.
_SOURCE_CAP_CALLABLE = 2000
_SOURCE_CAP_TYPE = 500
_CALLABLE_KINDS = frozenset({"function", "method", "constructor", "destructor"})

# Cap on the line numbers reported for one result.  A common term appears on
# many lines of a long body, and the caller needs enough anchors to find the
# code, not all of them.
_MATCH_LINES_CAP = 20

# FTS5 operators.  They are syntax and carry no text to find in a body.
_FTS5_OPERATORS = frozenset({"and", "or", "not", "near"})


def _body_query_terms(query: str) -> list[str]:
    """Reduce an FTS5 query to the plain terms to look for in a body.

    The terms come from the raw query and not from the expanded form: the
    expansion adds wildcards and operators, which never appear in source
    text.  Quotes and a trailing ``*`` go for the same reason.

    Underscores stay.  FTS5 splits on them, thus a query for ``batt test``
    must match ``_is_batt_test``.  A substring test for each term does that,
    and it needs no second tokenizer here.
    """
    terms = []
    for raw in query.replace('"', " ").split():
        term = raw.strip("*").strip().lower()
        # One character matches almost every line and tells the reader
        # nothing.
        if len(term) > 1 and term not in _FTS5_OPERATORS:
            terms.append(term)
    return terms


def _body_match_lines(source: str, start_line: int, terms: list[str]) -> list[int]:
    """Give the absolute line numbers in *source* that hold one of *terms*.

    WHY this exists: FTS5 ``snippet()`` gives the matching text but no
    position, and the symbol row gives only the first line of the body.  A
    caller that must cite the statement — one ``case`` label, one call in a
    long function — was left with the line of the enclosing definition,
    which for a large function is wrong by hundreds of lines.  It then had
    to leave fw-context for a text search to find the real line, and that
    text search reads unfiltered source.

    *source* is the full stored body and not the truncated copy that goes
    into the result, thus a match after the truncation point still gets a
    line number.
    """
    if not terms or not source:
        return []
    hits: list[int] = []
    for offset, text in enumerate(source.splitlines()):
        lowered = text.lower()
        if any(term in lowered for term in terms):
            hits.append(start_line + offset)
            if len(hits) >= _MATCH_LINES_CAP:
                break
    return hits


def search_bodies(
    query: Annotated[str, Field(description="FTS5 search terms for the body of a definition. 1-3 words. E.g. 'attach', 'callback', 'rise'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    kind: Annotated[str | None, Field(description="Optional kind filter: function, method, class, etc.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
    project_only: Annotated[bool, Field(description="Exclude vendor SDK code. When True, only application code. Default False.")] = False,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find patterns in the TEXT OF A DEFINITION — the code inside its extent.

    Searches the stored text of every definition (``is_definition=1``), and
    a definition is not only a callable.  Measured on one project of 60,877
    symbols, the text covers:

    - Callables — ``function``, ``method``, ``constructor``, ``destructor``.
      Call patterns (``.attach(``, ``.rise(``, ``callback(&``), ISR
      registration, one ``case`` label of a long ``switch``.
    - Types — ``class``, ``struct``, ``union``, ``enum``, ``namespace``.
      An enum constant, a bit field, a member declaration such as
      ``InterruptIn _pin;`` — all inside the body of the type that holds
      them.
    - Definitions of data — ``varglobal``, ``varlocal``, ``typedef``.  A
      table with a multi-line initializer is found by its content.

    A match on a type reports the type as the result, thus a query for one
    enum constant answers with the enum, and ``_match_lines`` gives the line
    of the constant itself.

    **When to use ``search_bodies`` and when ``search_code``:**

    - ``search_bodies`` — patterns in the code (what the code DOES or
      DECLARES): ``batt test``, ``attach``, ``BATT_TEST``.
    - ``search_code`` — symbols by NAME (what the code IS): ``modem init``,
      ``interrupt handler``.

    **Limitation — the extent of a definition is the boundary.**  Text that
    belongs to no definition is out of reach:

    - ``#include``, ``#define``, ``#ifdef`` — preprocessor directives.
      ``search_code`` covers a macro name and value.  ``search_content``
      covers the directive as text.
    - ``extern "C"`` — a linkage specifier is no symbol.
    - A comment or a declaration at file scope, outside every definition.

    For those, use ``search_content``, which indexes the full file text.

    Set ``project_only=True`` for a question about YOUR code (``"where do we
    register interrupt handlers?"``).  Leave it ``False`` (default) when the
    vendor SDK code — the framework or OS code that your team did not write
    — is also relevant.

    Results include ``_match_snippet`` — a highlighted excerpt that shows
    each match in context (e.g. ``_timeout.<b>attach</b>(callback(...))``) —
    and ``_match_lines``, the line numbers of the matches inside the
    definition.  ``line`` is where the definition starts, which for a large
    function is far from the match.  Cite from ``_match_lines`` instead.
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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line
        (first line of the definition), is_definition, signature,
        _match_snippet (excerpt around the match), source (the text of the
        definition).

        Also, when they carry an answer:

        * ``_match_lines`` (list[int]) — absolute line numbers of the
          matches, up to 20.  Computed from the full text, thus a match
          after the cut below still has a number.  Use these to cite
          ``file:line``, and not the ``line`` of the definition.
        * ``_source_truncated`` (True) — ``source`` is cut.  A callable
          keeps 2000 characters, any other kind 500, because the body of a
          type is mostly members that the match has nothing to do with.
          ``get_source`` gives the whole text.

        ``source`` here is bare text with no line-number prefix.  Only
        ``get_source`` numbers its lines.

        No match gives ``[]``.  A dict with ``error`` means the query
        failed.  A stale index prepends a dict with ``warning`` + ``hint``.
    """
    root = resolve_project_root(project_root)
    # Enforce limit bounds at the function entry point (not inside _do_search)
    # because _do_search may be called multiple times by stale recovery —
    # applying the clamp once here ensures consistency across retries.
    limit = max(0, min(limit, 100))

    # Computed once, outside the query closure: stale recovery can call the
    # closure again, and the terms depend only on the operator's query.
    terms = _body_query_terms(query)

    expanded = _expand_query(query, for_body_search=True)
    # Body search passes the query through unmodified — repair backslashes
    # (never valid FTS5) so `"extern \"C\""`-style queries don't raise.
    # Operators sometimes paste escaped strings from other tools; this
    # sanitization prevents FTS5 syntax errors from propagated escapes.
    if "\\" in expanded:
        from ...indexer.db._symbols import _sanitize_fts5_syntax

        expanded = _sanitize_fts5_syntax(expanded)

    def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        """Execute FTS5 search over the stored text of definitions.

        Searches the ``symbols_fts`` FTS5 index, which covers the text of
        every definition (``is_definition=1 AND source != ''``) — a
        callable, a type, or a definition of data.  Each result includes a
        ``_match_snippet`` from SQLite's ``snippet()`` function for
        contextual highlighting with ``<b>`` tags around matches, and
        ``_match_lines`` for the position that ``snippet()`` drops.

        **Sorting strategy — project-first:** When ``project_only=False``
        (default), results are sorted with project code first, then vendor
        code. The operator sees their own code before framework internals.
        Sorting happens in Python after the DB query because SQLite FTS5
        ``rank`` already provides relevance ordering within each group.

        **Limit enforcement — over-fetch then slice:** The DB query fetches
        ``limit * 3`` rows (3× multiplier for non-project_only, just
        ``limit`` for project_only). The 3× compensates for vendor-code
        filtering — most matches may be in vendor code, so we over-fetch,
        sort project-first, then slice to ``limit``. This avoids returning
        fewer than ``limit`` results when the majority of FTS5 matches live
        in vendor directories.

        **Why DB-level limit matters:** SQLite FTS5's ``rank`` column is a
        bm25 score computed during the MATCH — ordering by rank and limiting
        at the DB level ensures we get the most relevant matches before any
        Python-level filtering. Doing the same filtering in Python would
        require fetching all matches, which could be tens of thousands of
        rows in a large codebase.

        **FTS5 syntax errors:** Invalid FTS5 queries (e.g. unescaped special
        characters) raise ``sqlite3.OperationalError`` with error code 1.
        These are caught and logged — we return empty results rather than
        propagating the error to the operator, because FTS5 syntax errors
        are typically caused by operator input and are not operational
        failures.

        Args:
            c: Open read-only SQLite connection (provided by stale recovery).
            config_hash: Active build configuration hash for filtering.

        Returns:
            List of result dicts with name, qualified_name, kind, file,
            line, is_definition, signature, _match_snippet, and optionally
            source (truncated at 2000 characters).
        """
        kind_filter = ""
        project_filter = ""
        params: list = [expanded, config_hash]
        if kind:
            kind_filter = "AND s.kind = ?"
            params.append(kind)
        if project_only:
            project_filter = "AND s.is_project = 1"
        params.append(limit * 3 if not project_only else limit)

        try:
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
        except sqlite3.OperationalError as e:
            if getattr(e, "sqlite_errorcode", 0) == 1 and "fts5" in str(e).lower():
                log.warning("search_bodies: FTS5 syntax error (%s) — returning empty", e)
                rows = []
            else:
                raise

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
                cap = _SOURCE_CAP_CALLABLE if r["kind"] in _CALLABLE_KINDS else _SOURCE_CAP_TYPE
                if len(source) > cap:
                    d["source"] = source[:cap]
                    # Say that the text is cut.  Without this the caller
                    # reads a partial body as the whole one, and nothing in
                    # the result shows that `get_source` holds more.
                    d["_source_truncated"] = True
                else:
                    d["source"] = source
                # The lines come from the full body, thus a match after the
                # cut above still gets a number.
                if match_lines := _body_match_lines(source, r["line"], terms):
                    d["_match_lines"] = match_lines
            results.append(d)

        if project_only:
            final = results
        else:
            # Project-first sorting: group results by is_project, project
            # code first. The 3× over-fetch compensates for vendor matches
            # being filtered out. After sorting, slice to the operator's
            # requested limit. ``_is_project`` is a temporary sort key —
            # it is removed from each dict before returning.
            project_results = [r for r in results if r["_is_project"]]
            vendor_results = [r for r in results if not r["_is_project"]]
            final = project_results + vendor_results
        for r in final:
            del r["_is_project"]
        return final[:limit]

    return _with_search_context(root, "search_bodies", _do_search, variant or "", image or "")



def search_content(
    query: Annotated[str, Field(description="FTS5 search terms for full file content. 1-3 words. E.g. 'InterruptIn', 'extern C'. Bare multi-word = OR-joined.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
    project_only: Annotated[bool, Field(description="Exclude vendor SDK code. When True, only application code. Default False.")] = False,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find patterns in FULL file content — the whole file, not only the
    text that belongs to a definition.

    Searches **ifdef-filtered** file text — only code that actually compiles
    for the current build configuration.  Inactive ``#ifdef`` branches are
    replaced with blank lines (preserving original line numbers).

    Covers the text that belongs to no definition, which is what
    ``search_bodies`` cannot see: ``#include``, ``#define``, ``#ifdef``,
    ``extern "C"``, and a comment or declaration at file scope.  It covers
    the text of definitions too, but prefer ``search_bodies`` there — it
    reports the symbol that holds the match and the line of the match
    itself.  To find a symbol by NAME (``modem init``, ``interrupt
    handler``), use ``search_code``.

    Results are file-level — one entry per matching file.
    ``project_only=True`` filters to ``is_project = 1`` files; the default
    False includes the vendor SDK files.

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
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: file, language, mtime,
        _match_snippet (highlighted excerpt around the match).

        No match gives ``[]``.  A dict with ``error`` means the query
        failed.  A stale index prepends a dict with ``warning`` + ``hint``.
    """
    root = resolve_project_root(project_root)
    # Enforce limit bounds at the function entry point (not inside _do_search)
    # — same rationale as search_bodies: consistent bound across stale-recovery retries.
    limit = max(0, min(limit, 100))
    expanded = _expand_query(query)

    def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        """Execute FTS5 search over complete ifdef-filtered file content.

        Two code paths exist because not all indexes have the ``files_fts``
        FTS5 virtual table (older indexes created before file-content
        indexing was added):

        1. **FTS5 path** (modern indexes): Uses the ``files_fts`` FTS5 table
           with SQLite's ``snippet()`` for highlighted match excerpts. This
           is faster (FTS5 full-text index) and provides contextual snippets
           around each match showing where the pattern occurs.

        2. **LIKE fallback** (legacy indexes): When ``files_fts`` does not
           exist, or when an FTS5 syntax error occurs on the FTS5 query,
           falls back to SQL ``LIKE`` queries against ``files.content``.
           This path is slower for large codebases (sequential scan) and
           cannot provide contextual snippets — the result shows the first
           200 characters of the file. Results carry ``_fallback: "like"``
           so the LLM knows they came from the slower path.

        The fallback is automatic and transparent — the operator sees the
        same result format regardless of which path was taken. Running
        ``fw-context index`` upgrades a legacy index to include
        ``files_fts``, enabling the faster FTS5 path on subsequent queries.

        **Limit enforcement:** The DB query fetches ``limit * 3`` rows. The
        3× multiplier compensates for post-filtering (removing duplicate
        files, applying vendor-code sorting if needed). After Python-level
        processing, results are sliced to ``limit``.

        Args:
            c: Open read-only SQLite connection (provided by stale recovery).
            config_hash: Active build configuration hash for filtering.

        Returns:
            List of result dicts with file, language, mtime, _match_snippet,
            and optionally _fallback.
        """
        project_filter = ""
        if project_only:
            project_filter = "AND f.is_project = 1"

        # Check whether the files_fts virtual table exists.
        # Legacy indexes created before file-content FTS5 indexing was added
        # will not have this table — we fall back to LIKE search in that case.
        table_row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files_fts'"
        ).fetchone()

        if table_row is not None:
            try:
                # FTS5 path: uses snippet() for contextual highlighting.
                # Fetches limit * 3 rows — 3× compensates for vendor-code
                # files that may crowd out project-code matches.
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
            except sqlite3.OperationalError as e:
                # FTS5 syntax error on the query — fall back to LIKE.
                # This handles edge cases where the expanded query produces
                # FTS5-invalid syntax (e.g. unbalanced quotes, stray operators).
                if getattr(e, "sqlite_errorcode", 0) == 1 and "fts5" in str(e).lower():
                    table_row = None
                    log.warning(
                        "search_content: FTS5 syntax error (%s) — falling back to LIKE",
                        e,
                    )
                else:
                    raise
        # When files_fts is missing or FTS5 errored, use LIKE against files.content.
        # Each query term is individually escaped and wrapped in %wildcards%.
        # Results carry _fallback: "like" so the LLM knows the snippet is
        # a raw prefix (first 200 chars of the file), not a contextual match.
        if table_row is None:
            # No files_fts table — fall back to LIKE search on files.content.
            # Each query term is individually escaped (%, _, \) and wrapped
            # in %wildcards% for a substring match. This is slower than FTS5
            # (sequential scan) but works on legacy indexes.
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

    return _with_search_context(root, "search_content", _do_search, variant or "", image or "")
