"""SQLite connection management for fw-context-mcp index."""

import fcntl
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from fw_context_mcp.indexer.db._schema import (
    _SCHEMA,
    CURRENT_SCHEMA_VERSION,
    _ensure_migrated_columns,
    _run_data_migrations,
    _table_exists,
)

log = logging.getLogger(__name__)


class DatabaseCorruptionError(sqlite3.DatabaseError):
    """Raised when the SQLite database fails integrity check.

    The caller should present the error to the user with a clear action:
    run ``reset_index()`` then ``fw-context index`` to rebuild.
    """

    def __init__(self, db_path: str, details: str = ""):
        self.db_path = db_path
        self.details = details
        super().__init__(f"Database corruption detected at {db_path}: {details}")


class WriteLockTimeout(Exception):
    """Raised when the write lock cannot be acquired within the timeout."""


@contextmanager
def write_lock(db_dir: Path, timeout: float = 60.0) -> Generator[None, None, None]:
    """Acquire an exclusive write lock for the index directory.

    Serializes all write operations (symbol storage, LLM analysis, embeddings)
    across processes.  Uses ``fcntl.flock`` — the kernel releases the lock
    automatically on process exit, so a crash never leaves a stale lock.

    Blocks for up to *timeout* seconds; raises ``WriteLockTimeout`` when the
    lock cannot be acquired in time.  Callers should catch and propagate
    the error gracefully — never retry indefinitely.

    Args:
        db_dir: Directory containing the index database (lock file is
            ``<db_dir>/write.lock``).
        timeout: Maximum time to wait for the lock, in seconds (default 60).

    Raises:
        WriteLockTimeout: Lock could not be acquired within *timeout*.
    """
    import time as _time

    lock_file = db_dir / "write.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    deadline = _time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if _time.monotonic() > deadline:
                    raise WriteLockTimeout(f"Could not acquire write lock for {db_dir} within {timeout:.0f}s") from None
                _time.sleep(0.5)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply standard PRAGMAs and extensions to a freshly opened connection."""
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA synchronous = 1")
    # Load sqlite-vec extension for vector search (graceful when missing)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
    except (ImportError, Exception):
        pass


def open_db(path: Path, *, skip_integrity_check: bool = False, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open SQLite database at *path*, enabling WAL mode and loading extensions.

    Creates the parent directory if missing.  Configures WAL journal mode,
    foreign keys, and a 30 s busy timeout.  Loads the ``sqlite-vec`` extension
    (best-effort — silently skipped when unavailable).

    Runs the full schema and migrations in sequence:

    1. Executes ``_SCHEMA`` (CREATE TABLE IF NOT EXISTS, indexes, triggers).
    2. Applies add-column migrations from ``_MIGRATION_ADD_COLUMNS``
       (idempotent — skips duplicate column errors).
    3. Backfills ``symbols.file_path`` from ``files.path`` for rows with
       empty ``file_path`` (migration for old indexes).
    4. Backfills ``symbols.name_tokens`` via Python ``split_tokens()``
       (SQLite cannot call Python functions).
     5. Rebuilds ``symbols_fts`` FTS5 virtual table when it lacks the
        ``name_tokens`` column (older schema).
      6. Backfills ``symbols.summary/inputs/outputs`` from ``llm_analysis``
         table, then rebuilds FTS5 again when it lacks those columns.
      7. Initialises the ``vec_symbols`` vec0 table (best-effort).
      8. Unconditionally ensures ``files_fts`` FTS5 table exists (self-healing
         when a migration was interrupted — see ``_CRITICAL_TABLES`` for the
         same pattern applied to regular tables).
      9. Runs ``PRAGMA integrity_check`` (unless *skip_integrity_check*
         is ``True``) — raises ``DatabaseCorruptionError`` on failure
         (detected before any tool reads the data).

    On ``OperationalError('locked')`` the connection is retried once after a
    short sleep.  Other ``OperationalError`` and ``DatabaseError`` are
    converted to ``DatabaseCorruptionError``.

    Args:
        path: Filesystem path to the SQLite database file.
        skip_integrity_check: When ``True``, skip the ``PRAGMA integrity_check``
            scan.  Use for auxiliary connections (worker threads, pooled
            connections) where the check was already performed on the primary
            connection.  On large databases (5+ GB) the scan takes 15–30 s
            and saturates disk I/O.
        check_same_thread: Passed through to ``sqlite3.connect``.  Set to
            ``False`` when the connection will be shared across threads
            (e.g. in a thread-safe connection cache).  Default ``True``.

    Returns:
        Open ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.

    Raises:
        DatabaseCorruptionError: When ``PRAGMA integrity_check`` fails or the
            database is otherwise unreadable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")  # 10s — wait on lock, don't fail
    conn.execute("PRAGMA foreign_keys = ON")

    # WAL mode — persistent on the DB file once set, but needs to be applied
    # on first-open.  Run outside executescript() to avoid an unnecessary
    # write transaction when the DB is already in WAL mode.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DatabaseCorruptionError(str(path), str(e)) from e

    _configure_connection(conn)

    # Only run the (expensive) schema/migration block when the on-disk schema
    # is outdated.  executescript() implies a write transaction — skipping it
    # when the schema is current means read-only queries never acquire a
    # write lock, even while a background reindex is writing.
    try:
        current_schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DatabaseCorruptionError(str(path), str(e)) from e

    if current_schema_ver < CURRENT_SCHEMA_VERSION:
        try:
            conn.executescript(_SCHEMA)
            _ensure_migrated_columns(conn)
            _run_data_migrations(conn)
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                conn.close()
                import time as _time
                _time.sleep(1)
                conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
                try:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA busy_timeout = 10000")
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute("PRAGMA journal_mode = WAL")
                    _configure_connection(conn)
                    conn.executescript(_SCHEMA)
                    _ensure_migrated_columns(conn)
                    _run_data_migrations(conn)
                    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                except Exception:
                    conn.close()
                    raise
            elif "no such column" in str(e):
                _ensure_migrated_columns(conn)
                try:
                    conn.executescript(_SCHEMA)
                    _run_data_migrations(conn)
                    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                except sqlite3.OperationalError as e3:
                    conn.close()
                    raise DatabaseCorruptionError(
                        str(path),
                        f"Schema migration failed after column recovery — "
                        f"the database may be in an inconsistent state: {e3}",
                    ) from e3
            else:
                conn.close()
                raise DatabaseCorruptionError(str(path), str(e)) from e
        except sqlite3.DatabaseError as e:
            conn.close()
            raise DatabaseCorruptionError(str(path), str(e)) from e

    # ── Unconditional column sanity check ──────────────────────────────────
    # Belt-and-suspenders: ensure all columns from _MIGRATION_ADD_COLUMNS
    # exist, even when the version gate skipped the main migration block.
    # Idempotent — on a fully migrated DB this is a single empty commit.
    _ensure_migrated_columns(conn)

    # ── Defensive table creation ──────────────────────────────────────────
    # Critical tables that were added after the initial _SCHEMA definition.
    # These run unconditionally (outside the version gate) so that a database
    # whose migration was interrupted — leaving user_version stamped but
    # tables missing — self-heals automatically.  CREATE TABLE IF NOT EXISTS
    # is idempotent and takes microseconds when the table already exists.
    #
    # IMPORTANT: every table definition below MUST be kept in sync with the
    # corresponding CREATE TABLE statement in _SCHEMA above.  When you change
    # one, change the other.  The two copies exist because _CRITICAL_TABLES
    # runs unconditionally (self-healing) while _SCHEMA runs only on fresh
    # databases and schema-version migrations.
    _CRITICAL_TABLES = """
    CREATE TABLE IF NOT EXISTS indirect_call_sites (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL,
        from_file    TEXT    NOT NULL,
        from_line    INTEGER NOT NULL,
        from_usr     TEXT,
        expr_text    TEXT    NOT NULL DEFAULT '',
        target_usr   TEXT    NOT NULL,
        target_name  TEXT    NOT NULL DEFAULT '',
        fn_ptr_type  TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_ics_config_target ON indirect_call_sites(config_hash, target_usr);
    CREATE INDEX IF NOT EXISTS idx_ics_config_file   ON indirect_call_sites(config_hash, from_file);
    CREATE INDEX IF NOT EXISTS idx_ics_config_usr    ON indirect_call_sites(config_hash, from_usr);

    CREATE TABLE IF NOT EXISTS fp_assignments (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL,
        from_file    TEXT    NOT NULL,
        from_line    INTEGER NOT NULL,
        lhs_usr      TEXT    NOT NULL,
        lhs_name     TEXT    NOT NULL DEFAULT '',
        rhs_usr      TEXT    NOT NULL,
        rhs_name     TEXT    NOT NULL DEFAULT '',
        fn_ptr_type  TEXT    NOT NULL DEFAULT '',
        method       TEXT    NOT NULL,
        from_usr     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_fpa_config_lhs ON fp_assignments(config_hash, lhs_usr);
    CREATE INDEX IF NOT EXISTS idx_fpa_config_rhs ON fp_assignments(config_hash, rhs_usr);
    CREATE INDEX IF NOT EXISTS idx_fpa_config_file ON fp_assignments(config_hash, from_file);

    CREATE TABLE IF NOT EXISTS inheritance (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
        derived_usr  TEXT    NOT NULL,
        base_usr     TEXT    NOT NULL,
        access       TEXT    NOT NULL DEFAULT 'public',
        is_virtual   INTEGER NOT NULL DEFAULT 0,
        UNIQUE(config_hash, derived_usr, base_usr)
    );
    CREATE INDEX IF NOT EXISTS idx_inheritance_derived ON inheritance(config_hash, derived_usr);
    CREATE INDEX IF NOT EXISTS idx_inheritance_base    ON inheritance(config_hash, base_usr);

    CREATE TABLE IF NOT EXISTS overrides (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
        derived_usr  TEXT    NOT NULL,
        base_usr     TEXT    NOT NULL,
        UNIQUE(config_hash, derived_usr, base_usr)
    );
    CREATE INDEX IF NOT EXISTS idx_overrides_derived ON overrides(config_hash, derived_usr);
    CREATE INDEX IF NOT EXISTS idx_overrides_base    ON overrides(config_hash, base_usr);

    CREATE TABLE IF NOT EXISTS hotspot_cache (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
        symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        caller_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE(config_hash, symbol_id)
    );
    CREATE INDEX IF NOT EXISTS idx_hotspot_cache_config ON hotspot_cache(config_hash);

    CREATE TABLE IF NOT EXISTS macros (
        id               INTEGER PRIMARY KEY,
        config_hash      TEXT    NOT NULL REFERENCES build_configs(config_hash),
        file_id          INTEGER NOT NULL REFERENCES files(id),
        name             TEXT    NOT NULL,
        value            TEXT    NOT NULL DEFAULT '',
        expanded_value   TEXT    NOT NULL DEFAULT '',
        line             INTEGER NOT NULL,
        is_function_like INTEGER NOT NULL DEFAULT 0,
        UNIQUE(config_hash, file_id, line)
    );
    CREATE INDEX IF NOT EXISTS idx_macros_name ON macros(name, config_hash);
    CREATE INDEX IF NOT EXISTS idx_macros_file ON macros(file_id);
    """
    conn.executescript(_CRITICAL_TABLES)

    # Lazy imports — these symbols are defined in __init__.py (sibling module).
    # Deferred to avoid circular import: __init__.py imports from _connection.py.
    from fw_context_mcp.indexer.db import init_vec_table, rebuild_files_fts, rebuild_macros_fts  # noqa: F811

    # Migrate vec0 table when sqlite-vec is available (idempotent CREATE IF NOT EXISTS)
    try:
        init_vec_table(conn)
    except Exception as e:
        log.warning(
            "sqlite-vec vector table initialization failed — semantic search will use legacy BLOB fallback: %s",
            e,
        )

    # Unconditional files_fts self-healing — the FTS5 table and its triggers
    # may be missing when a migration was interrupted (user_version stamped
    # but files_fts never created).  rebuild_files_fts is idempotent and
    # fast when the table already exists.
    if not _table_exists(conn, "files_fts"):
        log.info("files_fts missing — rebuilding (self-healing)")
        rebuild_files_fts(conn)

    # Unconditional macros_fts self-healing — same pattern.
    if not _table_exists(conn, "macros_fts"):
        log.info("macros_fts missing — rebuilding (self-healing)")
        rebuild_macros_fts(conn)

    # Integrity check — detect corruption early, before any tool uses the DB.
    # Skip for auxiliary connections (worker threads, pooled connections)
    # where the check already ran on the primary connection.
    #
    # GOTCHA — on 5+ GB databases a full scan takes 15–30 s and saturates
    # disk I/O.  Never call this during MCP server startup (even in a
    # background thread — the I/O contention delays first queries).
    # server.py pre-marks _integrity_checked to skip it; the DB was already
    # verified during ``fw-context index``.  See server.py:324-328.
    if not skip_integrity_check:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                details = result[0]
                conn.close()
                raise DatabaseCorruptionError(str(path), details)
        except sqlite3.DatabaseError as e:
            conn.close()
            raise DatabaseCorruptionError(str(path), str(e)) from e

    return conn


def get_db_schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stored in the database (``PRAGMA user_version``).

    Compare with ``CURRENT_SCHEMA_VERSION`` — if lower, the index was built
    with an older schema and may miss data populated by newer migrations.
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def transaction(conn: sqlite3.Connection, checkpoint: bool = True) -> Generator[sqlite3.Connection, None, None]:
    """Commit-or-rollback context manager.

    By default a WAL truncate-checkpoint runs after each successful commit to
    keep the -wal file small. Pass ``checkpoint=False`` inside tight per-item
    loops (e.g. the indexer's per-TU commits) and run a single checkpoint once
    the loop finishes — per-commit checkpoints there are O(n) and dominate
    indexing time.

    The checkpoint is best-effort: if it fails (e.g. another connection holds
    a read transaction), the commit is NOT rolled back — data is already safe.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if checkpoint:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass  # best-effort — data was already committed
