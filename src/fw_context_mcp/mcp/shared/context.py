"""Shared DB context helpers — path resolution, safe open, staleness checks."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ...config import Config, derive_project_id
from ...config import load as load_config
from ...indexer.db import DatabaseCorruptionError, open_db
from ...utils import resolve_project_root

log = logging.getLogger(__name__)

# Per-process integrity-check cache: PRAGMA integrity_check scans the entire
# DB (15–30 s on 6+ GB).  We run it once per database path and skip it on
# subsequent opens — corruption is rare and read queries cannot cause it.
_integrity_checked: set[str] = set()

# Sentinel for detecting an uninitialized project at MCP server startup.
# Set in main() before mcp.run() — if the project is not initialized
# or has no index, all tool handlers return a uniform error message.
_server_init_error: str | None = None


def _set_server_init_error(message: str) -> None:
    """Set the error message that all tool handlers will see.

    Call only in main() before starting the server. After the sentinel
    is set, every tool handler will terminate with this message — the LLM
    forwards it to the user.
    """
    global _server_init_error
    _server_init_error = message


def _check_server_ready() -> None:
    """Check that the project is ready (initialized + indexed).

    Raises RuntimeError with user instructions if the server detected
    an uninitialized project at startup.
    """
    if _server_init_error is not None:
        raise RuntimeError(_server_init_error)


def _db_path(project_root: Path) -> Path:
    """Resolve the SQLite database path for a project root."""
    _check_server_ready()
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _resolve_context(project_root: str | None) -> tuple[Path, Config, str, Path]:
    """Resolve all context needed by most tools: db_path, config, project_id, root."""
    _check_server_ready()
    root = resolve_project_root(project_root)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    return cfg.index.db_dir / project_id / "index.db", cfg, project_id, root


def _open_db_safe(db_path: Path) -> tuple[sqlite3.Connection | None, dict | None]:
    """Open the database, returning (conn, None) or (None, error_dict) on corruption.

    Runs ``PRAGMA integrity_check`` only once per database path (per process
    lifetime).  Subsequent opens skip the check — on 5+ GB databases a full
    scan takes 15–30 s and turns every MCP query into a disk-bound operation.
    """
    db_key = str(db_path.resolve())
    skip = db_key in _integrity_checked
    try:
        conn = open_db(db_path, skip_integrity_check=skip)
        _integrity_checked.add(db_key)
        return conn, None
    except DatabaseCorruptionError as e:
        # Remove from cache so the user can see the full error on retry
        _integrity_checked.discard(db_key)
    except DatabaseCorruptionError as e:
        return None, {
            "error": f"Database corruption detected: {e.details}",
            "action": "reset_index",
            "hint": "Run reset_index() then fw-context index to rebuild.",
        }


def _is_stale(cfg, compile_commands_path: str) -> bool:
    """Check if compile_commands.json is newer than the index timestamp.

    Delegates to :func:`fw_context_mcp.utils.is_compile_commands_stale`.
    Returns False on any error (missing file, bad timestamp) to avoid
    blocking queries when the staleness check itself fails.
    """
    from fw_context_mcp.utils import is_compile_commands_stale

    try:
        created_at = cfg["created_at"]
    except (KeyError, IndexError):
        return False
    if not created_at:
        return False
    return is_compile_commands_stale(created_at, compile_commands_path)


def _detect_build_system(root: Path) -> str:
    """Detect the build system in use from well-known project files.

    Delegates to :func:`fw_context_mcp.indexer.build.detect_build_system`
    and returns its result or ``"unknown"`` when nothing is recognised.
    """
    from ...indexer.build import detect_build_system

    return detect_build_system(root) or "unknown"

