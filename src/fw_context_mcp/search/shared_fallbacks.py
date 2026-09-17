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
from fw_context_mcp.indexer.db._symbols import count_symbols
from fw_context_mcp.utils import abs_path, is_db_exception, macro_signature

# ── Row → dict conversion ───────────────────────────────────────────────────


# The last argument is the offset of the page.  It is positional, because
# one table drives every strategy with the same call; it has a default,
# because the pipeline layer does not page and calls without it.
FallbackFunc = Callable[
    [sqlite3.Connection, str, str, int, str | None, bool, Path, int],
    tuple[list[dict[str, Any]], str] | None,
]

# The counter of one strategy.  It takes what the strategy takes, less the
# page bound and the root, thus one table can drive a strategy and its
# counter with the same arguments.
CountFunc = Callable[[sqlite3.Connection, str, str, str | None, bool], int]


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
    # was "possibly related to sensor status or level" — a guess from the
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

# The kinds a topic search drops.  A local variable is indexed under the
# qualified name of the function that holds it, thus `sensor` reaches the
# `ret` of `read_sensor_value` although its own name says nothing.
# `varglobal` is NOT here — a global name is a real search target.
_LOW_SIGNAL_KINDS = ("variable", "varlocal")


def _variable_filter(kind: str | None) -> str:
    """SQL that drops the low-signal kinds, unless the caller asked for one.

    The relaxation steps below do not apply the ``kind`` of the caller to
    their rows, thus the filter cannot key on "a kind was given": with
    ``kind="function"`` that would let a local back in through the very step
    that ignores the kind.  Only an explicit ask for a variable kind turns
    the filter off.
    """
    if kind in _LOW_SIGNAL_KINDS:
        return ""
    kinds = ", ".join(f"'{k}'" for k in _LOW_SIGNAL_KINDS)
    return f"AND s.kind NOT IN ({kinds})"


# How many words of a query the LIKE strategies read.  The name-token
# search builds one ``CASE WHEN … END`` per term and adds them together,
# thus the term count is the DEPTH of the SQL expression — and SQLite
# refuses an expression deeper than 1000 with "Expression tree is too
# large".  A 1000-word query reached that limit and the tool answered
# with an error about SQLite instead of an answer about the code.
#
# The cap costs no answer.  The strategy demands N-1 of N terms, thus a
# query of hundreds of words matches nothing either way, and search_code
# documents a query of 1-3 words.  The individual-term strategy runs one
# FTS5 query PER term, so the same cap keeps a long query from turning
# into hundreds of round trips.
_MAX_QUERY_TERMS = 32


def _name_token_terms(query: str) -> list[str]:
    """Give the terms that the two LIKE strategies below search for."""
    return [t.lower() for t in query.split() if len(t) > 1][:_MAX_QUERY_TERMS]


def _name_tokens_sql(terms: list[str], kind: str | None, project_only: bool) -> tuple[str, list]:
    """Build the WHERE text and its parameters for the name-token search.

    The count and the page run on this one text, thus a ``total`` can
    never describe another answer than the rows do.
    """
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
    inner = f"""SELECT * FROM (
            SELECT s.*, ({match_sum}) AS _match_cnt FROM symbols s
            WHERE s.config_hash = ? {project_filter} {_variable_filter(kind)}
        ) sub WHERE sub._match_cnt >= ?"""
    return inner, like_params


def count_name_tokens(
    c: sqlite3.Connection, query: str, config_hash: str,
    kind: str | None = None, project_only: bool = False,
) -> int:
    """Count the symbols that ``_search_code_name_tokens`` answers with."""
    terms = _name_token_terms(query)
    if not terms:
        return 0
    inner, like_params = _name_tokens_sql(terms, kind, project_only)
    return c.execute(
        f"SELECT COUNT(*) FROM ({inner})",
        (*like_params, config_hash, max(1, len(terms) - 1)),
    ).fetchone()[0]


