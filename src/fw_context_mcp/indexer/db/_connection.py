"""SQLite connection management for fw-context-mcp index.

Connection lifecycle
────────────────────
Every connection goes through ``open_db`` → ``_configure_connection`` →
``ensure_schema`` → integrity check.  The pipeline is linear by design:

  1. **open_db** — creates parent directory, opens the file, sets
     ``row_factory`` and initial ``busy_timeout``, puts the DB in WAL
     mode, then delegates to ``_configure_connection``.
  2. **``_configure_connection``** — applies the full PRAGMA set
     (cache_size, mmap_size, synchronous, foreign_keys) and loads the
     ``sqlite-vec`` extension.  Called separately from ``open_db`` so
     auxiliary connections (worker threads) can re-use the same
     configuration without repeating the directory-creation and chmod
     steps.
  3. **ensure_schema** — idempotent schema migration and self-healing.
     The version-gate skips the expensive migration block when the
     on-disk schema is current.
  4. **Integrity check** — ``PRAGMA integrity_check``, skipped on
     auxiliary connections or when pre-marked as already verified.

WAL mode
────────
Write-Ahead Logging is enabled with ``PRAGMA journal_mode=WAL``.
Key properties for fw-context-mcp:

  • **Concurrent reads + writes** — the MCP server holds a read
    connection open for client queries while the background indexer
    writes through a separate connection.  WAL mode allows this
    without SQLITE_BUSY on reads.
  • **Persistent** — WAL mode is a persistent DB-file property.  Once
    set, all subsequent connections (including from other processes)
    use WAL automatically.  The initial PRAGMA is still required on
    first-open; it is wrapped in a try/except to convert to
    DatabaseCorruptionError when the DB is unreadable.
  • **WAL size management** — ``cache_size = -64000`` (64 MB) and
    ``mmap_size = 268435456`` (256 MB) reduce the frequency of WAL
    page evictions.  The ``transaction`` context manager runs
    ``PRAGMA wal_checkpoint(TRUNCATE)`` after each successful commit
    to keep the WAL file small.

Locking strategy
────────────────
The database layer uses three separate mechanisms:

  • **busy_timeout** — 10 s by default (CLI indexer: fail fast on
    collision).  The MCP server's executor overrides this on its
    dedicated connection with 120 s.  This is NOT a lock — it is a
    wait-before-failing timeout when another connection holds a
    write lock.
  • **``write_lock`` (``_locking.py``)** — a file-system lock
    (``DB_LOCK`` file in the database directory).  Acquired before
    any write batch (indexer per-TU commit, purge operations).  This
    coordinates multiple processes (indexer + MCP server) writing to
    the same database.
  • **``transaction`` context manager** — standard SQLite BEGIN/COMMIT/
    ROLLBACK.  Always used together with ``write_lock`` for writes;
    NOT used for read-only queries (readers hold no transaction that
    could block writers in WAL mode).

Schema versioning
─────────────────
The schema version is stored in ``PRAGMA user_version`` and compared
against ``CURRENT_SCHEMA_VERSION``.  When the on-disk version is older,
``ensure_schema`` runs the full migration pipeline.  When the on-disk
version is NEWER (tool downgrade scenario), it is re-stamped to current
with a warning — the expectation is that the developer downgraded and
the database should match the current code's expectations.
"""

