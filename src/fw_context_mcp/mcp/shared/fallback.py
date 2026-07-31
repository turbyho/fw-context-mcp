"""Fallback search — lexical FTS5 when Ollama/embeddings are unavailable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...config import derive_project_id
from ...indexer.db import get_active_config
from ...utils import abs_path
from .context import _is_stale, _open_db_safe
from .stale import _stale_files


def _fallback_to_search_code(
    root: Path,
    db_path: Path,
    query: str,
    limit: int,
    warning: str,
) -> list[dict]:
    """Fall back to lexical search when Ollama/embeddings are unavailable.

    Uses the same DB-safe open path as regular tools so migrations and
    integrity checks run.  Adds a stale warning when the index is out of date.
    """
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    assert conn is not None
    try:
        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]
            config_hash = cfg["config_hash"]
            results = _fallback_to_search_code_inner(
                conn, root, query, config_hash, limit, warning
            )
            # Collect file paths for staleness check before closing
            result_files = [abs_path(root, r["file"]) for r in results if "file" in r]
            stale_f = _stale_files(conn, config_hash, result_files, root)
            is_stale = _is_stale(cfg, cfg["compile_commands_path"])
    finally:
        pass  # connection managed by connection.py cache

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