def _search_code_name_tokens(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, kind: str | None, project_only: bool,
    root: Path, offset: int = 0,
) -> tuple[list[dict[str, Any]], str] | None:
    """Token-based LIKE fallback — matches CamelCase/snake_case token splits.

    Why min_matches = N-1?
        When FTS5 failed on all terms, it's likely one term is misspelled
        or uses a different convention than the code.  Requiring N-1 matches
        (instead of all N) tolerates one bad term while still filtering
        noise — a single shared term is too weak a signal.

    Why local variables drop out:
        ``name_tokens`` holds the tokens of the qualified name, thus a local
        carries the tokens of the function around it and matches every topic
        query aimed at that function.  The filter stays on for every kind
        but the variable kinds themselves — this step does not apply the
        ``kind`` of the caller to its rows, thus keying the filter on "a
        kind was given" would let a local back in through
        ``kind="function"``.

    Why the order ends at ``sub.usr``:
        The definition flag, the match count and the line all tie, and
        SQLite may then return tied rows in any order.  An OFFSET over such
        an order shows one row twice and hides another.  ``usr`` is unique
        within one build.
    """
    terms = _name_token_terms(query)
    if not terms:
        return None
    inner, like_params = _name_tokens_sql(terms, kind, project_only)
    rows = c.execute(
        f"""{inner}
        ORDER BY sub.is_definition DESC, sub._match_cnt DESC, sub.line, sub.usr
        LIMIT ? OFFSET ?""",
        (*like_params, config_hash, max(1, len(terms) - 1), limit, max(0, offset)),
    ).fetchall()
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, "name_tokens_like")