import logging
import sqlite3
import time as _time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "DatabaseCorruptionError",
    "ensure_schema",
    "get_db_schema_version",
    "open_db",
    "transaction",
]

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

    Set ``locked=True`` when the failure is a lock-contention exhaustion,
    not actual data corruption.  Callers use this to decide whether to
    suggest ``reset_index`` (corruption) or a retry (locking).

    The caller should present the error to the user with a clear action:
    run ``reset_index()`` then ``fw-context index`` to rebuild.
    """

    def __init__(self, db_path: str, details: str = "", locked: bool = False):
        self.db_path = db_path
        self.details = details
        self.locked = locked
        super().__init__(f"Database corruption detected at {db_path}: {details}")


# ── Removed: progress-handler query timeout — DO NOT RESTORE ─────────────
#
# This module used to install a SQLite progress handler on every
# connection (10 s per-query wall-clock limit, interrupting via
# set_progress_handler).  It was a workaround for the old connection-pool
# architecture in the MCP server, where several connections competed for
# one database file and a runaway query had to be killed defensively.
#
# Why it was removed: the handler killed ANY query after 10 s, including
# legitimate BFS call-graph traversals that need 15-25 s on large
# databases (350K+ references) — under parallel load every such query
# timed out.  Timeout enforcement moved to the MCP layer: the server's
# _wrap_tool applies a 300 s limit and cancels the running query via
# SyncQueryExecutor.interrupt() (sqlite3_interrupt).  The CLI indexer
# runs queries sequentially and needs no per-query timeout at all.
#
# DO NOT RESTORE a blanket per-query timeout here — it is the wrong
# layer (it cannot distinguish a locked query from a legitimately long
# one) and it breaks heavy read queries on large indexes.


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply standard PRAGMAs and extensions to a freshly opened connection.

    PRAGMA rationale
    ────────────────
    • ``busy_timeout = 10000`` (10 s) — CLI indexer must fail fast when
      colliding with a running MCP server.  The server's executor sets
      its own 120 s busy_timeout on its dedicated connection.
    • ``foreign_keys = ON`` — enforced at connection level because SQLite
      defaults to OFF.  All DELETE CASCADE and FK constraints in the
      schema are silent no-ops without this.
    • ``journal_mode = WAL`` — enables concurrent readers while a
      writer holds the write lock.  Without WAL, any read query during
      an index run would get SQLITE_BUSY.
    • ``cache_size = -64000`` — 64 MB page cache (negative = kibibytes).
      Large enough to hold the index's hot working set (symbols FTS5,
      symbols, references tables) in memory.  Reduces disk I/O during
      search queries and BFS traversals.
    • ``mmap_size = 268435456`` — 256 MB memory-mapped I/O.  On Linux
      this lets the kernel page-cache the database file directly,
      eliminating read() syscalls for cached pages.  The index can
      grow to 5+ GB; mmap handles large databases efficiently.
    • ``synchronous = 1`` (NORMAL) — balances durability with write
      throughput.  FULL (2) would add fsync after every write, halving
      indexing speed.  OFF (0) risks corruption on power loss.  NORMAL
      is safe because the WAL journal protects committed data.
    """
    # 10 s busy_timeout is deliberate: the CLI indexer must fail fast when
    # it collides with a running MCP server.  The server's executor sets
    # its own 120 s busy_timeout on its dedicated connection — do NOT
    # raise this global value.
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
        log.debug("sqlite-vec loaded successfully")
    except ImportError:
        log.debug("sqlite-vec not installed — semantic search will use legacy BLOB fallback")
    except sqlite3.OperationalError as e:
        log.debug("sqlite-vec extension failed to load (%s) — "
                  "semantic search KNN unavailable, will use BLOB fallback", e)
    except (sqlite3.Error, RuntimeError, OSError) as e:
        log.warning("sqlite-vec load error: %s", e)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Run schema creation, migrations, and self-healing on an open connection.

    Idempotent — safe to call on every connection open.  The version-gate
    skips the expensive migration block when the on-disk schema is current.

    Raises the original exception on failure — the caller (``open_db``) is
    responsible for retry logic and error conversion.
    """
    # Only run the (expensive) schema/migration block when the on-disk schema
    # is outdated.  executescript() implies a write transaction — skipping it
    # when the schema is current means read-only queries never acquire a
    # write lock, even while a background reindex is writing.
    current_schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]

    if current_schema_ver < CURRENT_SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        _ensure_migrated_columns(conn)
        _run_data_migrations(conn)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    elif current_schema_ver > CURRENT_SCHEMA_VERSION:
        log.warning("Schema version %d > current %d — re-stamping.", current_schema_ver, CURRENT_SCHEMA_VERSION)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

    # ── Unconditional column sanity check ──────────────────────────────────
    # Belt-and-suspenders: ensure all columns from _MIGRATION_ADD_COLUMNS
    # exist, even when the version gate skipped the main migration block.
    # Idempotent — on a fully migrated DB this is a single empty commit.
    _ensure_migrated_columns(conn)

    # ── Unconditional embeddings schema check ────────────────────────────
    # The embeddings composite-PK migration lives in _run_data_migrations
    # (version-gated), but databases that had their schema version bumped
    # before the migration code was added will skip it.  This unconditional
    # call patches those databases without requiring another version bump.
    from ._schema import _ensure_embeddings_schema
    _ensure_embeddings_schema(conn)

    # ── Defensive table creation ──────────────────────────────────────────
    # Self-healing: ensure critical tables exist even when a migration was
    # interrupted.  CREATE TABLE IF NOT EXISTS is idempotent and takes
    # microseconds when the table already exists.  Table definitions live
    # in _schema._CRITICAL_TABLES — single source of truth.
    from ._schema import _CRITICAL_TABLES

    conn.executescript(_CRITICAL_TABLES)

    # Lazy imports — these symbols are defined in __init__.py (sibling module).
    # Deferred to avoid circular import: __init__.py imports from _connection.py.
    from fw_context_mcp.indexer.db import (  # noqa: F811
        init_vec_table,
        rebuild_files_fts,
        rebuild_fts,
        rebuild_macros_fts,
    )

    # Migrate vec0 table when sqlite-vec is available (idempotent CREATE IF NOT EXISTS)
    try:
        init_vec_table(conn)
    except (sqlite3.Error, RuntimeError) as e:
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

    # Unconditional symbols_fts self-healing — same pattern as files_fts
    # and macros_fts above.  On a current-schema DB with a missing or
    # corrupt symbols_fts (e.g. interrupted migration that stamped
    # user_version), this recreates it so search_code/search_bodies
    # don't raise "no such table: symbols_fts".
    if not _table_exists(conn, "symbols_fts"):
        log.info("symbols_fts missing — rebuilding (self-healing)")
        rebuild_fts(conn)


def open_db(path: Path, *, skip_integrity_check: bool = False, check_same_thread: bool = True, _retries: int = 0) -> sqlite3.Connection:
    """Open SQLite database at *path*, enabling WAL mode and loading extensions.

    Creates the parent directory if missing.  Configures WAL journal mode,
    foreign keys, and a 30 s busy timeout.  Loads the ``sqlite-vec`` extension
    (best-effort — silently skipped when unavailable).

    Calls :func:`ensure_schema` to run the full schema and migrations in sequence:

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
    # Ensure DB file is only readable by the owner (defense in depth).
    try:
        path.chmod(0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    # 10 s busy_timeout is deliberate (fail-fast for the CLI indexer —
    # see _configure_connection).  The MCP server's executor overrides
    # this on its own connection with 120 s.
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

    # Schema + migrations — with retry on lock contention.
    try:
        ensure_schema(conn)
    except sqlite3.OperationalError as e:
        # SQLITE_BUSY (5) = database is locked (another process has a
        # write lock).  SQLITE_LOCKED (6) = a table within the database
        # is locked (another process is modifying it right now).
        #
        # Retry up to 3 times with a 1 s sleep between attempts.  This
        # handles transient lock contention when the MCP server's executor
        # is holding a write lock for a reindex_file call or when the
        # background indexer is mid-commit.
        #
        # After 3 failures, raise DatabaseCorruptionError with
        # locked=True — the caller can distinguish "the DB is corrupt"
        # from "the DB is busy" and suggest a different recovery action.
        if getattr(e, "sqlite_errorcode", 0) in (5, 6):
            if _retries >= 3:
                conn.close()
                raise DatabaseCorruptionError(
                    str(path),
                    f"Database locked after {_retries + 1} attempts — "
                    "another process may hold an exclusive lock. "
                    "Stop any running fw-context index process and retry.",
                    locked=True,
                ) from e
            conn.close()
            _time.sleep(1)
            return open_db(path, skip_integrity_check=skip_integrity_check,
                           check_same_thread=check_same_thread, _retries=_retries + 1)
        elif "no such column" in str(e):
            _ensure_migrated_columns(conn)
            try:
                conn.executescript(_SCHEMA)
                _run_data_migrations(conn)
                conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                # Re-run the self-healing parts of ensure_schema
                ensure_schema(conn)
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

    # No per-query progress-handler timeout is installed here — see the
    # "Removed: progress-handler query timeout" note at the top of this
    # module.  Query timeout enforcement lives in the MCP layer
    # (_wrap_tool 300 s + SyncQueryExecutor.interrupt()).
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

    Checkpoint frequency trade-off
    ──────────────────────────────
    Running wal_checkpoint(TRUNCATE) after every commit keeps the WAL file
    near zero bytes — good for disk usage but expensive under high write
    throughput (the indexer commits ~200-500 transactions during a full
    build re-index).  Each checkpoint is an fsync of the entire page cache.

    Running a single checkpoint at the end of the loop (``checkpoint=False``
    on each commit, then one manual checkpoint after the loop) reduces I/O
    by 200-500× during indexing.  The WAL file grows temporarily (up to
    ~100 MB on large projects) but is truncated at the end.
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
        except sqlite3.Error:
            pass  # best-effort — data was already committed
