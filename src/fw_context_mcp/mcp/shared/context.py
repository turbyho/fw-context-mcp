"""Shared DB context helpers — path resolution, safe open, staleness checks."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ...config import Config, derive_project_id
from ...config import load as load_config
from ...indexer.db import DatabaseCorruptionError, open_db
from ...utils import MTIME_TOLERANCE_S, resolve_project_root

log = logging.getLogger(__name__)

# Per-process integrity-check cache: PRAGMA integrity_check scans the entire
# DB (15–30 s on 6+ GB).  We run it once per database path and skip it on
# subsequent opens — corruption is rare and read queries cannot cause it.
_integrity_checked: set[str] = set()

# Sentry pro detekci nepřipraveného projektu při startu MCP serveru.
# Nastavuje se v main() před mcp.run() — pokud projekt není inicializovaný
# nebo nemá index, všechny tool handlery vrací jednotnou chybovou zprávu.
_server_init_error: str | None = None


def _set_server_init_error(message: str) -> None:
    """Nastav chybovou zprávu, kterou uvidí všechny tool handlery.

    Volej pouze v main() před spuštěním serveru. Po nastavení sentinelu
    každý tool handler skončí s touto zprávou — LLM ji přepošle uživateli.
    """
    global _server_init_error
    _server_init_error = message


def _check_server_ready() -> None:
    """Zkontroluj, že projekt je připravený (inicializovaný + indexovaný).

    Vyhazuje RuntimeError s instrukcemi pro uživatele, pokud server
    detekoval nepřipravený projekt při startu.
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
        return None, {
            "error": f"Database corruption detected: {e.details}",
            "action": "reset_index",
            "hint": "Run reset_index() then fw-context index to rebuild.",
        }


def _is_stale(cfg, compile_commands_path: str) -> bool:
    """Check if compile_commands.json is newer than the index timestamp.

    Returns False on any error (missing file, bad timestamp) to avoid
    blocking queries when the staleness check itself fails.
    """
    try:
        cc_mtime = os.path.getmtime(compile_commands_path)
        indexed_at = datetime.fromisoformat(cfg["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + MTIME_TOLERANCE_S
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        # Return False on error: a broken compile_commands.json or missing
        # timestamp should not block all queries with stale warnings.
        # The index metadata check (schema_version) still catches real staleness.
        log.warning("Staleness check failed: %s", e)
        return False


def _detect_build_system(root: Path) -> str:
    """Detect the build system in use from well-known project files.

    Delegates to :func:`fw_context_mcp.indexer.build.detect_build_system`
    and returns its result or ``"unknown"`` when nothing is recognised.
    """
    from ...indexer.build import detect_build_system

    return detect_build_system(root) or "unknown"