def _docstring_sql(
    query: str, config_hash: str, kind: str | None, project_only: bool,
) -> tuple[str, tuple] | None:
    """Build the FROM/WHERE text of the docstring search and its parameters.

    Gives None when the strategy does not apply, which is every query that
    does not hold exactly one term.  The count and the page run on this one
    text — see ``_name_tokens_sql``.
    """
    terms = _name_token_terms(query)
    if len(terms) != 1:
        return None
    escaped = terms[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    project_filter = "AND s.is_project = 1" if project_only else ""
    # A local variable carries no docstring of its own worth reading; when
    # it matches, the text belongs to the function around it.  Kept when the
    # caller asked for a kind — see _search_code_name_tokens.
    where = f"""FROM symbols s
           WHERE s.config_hash = ? {project_filter} {_variable_filter(kind)}
             AND s.docstring LIKE ? ESCAPE '\\'"""
    return where, (config_hash, f"%{escaped}%")


def count_docstring(
    c: sqlite3.Connection, query: str, config_hash: str,
    kind: str | None = None, project_only: bool = False,
) -> int:
    """Count the symbols that ``_search_code_docstring`` answers with."""
    built = _docstring_sql(query, config_hash, kind, project_only)
    if built is None:
        return 0
    where, params = built
    return c.execute(f"SELECT COUNT(*) {where}", params).fetchone()[0]


def _search_code_docstring(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, kind: str | None, project_only: bool,
    root: Path, offset: int = 0,
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

    Why the order ends at ``s.usr``:
        The definition flag and the line tie — line 1 of two headers — and
        SQLite may then return tied rows in any order.  An OFFSET over such
        an order shows one row twice and hides another.
    """
    built = _docstring_sql(query, config_hash, kind, project_only)
    if built is None:
        return None
    where, params = built
    rows = c.execute(
        f"""SELECT s.* {where}
           ORDER BY s.is_definition DESC, s.line, s.usr
           LIMIT ? OFFSET ?""",
        (*params, limit, max(0, offset)),
    ).fetchall()
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, "docstring_like")


def count_individual_terms(
    c: sqlite3.Connection, query: str, config_hash: str,
    _kind: str | None = None, project_only: bool = False,
) -> int:
    """Count the symbols that ``_search_code_individual_terms`` reaches.

    The strategy merges one search per term and drops a symbol it has
    already seen, thus the answer is the count of the symbols that match
    ANY term.  One OR query counts exactly that set, and it does not have
    to build it.
    """
    terms = _name_token_terms(query)
    if len(terms) <= 1:
        return 0
    return count_symbols(
        c, " ".join(terms), config_hash,
        exclude_variables=True, project_only=project_only,
    )


def _search_code_individual_terms(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, project_only: bool,
    root: Path, offset: int = 0,
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

    Why the offset slices the MERGED list:
        The rows of the terms interleave.  Giving the offset to each term
        query would walk each term past its own rows, and the merge would
        then step over the rows between — page 2 of a two-term answer
        would jump from row 4 to row 13 and hide the eight between.  Each
        term reads ``offset + limit`` rows, the merge runs, and the slice
        comes last.

    Why the terms interleave and no longer follow one another:
        A page of the concatenation holds the first term alone until that
        term runs out.  The reader then reads a whole page about one word
        of a two-word query.  Round-robin puts both on the first page,
        which is what this strategy exists for.
    """
    terms = _name_token_terms(query)
    if len(terms) <= 1:
        return None  # Need at least 2 terms for individual search to make sense
    reach = max(0, offset) + limit
    per_term = [
        search_symbols(
            c, term, config_hash, limit=reach,
            kind=None, exclude_variables=True, project_only=project_only,
        )
        for term in terms
    ]
    seen_usr: set[str] = set()
    merged: list = []
    for rank in range(max((len(rows) for rows in per_term), default=0)):
        for rows in per_term:
            if rank >= len(rows):
                continue
            row = rows[rank]
            if row["usr"] not in seen_usr:
                seen_usr.add(row["usr"])
                merged.append(row)
    rows = merged[max(0, offset):reach]
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, "individual_terms")


_MACRO_FTS_FROM = """FROM macros_fts
               JOIN macros m ON m.id = macros_fts.rowid
               JOIN files f ON f.id = m.file_id
               WHERE macros_fts MATCH ? AND m.config_hash = ?"""


def count_macros_fts(
    c: sqlite3.Connection, query: str, config_hash: str,
    _kind: str | None = None, project_only: bool = False,
) -> int:
    """Count the macros that ``_search_code_macros_fts`` answers with.

    A database error counts as zero, for the same reason that the strategy
    answers with None: every earlier strategy already found nothing, thus
    "no results at all" is the honest answer.
    """
    project_filter = "AND f.is_project = 1" if project_only else ""
    try:
        return c.execute(
            f"SELECT COUNT(*) {_MACRO_FTS_FROM} {project_filter}",
            (_expand_query(query), config_hash),
        ).fetchone()[0]
    except Exception as exc:
        if not is_db_exception(exc):
            raise
        return 0


def _search_code_macros_fts(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, project_only: bool,
    root: Path, offset: int = 0,
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

    Why the order ends at ``m.id``:
        ``rank`` ties — several macros score alike — and SQLite may then
        return tied rows in any order.  An OFFSET over such an order shows
        one macro twice and hides another.  ``m.id`` is unique.
    """
    try:
        project_filter = "AND f.is_project = 1" if project_only else ""
        m_rows = c.execute(
            f"""SELECT m.*, f.path AS file_path
               {_MACRO_FTS_FROM} {project_filter}
               ORDER BY rank, m.id
               LIMIT ? OFFSET ?""",
            (_expand_query(query), config_hash, limit, max(0, offset)),
        ).fetchall()
        if not m_rows:
            return None
        macro_dicts: list[dict[str, Any]] = []
        for r in m_rows:
            # The signature carries the parameter list, thus a reader sees
            # how the macro is invoked without a second call.  `params` used
            # to be glued to the front of `value`, where nothing could read
            # it apart from the replacement text.
            extra: dict[str, Any] = {
                "kind": "macro",
                "qualified_name": r["name"],
                "signature": "#define " + macro_signature(
                    r["name"], bool(r["is_function_like"]), r["params"],
                ),
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

# The same chain with the counter of each step beside it.  A paged caller
# needs both: the count decides WHICH step owns the answer, because an
# empty page of a step that did match means "past the end" and not "try
# the next step".  The pipeline layer does not page and uses the list
# above alone.
_SEARCH_CODE_STEPS: list[tuple[FallbackFunc, CountFunc]] = [
    (_search_code_name_tokens, count_name_tokens),
    (_search_code_docstring, count_docstring),
    (_search_code_individual_terms, count_individual_terms),
    (_search_code_macros_fts, count_macros_fts),
]
