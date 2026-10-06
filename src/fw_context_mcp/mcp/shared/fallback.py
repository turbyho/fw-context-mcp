"""Fallback search — lexical FTS5 when Ollama/embeddings are unavailable.

WHY this module exists: semantic and smart search depend on an LLM
backend (Ollama or cloud API) for embedding generation and query
translation.  When that backend is offline — common on air-gapped
build machines or before first-time setup — the search tools must
degrade gracefully to pure lexical FTS5 instead of returning an error.
An error would make the MCP server appear broken, so the assistant
would stop using fw-context entirely.  A fallback with a clear warning
lets the assistant continue working with lexical precision only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from ...config import derive_project_id
from ...config import load as load_config
from ...utils import abs_path
from .context import _is_stale, _quick_open_readonly, get_executor
from .paging import page_hint, page_notice, past_end_info
from .stale import _stale_files
from .variants import active_build


def _fallback_to_search_code(
    root: Path,
    db_path: Path,
    query: str,
    limit: int,
    warning: str,
    offset: int = 0,
    hint_args: Mapping[str, object] | None = None,
) -> list[dict]:
    """Fall back to lexical search when Ollama/embeddings are unavailable.

    Runs on the shared executor connection (same as regular tools) and
    adds a stale warning when the index is out of date.  ``config_hash``
    is read fresh per request via a short-lived read-only connection.

    The answer is one page, at *offset*, with the page notice of
    semantic_search: the reader pages the tool it called, and the same
    cause gives the same fallback on the next page.  *hint_args* go into
    the hint, thus the next call asks for the same set.
    """
    try:
        conn = _quick_open_readonly(db_path)
    except sqlite3.Error as e:
        # Read-only open does not create a missing file — report instead
        # of crashing (callers may pass a path whose index was deleted).
        return [{"error": f"Cannot open index database {db_path}: {e}. Run 'fw-context index' first."}]
    try:
        project_id = derive_project_id(root)
        # The build of semantic_search, which takes no selector: the build
        # that its main path reads (see active_build), and not the newest.
        cfg, refusal = active_build(conn, project_id, load_config(root), root)
        if refusal:
            return [{"error": refusal}]
        if not cfg:
            return [{"error": "No build config indexed."}]
        config_hash = cfg["config_hash"]
        compile_commands_path = cfg["compile_commands_path"]
    finally:
        conn.close()

    executor = get_executor(db_path)

    def _query(db_conn, cfg_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        results = _fallback_to_search_code_inner(
            db_conn, root, query, cfg_hash, limit, warning, offset=offset, hint_args=hint_args,
        )
        result_files = [abs_path(root, r["file"]) for r in results if "file" in r]
        stale_f = _stale_files(db_conn, cfg_hash, result_files, root)
        return results, stale_f

    results, stale_f = executor.execute_sync(_query, config_hash)

    is_stale, _ = _is_stale(cfg, compile_commands_path)
    if is_stale:
        results.insert(0, {
            "warning": "Index may be stale — compile_commands.json changed. Run 'fw-context index' to update.",
            "_method": "search_code_fallback",
        })
    if stale_f:
        results.insert(0, {
            "warning": f"Results may be stale — {len(stale_f)} file(s) changed. Run 'fw-context index' to update.",
            "_method": "search_code_fallback",
        })
    return results


def _fallback_to_search_code_inner(
    conn: sqlite3.Connection,
    root: Path,
    query: str,
    config_hash: str,
    limit: int,
    warning: str,
    offset: int = 0,
    hint_args: Mapping[str, object] | None = None,
) -> list[dict]:
    """Inner fallback with an open connection.

    One page of the plain FTS5 symbol search, and its count on the same
    conditions (``count_symbols``), thus ``total`` describes the rows.
    *hint_args* are the arguments of the semantic_search call that the hint
    repeats: a threshold that is not the default, and the project.
    """
    from fw_context_mcp.indexer.db import search_symbols
    from fw_context_mcp.indexer.db._symbols import count_symbols

    rows = search_symbols(
        conn, query, config_hash, limit=limit, kind=None,
        exclude_variables=True, offset=offset,
    )
    total = count_symbols(conn, query, config_hash, kind=None, exclude_variables=True)
    results: list[dict] = []
    for r in rows:
        d = {
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file": abs_path(root, r["file_path"]),
            "line": r["line"],
            "is_definition": bool(r["is_definition"]),
            "signature": r["signature"],
            "docstring": r["docstring"],
            "_method": "search_code_fallback",
        }
        if r["enum_value"] is not None:
            d["enum_value"] = r["enum_value"]
        results.append(d)

    if not results:
        if offset and total:
            return [
                {"warning": warning, "_method": "search_code_fallback"},
                past_end_info("symbol", offset, total),
            ]
        return [{"warning": f"{warning} (no lexical results either)."}]

    notice = page_notice(
        total, offset, len(results),
        hint=page_hint("semantic_search", query, **(hint_args or {}),
                       next_offset=offset + len(results)),
    )
    return [
        {"warning": warning, "_method": "search_code_fallback"},
        notice,
        *results,
    ]
