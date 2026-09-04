"""Shared fallback search strategies — canonical implementations.

Why shared fallbacks?
    Two layers need the same fallback logic:

    1. The MCP handler layer (``_search_fallbacks.py``) — called directly
       by ``search_code`` when the primary FTS5 search returns zero results.
    2. The search pipeline layer (``search_fallbacks.py``) — phases like
       ``NameTokensFallbackPhase`` that run inside the pipeline when
       ``fts5_results`` is empty.

    Duplicating the fallback implementations would risk divergence — a
    fix in one layer would not apply to the other.  This module provides
    a single canonical implementation for each fallback strategy.

Why an ordered chain of fallbacks?
    Each fallback is progressively broader — from precise (name_tokens LIKE)
    to broad (macros FTS).  The first fallback that returns results stops
    the chain.  This ensures the most precise match is used while still
    covering edge cases where FTS5 failed entirely (e.g. the query uses
    a term not in the FTS5 tokeniser's dictionary).

Used by both the MCP handler layer (:mod:`fw_context_mcp.mcp.handlers._search_fallbacks`)
and the search pipeline layer (:mod:`fw_context_mcp.search.phases.search_fallbacks`).

Each fallback strategy receives a database connection, query, config hash, limit,
optional kind/project_only filters, and a *root* path for producing absolute file
paths.  Returns a ``(list[dict], method_name)`` tuple on success, or ``None`` when
the strategy found no matches or is not applicable to the query shape.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fw_context_mcp.indexer.db import _expand_query, search_symbols
from fw_context_mcp.utils import abs_path, is_db_exception

# ── Row → dict conversion ───────────────────────────────────────────────────


FallbackFunc = Callable[
    [sqlite3.Connection, str, str, int, str | None, bool, Path],
    tuple[list[dict[str, Any]], str] | None,
]


def _symbol_row_to_dict(r: sqlite3.Row, root: Path, **extra) -> dict[str, Any]:
    """Convert a symbol ``sqlite3.Row`` (or plain dict) to a dict for MCP tool output.

    Why accept both Row and dict?
        Phase code produces dicts (via ``dict(r)``); tests pass plain dicts.
        Accepting both avoids an extra conversion step in callers.

    Why conditional fields?
        Fields like ``template_usr``, ``parent_usr``, ``enum_value``, and
        the LLM analysis are absent from many symbols.  Including them as
        empty strings would bloat the output.  Conditional inclusion keeps
        the dict lean.

    Why the LLM analysis is nested?
        ``summary``, ``inputs``, and ``outputs`` are the output of a model,
        not a property of the code.  They go under ``llm_analysis`` so that
        a reader cannot mistake them for ``signature`` or ``docstring``,
        which come from the source.

    Accepts both ``sqlite3.Row`` (from database queries) and plain dicts
    (from in-memory test fixtures).  Includes all standard symbol fields
    plus any ``**extra`` kwargs.  Conditional fields are only added when
    present and non-empty.
    """
    row: dict[str, Any] = dict(r) if not isinstance(r, dict) else r
    d: dict[str, Any] = {
        "name": row.get("name", ""),
        "qualified_name": row.get("qualified_name", ""),
        "kind": row.get("kind", ""),
        "file": abs_path(root, row.get("file_path", "")) if root else row.get("file_path", ""),
        "line": row.get("line", 0),
        "is_definition": bool(row.get("is_definition", False)),
        "signature": row.get("signature") or "",
        "docstring": row.get("docstring") or "",
        "is_template": bool(row.get("is_template", False)),
        "is_virtual": bool(row.get("is_virtual", False)),
        "is_pure_virtual": bool(row.get("is_pure_virtual", False)),
    }
    if row.get("template_usr"):
        d["template_usr"] = row["template_usr"]
    if row.get("parent_usr"):
        d["parent_usr"] = row["parent_usr"]
    if row.get("enum_value") is not None:
        d["enum_value"] = row["enum_value"]
    # LLM analysis goes in a wrapper, and not next to `signature` and
    # `docstring`.  Those two come from the code.  These three come from a
    # model.  Measured on one project, a summary said that an identifier
    # was "possibly related to battery status or level" — a guess from the
    # name of the identifier.  Flat keys put that text among indexed facts
    # with nothing to separate the two, and a reader cited it as code.
    #
    # The name of the wrapper says where the text came from.  The name of
    # the model is not repeated here: there is one model for each index,
    # and `get_active_build().analysis.model` already gives it.
    analysis = {f: row[f] for f in ("summary", "inputs", "outputs") if row.get(f)}
    if analysis:
        d["llm_analysis"] = analysis
    d.update(extra)
    return d


# ── Formatting helper ────────────────────────────────────────────────────────


def _fmt_symbol_rows(rows: list, root: Path, method: str) -> tuple[list[dict[str, Any]], str]:
    """Convert a list of symbol rows to dicts with a ``_fallback`` marker.

    Why omit the marker for fts5+kind?
        FTS5+kind is the primary search path, not a fallback.  Prefixing
        results with ``_fallback: "fts5+kind"`` would mislead consumers
        into treating primary results as degraded.  Only actual fallback
        strategies get the marker.
    """
    extra: dict[str, str] = {"_fallback": method} if method != "fts5+kind" else {}
    result = [_symbol_row_to_dict(r, root, **extra) for r in rows]
    return result, method


# ── Fallback strategies ──────────────────────────────────────────────────────


def _search_code_name_tokens(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, project_only: bool,
    root: Path,
) -> tuple[list[dict[str, Any]], str] | None:
    """Token-based LIKE fallback — matches CamelCase/snake_case token splits.

    Why min_matches = N-1?
        When FTS5 failed on all terms, it's likely one term is misspelled
        or uses a different convention than the code.  Requiring N-1 matches
        (instead of all N) tolerates one bad term while still filtering
        noise — a single shared term is too weak a signal.
    """
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if not terms:
        return None
    min_matches = max(1, len(terms) - 1)
    like_cases: list[str] = []
    like_params: list[str] = []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_cases.append(
            "CASE WHEN s.name_tokens LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
        )
        like_params.append(f"%{escaped}%")
    match_sum = " + ".join(like_cases)
    project_filter = "AND s.is_project = 1" if project_only else ""
    rows = c.execute(
        f"""SELECT * FROM (
            SELECT s.*, ({match_sum}) AS _match_cnt FROM symbols s
            WHERE s.config_hash = ? {project_filter}
        ) sub WHERE sub._match_cnt >= ?
        ORDER BY sub.is_definition DESC, sub._match_cnt DESC, sub.line
        LIMIT ?""",
        (*like_params, config_hash, min_matches, limit),
    ).fetchall()
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, "name_tokens_like")


def _search_code_docstring(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, project_only: bool,
    root: Path,
) -> tuple[list[dict[str, Any]], str] | None:
    """Single-term docstring LIKE fallback — only runs for 1-word queries.

    Why only single-term queries?
        Multi-word LIKE over docstrings is expensive and rarely produces
        better results than individual-term FTS5 — the docstring column
        has no FTS5 index, so each LIKE is a full table scan.  Single-term
        is acceptable (1 scan); multi-term would be O(n²) in practice.

    Why docstring search at all?
        FTS5 indexes symbol names, qualified names, and name_tokens — but
        NOT docstrings.  A concept like "power consumption" may only appear
        in the docstring of ``get_load_power``, not in its name.  LIKE
        catches these cases as a last resort for single-word queries.
    """
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if len(terms) != 1:
        return None
    escaped = terms[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    project_filter = "AND s.is_project = 1" if project_only else ""
    rows = c.execute(
        f"""SELECT s.* FROM symbols s
           WHERE s.config_hash = ? {project_filter} AND s.docstring LIKE ? ESCAPE '\\'
           ORDER BY s.is_definition DESC, s.line
           LIMIT ?""",
        (config_hash, f"%{escaped}%", limit),
    ).fetchall()
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, "docstring_like")


def _search_code_individual_terms(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, project_only: bool,
    root: Path,
) -> tuple[list[dict[str, Any]], str] | None:
    """Individual-term FTS5 fallback — each word searched separately.

    Why separate searches instead of OR?
        FTS5 OR queries internally score matches by term frequency.  When
        one term dominates (e.g. "uart" matches 500 times), the OR query
        may return only "uart" results and bury the second term's matches.
        Searching each term separately and merging ensures both terms
        contribute to the result set.

    Why deduplication by USR?
        The same symbol may match multiple terms — merging without dedup
        would produce duplicates.  USR (Unified Symbol Resolution) is
        unique per symbol across the entire index.
    """
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if len(terms) <= 1:
        return None  # Need at least 2 terms for individual search to make sense
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
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, "individual_terms")


def _search_code_macros_fts(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, project_only: bool,
    root: Path,
) -> tuple[list[dict[str, Any]], str] | None:
    """Macro FTS fallback — searches ``#define`` names and values.

    Why last resort?
        Macros are preprocessor constructs, not symbols.  Users typically
        search for functions and types.  This fallback runs only when all
        symbol-level strategies returned nothing — it catches queries like
        ``#define UART_BAUD`` or ``configMAX_PRIORITIES`` that are macros,
        not functions.

    ``_expand_query()`` adds ``*`` suffix to each term — macros are
    typically short and exact (``UART_BAUD``), not prefix-matchable
    without the wildcard.
    """
    try:
        expanded = _expand_query(query)
        project_filter = "AND f.is_project = 1" if project_only else ""
        m_rows = c.execute(
            f"""SELECT m.*, f.path AS file_path
               FROM macros_fts
               JOIN macros m ON m.id = macros_fts.rowid
               JOIN files f ON f.id = m.file_id
               WHERE macros_fts MATCH ? AND m.config_hash = ? {project_filter}
               ORDER BY rank
               LIMIT ?""",
            (expanded, config_hash, limit),
        ).fetchall()
        if not m_rows:
            return None
        macro_dicts: list[dict[str, Any]] = []
        for r in m_rows:
            extra: dict[str, Any] = {
                "kind": "macro",
                "qualified_name": r["name"],
                "signature": f"#define {r['name']}",
                "is_definition": True,
                "_fallback": "macros_fts",
            }
            if r["value"]:
                extra["_macro_value"] = r["value"]
            if r["expanded_value"]:
                extra["_macro_expanded_value"] = r["expanded_value"]
            macro_dicts.append(_symbol_row_to_dict(r, root, **extra))
        return macro_dicts, "macros_fts"
    except Exception as exc:
        if not is_db_exception(exc):
            raise
        # Database errors during macro fallback are non-critical —
        # the earlier fallbacks already tried and failed, so returning
        # None here just means "no results at all."
        return None


# ── Ordered fallback chain ───────────────────────────────────────────────────

# Order matters: precise first, broad last.  Each subsequent fallback is
# more expensive and less precise than the previous one.  Stopping at the
# first that returns results keeps the search fast for typical queries.
_SEARCH_CODE_FALLBACKS: list[FallbackFunc] = [
    _search_code_name_tokens,       # most precise: token-boundary LIKE
    _search_code_docstring,         # single-word docstring catch-all
    _search_code_individual_terms,  # each word separately
    _search_code_macros_fts,        # last resort: macro names
]
