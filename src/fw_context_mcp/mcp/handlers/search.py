"""Search MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import Field

from ...config import derive_project_id
from ...config import load as load_config
from ...indexer.db import get_active_config, search_symbols
from ...llm.ollama import call_ollama_embed, check_setup
from ...utils import abs_path, resolve_project_root
from ..shared.context import _db_path, _is_stale, _open_db_safe
from ..shared.fallback import _fallback_to_search_code, _fallback_to_search_code_inner
from ..shared.stale import _with_stale_recovery

log = logging.getLogger(__name__)

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
    """Look up a symbol by name — exact or prefix matching.

    Returns all declarations and definitions matching the name across the
    entire indexed codebase. Prefer this over search_code when you know the
    exact symbol name or a prefix of it (``uart_`` finds all UART symbols).
    Use search_code for keyword/concept search when you don't know the name.

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
        with the integer value. May also include ``template_usr``,
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
    kind: Annotated[str | None, Field(description="Optional kind filter: function, method, class, struct, enum, typedef, variable, field, namespace.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
) -> list[dict]:
    """Full-text search over indexed C/C++ symbols (functions, classes, methods, enums, etc.).

    Read-only. No side effects. Use when looking for symbols by topic or keyword
    rather than exact name. Prefer ``lookup_symbol`` when you already know the
    symbol name.

    **FTS5 syntax:**
    - ``init*`` matches init, init_uart, initialize (trailing wildcard)
    - ``"spi init"`` matches the exact phrase "spi init"
    - Do NOT use underscore in queries — ``modem_init`` is split into
      ``modem AND init``. Use ``modem init`` instead.

    **Progressive relaxation:** when the initial FTS5 search returns nothing,
    the tool automatically broadens the search in up to five steps:

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

    Results from fallback steps carry ``_fallback`` indicating which method
    succeeded (``"fts5"``, ``"name_tokens_like"``, ``"docstring_like"``,
    ``"individual_terms"``).

    **Kind filter values:** ``function``, ``method``, ``constructor``,
    ``destructor``, ``class``, ``struct``, ``enum``, ``enum_constant``,
    ``typedef``, ``variable``, ``field``, ``namespace``.

    Each result may include ``summary``, ``inputs``, ``outputs``
    when LLM analysis has been generated (``fw-context index --analyze``).
    These provide structured descriptions: what the symbol does, what
    parameters/data it receives, and what it returns/produces.

    Args:
        query: Search term(s) with FTS5 syntax. Keep queries short — 1–3 words.
        project_root: Project root directory. Auto-detected from CWD if omitted.
        kind: Optional filter to return only symbols of this kind.
        limit: Maximum number of results (default 20, max 100).

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
            rows = search_symbols(
                c, query, config_hash, limit=limit, kind=kind,
                exclude_variables=False,
            )
            # Progressive fallback cascade when FTS5 returns nothing.
            # Each step broadens the search until we find results or exhaust options.
            method = "fts5+kind"  # track which step succeeded for _fallback marker
            if not rows and kind:
                # Step 2: drop kind filter — users often guess the wrong kind
                rows = search_symbols(
                    c, query, config_hash, limit=limit, kind=None,
                    exclude_variables=False,
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
                        kind=None, exclude_variables=True,
                    )
                    for r in term_results:
                        if r["usr"] not in seen_usr:
                            seen_usr.add(r["usr"])
                            ind_rows.append(r)
                rows = ind_rows[:limit]
                if rows:
                    method = "individual_terms"

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
    """Natural-language search: Ollama generates FTS5 keywords, then searches the index.

    Read-only. No side effects. Slow (10-30 s) — delegates to the full
    ``SMART_SEARCH`` pipeline (8 phases: translate → rough_search → llm_query →
    fts5_search → refine → embedding → deduplicate → format).

    Multi-phase approach:
    1) Translate non-English queries
    2) Rough search to gather sample symbols for naming conventions
    3) Ollama sees those samples + query and generates FTS5 terms
    4) FTS5 search with generated terms
    5) Refine: Ollama checks results and course-corrects query terms
    6) Semantic embedding search (cosine similarity re-rank)
    7) Deduplicate, score, and format results

    **When to prefer over search_code:** When you don't know the exact keywords
    and want to describe what you're looking for ("how does the modem connect?",
    "handle BLE pairing failure").

    **Fallback:** When Ollama is unavailable, falls back to direct FTS5 search
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
    from fw_context_mcp.search import SMART_SEARCH
    from fw_context_mcp.search.context import PipelineContext
    from fw_context_mcp.search.pipeline import PipelineRunner

    try:
        ctx = PipelineContext.create(query=query, project_root=project_root, limit=limit)
    except ValueError as e:
        return [{"error": str(e)}]

    runner = PipelineRunner(SMART_SEARCH)
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
    """Read-only. Semantic search using pre-computed symbol embeddings.

    Finds symbols conceptually related to a natural-language query, even when
    the query words don't appear literally in the code.  Uses cosine similarity
    over 1024-dimensional embeddings generated during ``fw-context index``.

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
    - ``0.55`` — balanced (default, ~1000 results)
    - ``0.60`` — precise: ~175 avg, high precision
    - ``0.65`` — strict: few results, may miss relevant symbols

    **Source-aware ranking:** Project code (``src/``) boosted 1.2×,
    library code (``lib/``) 1.1×, vendored SDK (``mbed-os/``) 0.85×.

    **Requires Ollama** with an embedding model (``mxbai-embed-large``).
    Falls back to ``search_code`` with a warning if Ollama is unavailable.

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
                warning="Ollama is disabled in config. "
                        "Enable it with `[llm] enabled = true` to use semantic search.",
            )

        try:
            setup = check_setup(cfg.llm)
        except Exception:
            setup = {"ollama_running": False}

        if not setup.get("ollama_running"):
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning="Ollama is not running. Start it to use semantic search.",
            )

        # Generate query embedding
        try:
            query_embs = await asyncio.to_thread(
                call_ollama_embed, [query], cfg.llm
            )
            query_vec = query_embs[0]
        except Exception as e:
            log.warning("semantic_search: Ollama embed failed: %s", e)
            return _fallback_to_search_code(
                root, db_path, query, limit,
                warning=f"Ollama embedding failed: {e}. "
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

            # Source-aware boost: project code > libraries > vendored SDK
            def _source_boost(file_path: str) -> float:
                if file_path.startswith("src/"):
                    return 1.2
                elif file_path.startswith("lib/"):
                    return 1.1
                else:
                    return 0.85

            # Compute cosine similarity + source boost for each embedding
            BATCH = 1000
            norm_a = math.sqrt(sum(x * x for x in query_vec))
            keep = limit * 10
            top_candidates: list[tuple[float, float, int]] = []

            for offset in range(0, total, BATCH):
                rows = c.execute(
                    """SELECT e.symbol_id, e.embedding, s.file_path
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
                        boost = _source_boost(r["file_path"] or "")
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
