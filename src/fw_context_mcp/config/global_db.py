"""Global project registry — maps ``project_id`` (UUID4 hex) to metadata.

This database lives at ``~/.fw-context/projects.db`` and is shared across
all firmware projects.  It answers the question "what project is this UUID4?"
— returning the project name, build system type, and last-known root path.

The registry is populated:
- At ``fw-context init`` (``project_type = "unknown"``)
- At ``fw-context index`` (``project_type`` updated from build detection)

WHY a separate global registry instead of embedding metadata in each
project's index database: the MCP server needs to resolve project IDs
to names before opening the project's own database.  For example,
``list_projects`` must return the project name without opening every
``index.db`` on the filesystem and running ``get_active_config`` on each.
The registry is a fast lookup table — one SQLite file, one query per
lookup.

WHY the connection is cached at module level: the registry is read on
EVERY MCP tool invocation (via ``derive_project_id``).  Opening and
closing a SQLite connection per call would add 2-5 ms of filesystem
overhead.  A long-lived connection with WAL mode supports concurrent
readers (multiple MCP servers) and infrequent writers (init/index).
The health check (``SELECT 1``) before reuse catches stale connections
from process forks.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

from fw_context_mcp.utils import SAFE_EXCEPT, is_fatal

log = logging.getLogger(__name__)

#: Where the registry lives when nothing redirects it.
_DEFAULT_GLOBAL_DB_PATH = Path.home() / ".fw-context" / "projects.db"

#: The path that :func:`_global_db_path` uses unless it is redirected.
#: A test may set this attribute to reach a registry of its own; see that
#: function for what wins over what.
_GLOBAL_DB_PATH = _DEFAULT_GLOBAL_DB_PATH

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
#: The path that ``_global_conn`` was opened on.  The cache is keyed on it
#: because the redirect can move between two calls — a test sets it, and a
#: connection from before would then read the registry of the operator.
_global_conn_path: Path | None = None
_global_lock = threading.Lock()  # guards initialization


def _global_db_path() -> Path:
    """Return the path to the global projects database.

    Three sources, and they are read in this order:

    1. ``_GLOBAL_DB_PATH`` when something set it away from the default.
       An in-process test does that, and it must win — the test knows
       which registry it wants.
    2. ``FW_CONTEXT_PROJECTS_DB``.  A subprocess cannot inherit a
       monkeypatched attribute, and this suite spawns the CLI in one,
       thus the environment carries the redirect across the boundary.
    3. The default, ``~/.fw-context/projects.db``.

    WHY the redirect exists at all: ``conftest.py`` already points
    ``FW_CONTEXT_INDEX_DIR`` at a temp directory so that no test writes
    the real index, and the registry had no such door.  Every test run
    therefore registered its throwaway projects in the registry of the
    operator: measured on one machine, 28989 rows of which 6 named a
    project that exists.  The same shape as the index dir, for the same
    reason.
    """
    if _GLOBAL_DB_PATH != _DEFAULT_GLOBAL_DB_PATH:
        return _GLOBAL_DB_PATH
    env = os.environ.get("FW_CONTEXT_PROJECTS_DB")
    return Path(env).expanduser() if env else _GLOBAL_DB_PATH


def open_global_db() -> sqlite3.Connection:
    """Open the global project registry database, creating it if needed.

    Returns a cached connection — the registry is read-mostly so a single
    long-lived connection is safe and avoids per-call open overhead.
    """
    global _global_conn, _global_conn_path
    with _global_lock:
        wanted = _global_db_path()
        if _global_conn is not None and _global_conn_path != wanted:
            # The redirect moved after this connection opened.  Reusing it
            # would read the registry that the caller just redirected away
            # from, and a test would then write the real one.
            try:
                _global_conn.close()
            except SAFE_EXCEPT as e:
                if is_fatal(e):
                    raise
            _global_conn = None

        if _global_conn is not None:
            try:
                _global_conn.execute("SELECT 1")
            except sqlite3.Error:
                try:
                    _global_conn.close()
                except SAFE_EXCEPT as e:
                    if is_fatal(e):
                        raise
                    pass
                _global_conn = None  # stale connection, reopen

        if _global_conn is None:
            db_path = wanted
            db_path.parent.mkdir(parents=True, exist_ok=True)

            _global_conn = sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                db_path.chmod(0o600)
            except OSError:
                pass
            _global_conn.row_factory = sqlite3.Row
            _global_conn.execute("PRAGMA journal_mode=WAL")
            _global_conn.execute("PRAGMA foreign_keys=ON")
            _global_conn.executescript(_SCHEMA)
            _global_conn.commit()
            _global_conn_path = db_path

    return _global_conn


def _index_db_of(project_id: str) -> Path:
    """Give the DEFAULT index database of *project_id*.

    A registry row carries no config, thus the default location is the
    only one this module can read.  ``FW_CONTEXT_INDEX_DIR`` overrides it
    for the whole process, as ``config/settings.py`` does for a project.
    A project that names its own ``db_dir`` in config.toml keeps its index
    somewhere else, and this function then reports a path that does not
    exist — see :func:`_row_is_stale` for what that costs.
    """
    root = os.environ.get("FW_CONTEXT_INDEX_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".fw-context" / "index"
    return base / project_id / "index.db"


def _row_is_stale(project_id: str, root_path: str) -> bool:
    """Does anything on disk still confirm this registry row?

    A row maps a NAME and an id to a path, and it holds nothing else.  It
    is worth keeping only while something on disk still answers for it,
    thus the test is ONE positive condition and the rest is stale:

    * an index database of this id exists, or
    * the config at the root declares exactly this ``project_id``.

    WHY the rule reads that way round.  Written as a list of the ways a
    row can die — the root is gone, the root holds no ``.fw-context``, the
    root declares another id — it missed the shapes that fit none of them:
    measured on one machine, 160 rows of a throwaway directory that still
    existed and held a config with no id at all.  A row that nothing
    confirms is stale, whatever the reason.

    The index database is the guard against a false positive: a project
    on a mount that is not attached right now cannot confirm itself, and
    its index still can.  A project that names its own ``db_dir`` defeats
    that guard, and the cost is one row that the next ``fw-context init``
    or ``fw-context index`` writes again.

    An unreadable path proves nothing, thus the row stays.
    """
    if _index_db_of(project_id).exists():
        return False
    if not root_path:
        return True
    try:
        config = Path(root_path) / ".fw-context" / "config.toml"
        if not config.is_file():
            return True
        return _declared_project_id(config) != project_id
    except OSError:
        return False


def _declared_project_id(config: Path) -> str:
    """Read ``id`` from the ``[project]`` table of a config, or give ``""``.

    The read is a scan and not a TOML parse, because a config that no
    parser accepts must still not raise here — a broken file leaves the
    row alone rather than deleting it.

    The scan tracks the section, thus an ``id`` key of another table
    cannot answer for the project.  Reading the first ``id`` of the file
    would let ``[llm]`` or a build variant decide whether a registry row
    lives.
    """
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    in_project = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("["):
            in_project = line.replace(" ", "") == "[project]"
            continue
        if not in_project:
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "id":
            continue
        return value.split("#")[0].strip().strip('"').strip("'")
    return ""


def prune_stale_projects(conn: sqlite3.Connection) -> int:
    """Drop the registry rows that nothing on disk refers to any more.

    WHY this runs by itself: the registry only ever grew.  A row is
    written at ``init`` and at ``index`` and nothing removed one, thus a
    project that moved, that was deleted, or that took a new id left its
    old row behind for ever.  Measured on one machine: 28989 rows, of
    which 6 named a project that exists.  This function left 60 of them
    and took 362 ms; the pass after it removed none.

    WHAT that cost: the row holds a NAME, and two rows of one name make
    the ``project="<name>"`` selector fail with an ambiguity error.  The
    id selector and ``project_root`` are unaffected, and no answer of any
    tool is wrong — the cost is one refused call on the shortest path.

    Returns the number of rows removed.  A failure removes none and
    raises nothing: a registry that cannot be tidied must not stop the
    write that called this.
    """
    try:
        rows = conn.execute("SELECT project_id, root_path FROM projects").fetchall()
    except sqlite3.Error:
        log.debug("prune: cannot read the registry", exc_info=True)
        return 0

    stale = [r["project_id"] for r in rows if _row_is_stale(r["project_id"], r["root_path"] or "")]
    if not stale:
        return 0
    try:
        for chunk_start in range(0, len(stale), 500):
            chunk = stale[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM projects WHERE project_id IN ({placeholders})", chunk  # noqa: S608
            )
        conn.commit()
    except sqlite3.Error:
        log.debug("prune: cannot delete stale rows", exc_info=True)
        return 0
    log.debug("prune: removed %d stale registry row(s)", len(stale))
    return len(stale)


def upsert_project_registry(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    project_type: str,
    root_path: str,
) -> None:
    """Insert or update a project in the global registry.

    This function only WRITES.  The tidy-up is
    :func:`prune_stale_projects`, and the two CLI commands that register a
    project call it straight after this one.  Keeping the two apart
    matters: a write that also deletes would judge every OTHER row at a
    moment that says nothing about them, and a caller that wants one row
    written could not ask for that alone.

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
        """INSERT INTO projects(project_id, name, project_type, root_path, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
           ON CONFLICT(project_id) DO UPDATE SET
               name = excluded.name,
               project_type = excluded.project_type,
               root_path = excluded.root_path,
               updated_at = excluded.updated_at""",
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


def get_projects_by_name(name: str) -> list[dict]:
    """Look up projects in the global registry by their name.

    The ``name`` column has no UNIQUE constraint, thus two projects can
    carry the same name — for example one checkout under ``work/`` and a
    second checkout of the same repository under ``archive/``.  This
    function returns every match, and the caller decides what an
    ambiguous name means.  ``get_project_by_id`` returns a single dict
    because ``project_id`` is the primary key.

    The match is exact and case-sensitive.  A prefix match would let one
    project name select a different project, and a wrong project answers
    with symbols that look correct but come from other source code.

    Args:
        name: Exact project name, as ``list_projects`` shows it.

    Returns:
        list of dicts with keys ``project_id``, ``name``, ``project_type``,
        ``root_path``, ``created_at``, ``updated_at``.  The list is empty
        when no project has this name.  The order is the most recently
        indexed project first.
    """
    conn = open_global_db()
    rows = conn.execute(
        "SELECT * FROM projects WHERE name = ? ORDER BY updated_at DESC",
        (name,),
    ).fetchall()
    return [dict(row) for row in rows]


