"""Search fallback strategies and shared formatting utilities."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fw_context_mcp.indexer.db import _expand_query, search_symbols
from fw_context_mcp.utils import abs_path

def _search_code_fts5_kind(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, kind: str | None, project_only: bool,
    root: Path,
) -> tuple[list[dict], str] | None:
    rows = search_symbols(
        c, query, config_hash, limit=limit, kind=kind,
        exclude_variables=False, project_only=project_only,
    )
    method = "fts5+kind"
    if not rows and kind:
        rows = search_symbols(
            c, query, config_hash, limit=limit, kind=None,
            exclude_variables=False, project_only=project_only,
        )
        if rows:
            method = "fts5"
    if not rows:
        return None
    return _fmt_symbol_rows(rows, root, method)


def _search_code_name_tokens(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, _kind: str | None, _project_only: bool,
    root: Path,
) -> tuple[list[dict], str] | None:
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if not terms:
        return None
    min_matches = max(1, len(terms) - 1)
    like_cases = []
    like_params = []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_cases.append("CASE WHEN s.name_tokens LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END")
        like_params.append(f"%{escaped}%")
    match_sum = " + ".join(like_cases)
    project_filter = "AND s.is_project = 1" if _project_only else ""
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
    limit: int, _kind: str | None, _project_only: bool,
    root: Path,
) -> tuple[list[dict], str] | None:
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if len(terms) != 1:
        return None
    escaped = terms[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    project_filter = "AND s.is_project = 1" if _project_only else ""
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
) -> tuple[list[dict], str] | None:
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
    limit: int, _kind: str | None, _project_only: bool,
    root: Path,
) -> tuple[list[dict], str] | None:
    try:
        expanded = _expand_query(query)
        project_filter = "AND f.is_project = 1" if _project_only else ""
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
            if r["expanded_value"]:
                d["_macro_expanded_value"] = r["expanded_value"]
            macro_dicts.append(d)
        return macro_dicts, "macros_fts"
    except (sqlite3.OperationalError, Exception):
        return None


_SEARCH_CODE_FALLBACKS = [
    _search_code_name_tokens,
    _search_code_docstring,
    _search_code_individual_terms,
    _search_code_macros_fts,
]



def _symbol_row_to_dict(r, root: Path, **extra) -> dict:
    """Convert a symbol sqlite3.Row to a dict for MCP tool output.

    Includes all standard symbol fields plus any **extra kwargs.
    Conditional fields (template_usr, parent_usr, enum_value, summary,
    inputs, outputs) are only added when present and non-empty.
    """
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
    d.update(extra)
    return d


def _fmt_symbol_rows(rows: list, root: Path, method: str) -> tuple[list[dict], str]:
    extra = {"_fallback": method} if method != "fts5+kind" else {}
    result = [_symbol_row_to_dict(r, root, **extra) for r in rows]
    return result, method

