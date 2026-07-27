"""Search MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated, Any

from pydantic import Field

from ...config import derive_project_id
from ...config import load as load_config
from ...indexer.db import _expand_query, get_active_config, lookup_macro, search_symbols
from ...llm.ollama import call_ollama_embed, check_setup
from ...utils import abs_path, resolve_project_root
from ..shared.context import _db_path, _is_stale, _open_db_safe
from ..shared.fallback import _fallback_to_search_code, _fallback_to_search_code_inner
from ..shared.stale import _with_stale_recovery

log = logging.getLogger(__name__)

# Directories considered "application code" by project_only filters.
# REMOVED: _PROJECT_DIRS, _PROJECT_PATH_FILTER, _PROJECT_PATH_FILTER_FILES
# Replaced by s.is_project = 1 / f.is_project = 1 using the DB column.

LOOKUP_EXACT_SQL = """SELECT s.* FROM symbols s
   WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
   ORDER BY s.is_definition DESC, s.line
   LIMIT ?"""

LOOKUP_PREFIX_SQL = r"""SELECT s.* FROM symbols s
   WHERE s.config_hash=? AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
   ORDER BY s.is_definition DESC, s.line
   LIMIT ?"""

# ── moved from server.py ──
def lookup_symbol(
    name: Annotated[str, Field(description="Symbol name. Exact match if exact=True, prefix LIKE match otherwise. E.g. 'uart_init' or 'uart_'.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")] = None,
    exact: Annotated[bool, Field(description="True = exact name match, False = prefix LIKE match (default).")] = False,
    limit: Annotated[int, Field(description="Maximum results returned (capped at 100, default 50).")] = 50,
) -> list[dict]:
    """Look up a C/C++ symbol by name via libclang index — exact or prefix
    matching. Finds symbols text-based search can miss: build-conditional
    code, template instantiations, macro-expanded names. Prefer this when
    you know the exact symbol name or prefix. Falls back to macro lookup.

    Finds symbols text-based search can miss: build-conditional code, template
    instantiations, macro-expanded names. Macros are extracted via
    ``clang -dM -E`` during indexing so ``#ifdef``-conditional macros
    resolve correctly for the active build config. Prefer this over
    search_code when you know the exact symbol name or a prefix
    (``uart_`` finds all UART symbols). Use search_code for
    keyword/concept search.

    Read-only: yes. May auto-reindex stale files (non-blocking).

    Args:
        name: Symbol name (exact match) or prefix (set exact=False).
            E.g. 'uart_init' finds the exact function; 'uart_' finds
            all symbols starting with 'uart_'.
        project_root: Project directory. Auto-detected if omitted.
        exact: True = exact name match, False = prefix LIKE match (default).
        limit: Maximum results (default 50).

    Returns:
        list[dict]: Symbols with name, qualified_name, kind, file, line,
        signature, docstring, is_definition, is_template, is_virtual,
        is_pure_virtual fields. Enum constants include ``enum_value``
        with the integer value. Macro results include ``kind="macro"``,
        ``value`` (raw definition), and ``expanded_value`` (preprocessor-
        resolved value). May also include ``template_usr``,
        ``parent_usr``, ``summary``, ``inputs``, ``outputs`` when available.
        When no results found, may include ``_did_you_mean`` with suggested
        symbol names. Empty list if not found.
    """
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]

        limit = min(limit, 100)

        def _do_lookup(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            if exact:
                rows = c.execute(
LOOKUP_EXACT_SQL,
                    (config_hash, name, name, limit),
                ).fetchall()
            else:
                esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = c.execute(
LOOKUP_PREFIX_SQL,
                    (config_hash, f"{esc}%", f"{esc}%", limit),
                ).fetchall()

            # Fallback: "Foo::bar" without namespace — extract short name, suffix-filter
            if not rows and "::" in name:
                short_name = name.rsplit("::", 1)[-1]
                if exact:
                    rows = c.execute(
                        """SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, short_name, short_name, limit * 2),
                    ).fetchall()
                else:
                    esc2 = short_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    rows = c.execute(
                        r"""SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, f"{esc2}%", f"{esc2}%", limit * 2),
                    ).fetchall()
                rows = [r for r in rows if r["qualified_name"].endswith(name)][:limit]

            # Did-you-mean? suggestions when nothing matched
            _suggestions: list[str] = []
            if not rows:
                try:
                    from ...search.did_you_mean import suggest as suggest_names
                    _suggestions = suggest_names(c, config_hash, name, limit=5)
                except Exception:
                    pass  # suggestions are best-effort

            # Macro fallback: check the macros table
            if not rows:
                _macro_rows = lookup_macro(c, config_hash, name, exact=exact, limit=limit)
                if _macro_rows:
                    result = [
                        {
                            "name": m["name"],
                            "qualified_name": m["name"],
                            "kind": "macro",
                            "file": abs_path(root, m["file_path"]),
                            "line": m["line"],
                            "value": m["value"],
                            **({"expanded_value": m["expanded_value"]} if m["expanded_value"] else {}),
                        }
                        for m in _macro_rows
                    ]
                    if _suggestions:
                        result.append({"_did_you_mean": _suggestions})
                    return result

            # Auto-fallback: when exact/prefix lookup found nothing but we have
            # did-you-mean suggestions, try the top match so the user doesn't
            # get an empty result — e.g. "uart_init" → nrfx_uarte_init
            if not rows and _suggestions:
                for suggestion in _suggestions[:3]:
                    rows = c.execute(
                        """SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, suggestion, suggestion, limit),
                    ).fetchall()
                    if rows:
                        break

            result = [
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": abs_path(root, r["file_path"]),
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                    "is_template": bool(r["is_template"]),
                    "is_virtual": bool(r["is_virtual"]),
                    "is_pure_virtual": bool(r["is_pure_virtual"]),
                    **({"template_usr": r["template_usr"]} if r["template_usr"] else {}),
                    **({"parent_usr": r["parent_usr"]} if r["parent_usr"] else {}),
                    **({"enum_value": r["enum_value"]} if r["enum_value"] is not None else {}),
                    **({"summary": r["summary"]} if r["summary"] else {}),
                    **({"inputs": r["inputs"]} if r["inputs"] else {}),
                    **({"outputs": r["outputs"]} if r["outputs"] else {}),
                }
                for r in rows
            ]
            if _suggestions:
                result.append({"_did_you_mean": _suggestions})
            return result

        return _with_stale_recovery(root, db_path, _do_lookup)
    except Exception as e:
        log.exception("lookup_symbol failed: %s", e)
        return [{"error": f"lookup_symbol failed: {e}"}]

