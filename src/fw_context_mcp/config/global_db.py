"""Global project registry — maps ``project_id`` (UUID4 hex) to metadata.

This database lives at ``~/.fw-context/projects.db`` and is shared across
all firmware projects.  It answers the question "what project is this UUID4?"
— returning the project name, build system type, and last-known root path.

The registry is populated:
- At ``fw-context init`` (``project_type = "unknown"``)
- At ``fw-context index`` (``project_type`` updated from build detection)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_GLOBAL_DB_PATH = Path.home() / ".fw-context" / "projects.db"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    project_type TEXT NOT NULL,
    root_path    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at);
"""

# Module-level connection cache — reuses the same connection for the
# lifetime of the process.  The global registry is read-mostly (written at
# init and index time, read on every MCP tool invocation), so a long-lived
# connection is safe.
_global_conn: sqlite3.Connection | None = None
_global_lock = threading.Lock()  # guards initialization


def _global_db_path() -> Path:
    """Return the path to the global projects database."""
    return _GLOBAL_DB_PATH


def open_global_db() -> sqlite3.Connection:
    """Open the global project registry database, creating it if needed.

    Returns a cached connection — the registry is read-mostly so a single
    long-lived connection is safe and avoids per-call open overhead.
    """
    global _global_conn
    with _global_lock:
        if _global_conn is not None:
            try:
                _global_conn.execute("SELECT 1")
            except sqlite3.Error:
                try:
                    _global_conn.close()
                except Exception:
                    pass
                _global_conn = None  # stale connection, reopen

        if _global_conn is None:
            db_path = _global_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)

            _global_conn = sqlite3.connect(str(db_path))
            _global_conn.row_factory = sqlite3.Row
            _global_conn.execute("PRAGMA journal_mode=WAL")
            _global_conn.execute("PRAGMA foreign_keys=ON")
            _global_conn.executescript(_SCHEMA)
            _global_conn.commit()

    return _global_conn


def upsert_project_registry(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    project_type: str,
    root_path: str,
) -> None:
    """Insert or update a project in the global registry.

    Args:
        conn: An open connection from ``open_global_db()``.
        project_id: UUID4 hex string (32 chars).
        name: Human-readable project name.
        project_type: Build system key — one of ``"mbed-os"``, ``"zephyr"``,
            ``"platformio"``, ``"arduino"``, ``"cmake"``, ``"esp-idf"``,
            ``"makefile"``, ``"bare"``, ``"keil-mdk"``, ``"iar-ewarm"``,
            ``"stm32cubeide"``, ``"ti-ccs"``, or ``"unknown"``.
        root_path: Absolute path to the project root directory (last known).
    """
    conn.execute(
        """INSERT OR REPLACE INTO projects(project_id, name, project_type, root_path, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))""",
        (project_id, name, project_type, root_path),
    )
    conn.commit()


def get_project_by_id(project_id: str) -> dict | None:
    """Look up a project in the global registry by its UUID4 ID.

    Returns a dict with keys ``project_id``, ``name``, ``project_type``,
    ``root_path``, ``created_at``, ``updated_at``, or ``None`` when not found.
    """
    conn = open_global_db()
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_all_registered_projects() -> list[dict]:
    """Return all projects from the global registry, newest first."""
    conn = open_global_db()
    rows = conn.execute(
        "SELECT * FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
