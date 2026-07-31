"""Fallback search phases for the search pipeline.

Each phase implements one fallback strategy from
:mod:`fw_context_mcp.mcp.handlers._search_fallbacks`.  When the primary
FTS5 search returns no results, these phases try progressively broader
strategies: name-token matching, docstring LIKE, individual-term search,
and macro FTS lookup.

All phases respect the progressive-fallback contract: ``should_run()``
returns ``True`` only when ``fts5_results`` is empty, and ``run()``
populates ``fts5_results`` only when matches are found.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from fw_context_mcp.indexer.db import _expand_query, open_db, search_symbols
from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.utils import abs_path

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


# ── Shared helpers ───────────────────────────────────────────────────────────


def _symbol_row_to_dict(r: sqlite3.Row, root, **extra) -> dict:
    """Convert a symbol sqlite3.Row to a dict for MCP tool output."""
    d = {
        "name": r["name"],
        "qualified_name": r["qualified_name"],
        "kind": r["kind"],
        "file": abs_path(root, str(r["file_path"])),
        "line": r["line"],
        "is_definition": bool(r["is_definition"]),
        "signature": r["signature"] or "",
        "docstring": r["docstring"] or "",
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
    d.update(extra)
    return d


# ── Fallback phases ──────────────────────────────────────────────────────────


class NameTokensFallbackPhase(Phase):
    """Fallback: FTS5 symbol-name token search with substring LIKE matching.

    Splits the query into terms and matches them against the pre-computed
    ``name_tokens`` column (CamelCase/snake_case split).  Requires at
    least N-1 of N query terms to match.
    """

    name = "name_tokens_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        conn = open_db(ctx.db_path)
        try:
            with conn:
                rows = _do_name_tokens_fallback(
                    conn, ctx.query, ctx.config_hash,
                    ctx.limit, project_only=False,
                )
                if rows:
                    return ctx.evolve(fts5_results=rows)
        finally:
            conn.close()
        return ctx


class DocstringFallbackPhase(Phase):
    """Fallback: single-term docstring LIKE search.

    Only runs when the query is a single word — does a raw LIKE over
    the ``docstring`` column to catch terms the FTS5 tokeniser missed.
    """

    name = "docstring_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        conn = open_db(ctx.db_path)
        try:
            with conn:
                rows = _do_docstring_fallback(
                    conn, ctx.query, ctx.config_hash,
                    ctx.limit, project_only=False,
                )
                if rows:
                    return ctx.evolve(fts5_results=rows)
        finally:
            conn.close()
        return ctx


class IndividualTermsFallbackPhase(Phase):
    """Fallback: search each query word individually and merge results.

    Only runs when the query has 2+ words — searches each word separately
    via FTS5 and merges de-duplicated results.
    """

    name = "individual_terms_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        conn = open_db(ctx.db_path)
        try:
            with conn:
                rows = _do_individual_terms_fallback(
                    conn, ctx.query, ctx.config_hash,
                    ctx.limit, project_only=False,
                )
                if rows:
                    return ctx.evolve(fts5_results=rows)
        finally:
            conn.close()
        return ctx


class MacrosFtsFallbackPhase(Phase):
    """Fallback: FTS5 search over the ``macros_fts`` table.

    Matches ``#define`` names and expansion values — the last resort
    when no symbol matched any of the previous strategies.
    """

    name = "macros_fts_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        conn = open_db(ctx.db_path)
        try:
            with conn:
                rows = _do_macros_fts_fallback(
                    conn, ctx.query, ctx.config_hash,
                    ctx.limit, project_only=False,
                )
                if rows:
                    return ctx.evolve(fts5_results=rows)
        finally:
            conn.close()
        return ctx


# ── Internal fallback implementations ────────────────────────────────────────


def _do_name_tokens_fallback(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, project_only: bool = False,
) -> list[dict]:
    """Token-based LIKE fallback — matches CamelCase/snake_case token splits."""
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if not terms:
        return []
    min_matches = max(1, len(terms) - 1)
    like_cases = []
    like_params = []
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
    return [_symbol_row_to_dict(r, None, _fallback="name_tokens_like") for r in rows]


def _do_docstring_fallback(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, project_only: bool = False,
) -> list[dict]:
    """Single-term docstring LIKE fallback."""
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if len(terms) != 1:
        return []
    escaped = terms[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    project_filter = "AND s.is_project = 1" if project_only else ""
    rows = c.execute(
        f"""SELECT s.* FROM symbols s
           WHERE s.config_hash = ? {project_filter}
             AND s.docstring LIKE ? ESCAPE '\\'
           ORDER BY s.is_definition DESC, s.line
           LIMIT ?""",
        (config_hash, f"%{escaped}%", limit),
    ).fetchall()
    return [_symbol_row_to_dict(r, None, _fallback="docstring_like") for r in rows]


def _do_individual_terms_fallback(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, project_only: bool = False,
) -> list[dict]:
    """Individual-term FTS5 fallback — each word searched separately."""
    terms = [t.lower() for t in query.split() if len(t) > 1]
    if len(terms) <= 1:
        return []
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
    return [_symbol_row_to_dict(r, None, _fallback="individual_terms") for r in rows]


def _do_macros_fts_fallback(
    c: sqlite3.Connection, query: str, config_hash: str,
    limit: int, project_only: bool = False,
) -> list[dict]:
    """Macro FTS fallback — searches #define names and values."""
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
            return []
        macro_dicts: list[dict] = []
        for r in m_rows:
            d: dict = {
                "name": r["name"],
                "qualified_name": r["name"],
                "kind": "macro",
                "file": abs_path(None, str(r["file_path"])),
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
        return macro_dicts
    except (sqlite3.OperationalError, Exception):
        return []