# ── moved from server.py ──
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
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = min(limit, 100)

        def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            rows: list = search_symbols(
                c, query, config_hash, limit=limit, kind=kind,
                exclude_variables=False, project_only=project_only,
            )
            # Progressive fallback cascade when FTS5 returns nothing.
            # Each step broadens the search until we find results or exhaust options.
            method = "fts5+kind"  # track which step succeeded for _fallback marker
            if not rows and kind:
                # Step 2: drop kind filter — users often guess the wrong kind
                rows = search_symbols(
                    c, query, config_hash, limit=limit, kind=None,
                    exclude_variables=False, project_only=project_only,
                )
                if rows:
                    method = "fts5"

            if not rows:
                # Step 3: name_tokens substring matching.
                # name_tokens is a pre-computed column with CamelCase/snake_case
                # tokens already split to lowercase space-separated words
                # (e.g. ``BuildType`` → ``"build type"``,
                #  ``socket_state_t`` → ``"socket state"``).
                # We require at least N-1 of N query terms to match so that a
                # single unrelated term doesn't kill the result set.
                terms = [t.lower() for t in query.split() if len(t) > 1]
                if terms:
                    min_matches = max(1, len(terms) - 1)
                    like_cases = []
                    for term in terms:
                        esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
                        like_cases.append(
                            f"CASE WHEN s.name_tokens LIKE '%{esc}%' ESCAPE '\\' THEN 1 ELSE 0 END"
                        )
                    match_sum = " + ".join(like_cases)
                    rows = c.execute(
                        f"""SELECT s.*, ({match_sum}) AS _match_cnt FROM symbols s
                           WHERE s.config_hash = ? AND ({match_sum}) >= ?
                           ORDER BY s.is_definition DESC, _match_cnt DESC, s.line
                           LIMIT ?""",
                        (config_hash, min_matches, limit),
                    ).fetchall()
                    if rows:
                        method = "name_tokens_like"

            if not rows and len(terms) == 1:
                # Step 4: single-term last resort — LIKE on docstring (in
                # case FTS5 tokenizer missed something the raw text contains).
                esc = terms[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
                rows = c.execute(
                    f"""SELECT s.* FROM symbols s
                       WHERE s.config_hash = ? AND s.docstring LIKE '%{esc}%' ESCAPE '\\'
                       ORDER BY s.is_definition DESC, s.line
                       LIMIT ?""",
                    (config_hash, limit),
                ).fetchall()
                if rows:
                    method = "docstring_like"

            if not rows and len(terms) > 1:
                # Step 5: fall back to individual FTS5 searches for each term,
                # then merge and deduplicate.
                seen_usr: set[str] = set()
                ind_rows: list = []
                for term in terms:
                    term_results = search_symbols(
                        c, term, config_hash,
                        limit=max(3, limit // len(terms)),
                        kind=None, exclude_variables=True, project_only=project_only,
                    )
                    for r in term_results:
                        if r["usr"] not in seen_usr:
                            seen_usr.add(r["usr"])
                            ind_rows.append(r)
                rows = ind_rows[:limit]
                if rows:
                    method = "individual_terms"

            # Step 6: macro FTS5 fallback — when no symbols matched, also search
            # the macros_fts table for #define macro names and values.
            if not rows:
                try:
                    expanded = _expand_query(query)
                    m_rows = c.execute(
                        """SELECT m.*, f.path AS file_path
                           FROM macros_fts
                           JOIN macros m ON m.id = macros_fts.rowid
                           JOIN files f ON f.id = m.file_id
                           WHERE macros_fts MATCH ? AND m.config_hash = ?
                           ORDER BY rank
                           LIMIT ?""",
                        (expanded, config_hash, limit),
                    ).fetchall()
                    if m_rows:
                        macro_dicts: list[dict[str, Any]] = []
                        for r in m_rows:
                            d: dict[str, Any] = {
                                "name": r["name"],
                                "qualified_name": r["name"],
                                "kind": "macro",
                                "file": abs_path(root, r["file_path"]),
                                "line": r["line"],
                                "is_definition": True,
                                "signature": f"#define {r['name']}",
                                "docstring": "",
                                "is_template": False,
                                "is_virtual": False,
                                "is_pure_virtual": False,
                                "_fallback": "macros_fts",
                            }
                            if r["value"]:
                                d["_macro_value"] = r["value"]
                            if r.get("expanded_value"):
                                d["_macro_expanded_value"] = r["expanded_value"]
                            macro_dicts.append(d)
                        rows.extend(macro_dicts)
                except (sqlite3.OperationalError, Exception):
                    pass  # macros_fts may not exist on older indexes

            fallback_used = (method != "fts5+kind")

            def _fmt(r) -> dict:
                d = {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": abs_path(root, r["file_path"]),
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                    "is_template": bool(r["is_template"]),
                    "is_virtual": bool(r["is_virtual"]),
                    "is_pure_virtual": bool(r["is_pure_virtual"]),
                }
                if r["template_usr"]:
                    d["template_usr"] = r["template_usr"]
                if r["parent_usr"]:
                    d["parent_usr"] = r["parent_usr"]
                if r["enum_value"] is not None:
                    d["enum_value"] = r["enum_value"]
                if "summary" in r.keys() and r["summary"]:
                    d["summary"] = r["summary"]
                if "inputs" in r.keys() and r["inputs"]:
                    d["inputs"] = r["inputs"]
                if "outputs" in r.keys() and r["outputs"]:
                    d["outputs"] = r["outputs"]
                if fallback_used:
                    d["_fallback"] = method
                return d

            return [_fmt(r) for r in rows]

        return _with_stale_recovery(root, db_path, _do_search)
    except Exception as e:
        log.exception("search_code failed: %s", e)
        return [{"error": f"search_code failed: {e}"}]

# ── moved from server.py ──
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
    fts5_search → refine → embedding → rrf_fusion → deduplicate →
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
    ctx = await runner.run(ctx)

    # Add staleness warning if applicable
    results = list(ctx.formatted_results)
    if ctx.ollama_warning is None:
        conn, err = _open_db_safe(ctx.db_path)
        if err:
            return [err]
        assert conn is not None
        try:
            with conn:
                cfg_data = get_active_config(conn, derive_project_id(ctx.project_root))
                if cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"]):
                    results.append({
                        "warning": "Index may be stale — compile_commands.json changed since last index.",
                        "hint": "Call reindex_file() on modified files or run 'fw-context index' to update.",
                    })
        finally:
            conn.close()
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
    import asyncio
    import math
    import struct

    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = min(limit, 100)
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
        except Exception:
            setup = {"ollama_running": False}

        if not setup.get("ollama_running"):
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning="LLM is not running. Start it to use semantic search.",
            )

        # Generate query embedding
        try:
            query_embs = await asyncio.to_thread(
                call_ollama_embed, [query], cfg.llm, query=True
            )
            query_vec = query_embs[0]
        except Exception as e:
            log.warning("semantic_search: Ollama embed failed: %s", e)
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning=f"LLM embedding failed: {e}. "
                        "Showing lexical search results instead.",
            )

        # Load embeddings and run cosine search
        def _do_semantic(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            # Count embeddings first so we can paginate
            total = c.execute(
                """SELECT COUNT(*)
                   FROM embeddings e
                   JOIN symbols s ON s.id = e.symbol_id
                   WHERE s.config_hash = ? AND s.is_definition = 1""",
                (config_hash,),
            ).fetchone()[0]

            if total == 0:
                return _fallback_to_search_code_inner(
                    c, root, query, config_hash, limit,
                    warning="No embeddings found in the index. "
                            "Run `fw-context index --embeddings` to generate them.",
                )

            # Source-aware boost: project code > vendored SDK
            def _source_boost(row: dict) -> float:
                return 1.2 if row.get("is_project") == 1 else 0.85

            # Compute cosine similarity + source boost for each embedding
            BATCH = 1000
            norm_a = math.sqrt(sum(x * x for x in query_vec))
            keep = limit * 10
            top_candidates: list[tuple[float, float, int]] = []

            for offset in range(0, total, BATCH):
                rows = c.execute(
                    """SELECT e.symbol_id, e.embedding, s.file_path, s.is_project
                       FROM embeddings e
                       JOIN symbols s ON s.id = e.symbol_id
                       WHERE s.config_hash = ? AND s.is_definition = 1
                       ORDER BY e.symbol_id
                       LIMIT ? OFFSET ?""",
                    (config_hash, BATCH, offset),
                ).fetchall()

                for r in rows:
                    try:
                        vec = struct.unpack(f'{len(query_vec)}f', r["embedding"])
                    except Exception:
                        continue
                    dot = sum(x * y for x, y in zip(query_vec, vec, strict=True))
                    norm_b = math.sqrt(sum(x * x for x in vec))
                    raw_sim = dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
                    if raw_sim > threshold:
                        boost = _source_boost(r)
                        top_candidates.append((raw_sim * boost, raw_sim, r["symbol_id"]))

                if len(top_candidates) > keep:
                    top_candidates.sort(key=lambda x: -x[0])
                    top_candidates = top_candidates[:keep]

            if not top_candidates:
                return [{
                    "warning": f"No symbols matched with similarity > {threshold}. "
                               "Try lowering the threshold or rephrasing the query.",
                    "hint": "Use search_code for lexical/keyword search.",
                }]

            top_candidates.sort(key=lambda x: -x[0])
            top = top_candidates[:limit]

            if not top:
                return [{
                    "warning": f"No symbols matched with similarity > {threshold}. "
                               "Try lowering the threshold or rephrasing the query.",
                    "hint": "Use search_code for lexical/keyword search.",
                }]

            # Resolve symbol details
            sym_ids = [r[2] for r in top]  # r[2] is symbol_id
            placeholders = ",".join("?" * len(sym_ids))
            sym_rows = c.execute(
                f"""SELECT * FROM symbols
                    WHERE config_hash = ? AND id IN ({placeholders})
                    ORDER BY CASE id {' '.join(f'WHEN {i} THEN {j}' for j, i in enumerate(sym_ids))} END""",
                (config_hash, *sym_ids),
            ).fetchall()

            sym_map = {r["id"]: r for r in sym_rows}
            # Map symbol_id → raw similarity (r[1] from scored tuple)
            sim_map = {r[2]: r[1] for r in top}  # symbol_id → raw_sim

            results: list[dict] = []
            for sid in sym_ids:
                sr = sym_map.get(sid)
                if sr is None:
                    continue
                d = {
                    "name": sr["name"],
                    "qualified_name": sr["qualified_name"],
                    "kind": sr["kind"],
                    "file": abs_path(root, sr["file_path"]),
                    "line": sr["line"],
                    "is_definition": bool(sr["is_definition"]),
                    "signature": sr["signature"],
                    "docstring": sr["docstring"],
                    "_similarity": round(sim_map[sid], 4),
                    "_method": "embedding",
                }
                if sr["enum_value"] is not None:
                    d["enum_value"] = sr["enum_value"]
                if sr["summary"]:
                    d["summary"] = sr["summary"]
                if sr["inputs"]:
                    d["inputs"] = sr["inputs"]
                if sr["outputs"]:
                    d["outputs"] = sr["outputs"]
                results.append(d)

            return results

        return _with_stale_recovery(root, db_path, _do_semantic)

    except Exception as e:
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
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = min(limit, 100)
        expanded = _expand_query(query)

        def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            kind_filter = ""
            project_filter = ""
            params: list = [expanded, config_hash]
            if kind:
                kind_filter = "AND s.kind = ?"
                params.append(kind)
            if project_only:
                project_filter = "AND s.is_project = 1"
            params.append(limit * 3)  # fetch more for post-filter boosting

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

            # Prioritize project code first, then vendor — both groups retain FTS5 rank order
            if project_only:
                final = results
            else:
                project_results = [r for r in results if r["_is_project"]]
                vendor_results = [r for r in results if not r["_is_project"]]
                final = project_results + vendor_results
            for r in final:
                del r["_is_project"]
            return final[:limit]

        return _with_stale_recovery(root, db_path, _do_search)
    except Exception as e:
        log.exception("search_bodies failed: %s", e)
        return [{"error": f"search_bodies failed: {e}"}]


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
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = min(limit, 100)
        expanded = _expand_query(query)

        def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            project_filter = ""
            if project_only:
                project_filter = "AND f.is_project = 1"

            # Check if files_fts table exists — may be missing on indexes
            # that predate the files_fts feature.  Fall back to LIKE on
            # files.content with a _fallback marker so the caller knows
            # results are approximate (no snippet highlighting).
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
                # Fallback: LIKE search on files.content — no FTS5 index.
                # Split the raw query into individual terms and AND them via
                # multiple LIKE conditions.
                terms = [t.strip() for t in query.replace("_", " ").split() if t.strip()]
                if not terms:
                    return []
                like_clauses = " AND ".join(["f.content LIKE ?" for _ in terms])
                like_params = [f"%{t}%" for t in terms]
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
                    d["_fallback"] = "like"  # no FTS5 — approximate results
                results.append(d)
            return results[:limit]

        return _with_stale_recovery(root, db_path, _do_search)
    except Exception as e:
        log.exception("search_content failed: %s", e)
        return [{"error": f"search_content failed: {e}"}]
