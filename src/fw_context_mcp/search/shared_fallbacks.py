"""Shared fallback search strategies — canonical implementations.

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
    for field in ("summary", "inputs", "outputs"):
        if row.get(field):
            d[field] = row[field]
    d.update(extra)
    return d


# ── Formatting helper ────────────────────────────────────────────────────────


def _fmt_symbol_rows(rows: list, root: Path, method: str) -> tuple[list[dict[str, Any]], str]:
    """Convert a list of symbol rows to dicts with a ``_fallback`` marker.

    The marker is omitted for direct FTS5 hits (``method == "fts5+kind"``)
    because those are primary results, not fallback results.
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
    """Token-based LIKE fallback — matches CamelCase/snake_case token splits."""
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
    """Single-term docstring LIKE fallback — only runs for 1-word queries."""
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
    """Individual-term FTS5 fallback — each word searched separately."""
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if len(terms) <= 1:
        return None
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
    """Macro FTS fallback — searches ``#define`` names and values."""
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
        return None


# ── Ordered fallback chain ───────────────────────────────────────────────────

_SEARCH_CODE_FALLBACKS: list[FallbackFunc] = [
    _search_code_name_tokens,
    _search_code_docstring,
    _search_code_individual_terms,
    _search_code_macros_fts,
]
