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
from pathlib import Path

from ...config import derive_project_id
from ...indexer.db import get_active_config
from ...utils import abs_path
from .context import _is_stale, _quick_open_readonly, get_executor
from .stale import _stale_files


def _fallback_to_search_code(
    root: Path,
    db_path: Path,
    query: str,
    limit: int,
    warning: str,
) -> list[dict]:
    """Fall back to lexical search when Ollama/embeddings are unavailable.

    Runs on the shared executor connection (same as regular tools) and
    adds a stale warning when the index is out of date.  ``config_hash``
    is read fresh per request via a short-lived read-only connection.
    """
    try:
        conn = _quick_open_readonly(db_path)
    except sqlite3.Error as e:
        # Read-only open does not create a missing file — report instead
        # of crashing (callers may pass a path whose index was deleted).
        return [{"error": f"Cannot open index database {db_path}: {e}. Run 'fw-context index' first."}]
    try:
        project_id = derive_project_id(root)
        cfg = get_active_config(conn, project_id)
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
            db_conn, root, query, cfg_hash, limit, warning
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
) -> list[dict]:
    """Inner fallback with an open connection."""
    from fw_context_mcp.indexer.db import search_symbols

    rows = search_symbols(
        conn, query, config_hash, limit=limit, kind=None,
        exclude_variables=True,
    )
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
        return [{"warning": f"{warning} (no lexical results either)."}]

    return [
        {"warning": warning, "_method": "search_code_fallback"},
        *results,
    ]
