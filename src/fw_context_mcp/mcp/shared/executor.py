"""Single-connection sync query executor for MCP handlers.

Design rationale (self-contained — no external documents needed):

- ONE SQLite connection per database, serialized by ``threading.Lock``.
  The previous design pooled 1-4 connections per database.  Multiple
  connections on a single SQLite file do not provide real parallelism:
  the SQLite-internal mutex plus shared disk I/O serialize them anyway,
  and under parallel MCP load the pool produced I/O contention that made
  every query slower (observed: all BFS call-graph queries timing out on
  a 350K-reference database when tests ran in parallel).

- Handlers stay SYNCHRONOUS and run in ``asyncio.to_thread`` workers.
  An async conversion was rejected because the async branch of
  ``_wrap_tool`` has no ``ctx`` parameter and therefore cannot emit MCP
  progress notifications — some MCP clients kill the whole connection
  after ~15 s without progress.  Sync handlers keep the 5 s progress
  loop working unchanged.

- NO per-query timeout parameter anywhere in this module.  A timeout
  number that nothing enforces is a misleading API.  Timeout enforcement
  lives in exactly one place: ``_wrap_tool``'s 300 s limit plus
  ``interrupt_all()`` calling ``sqlite3_interrupt()``.

- The connection is WARM for the process lifetime — the expensive parts
  of ``open_db`` (ensure_schema write transaction, integrity_check scan,
  extension loading) are paid once at executor creation, not per request.
  Per-request cost is: one lock acquire, one ``st_mtime`` stat (reindex
  detection) and at most one ``SELECT 1`` health check per 60 s.

- ``config_hash`` is NEVER stored on the executor.  It is re-read per
  request (via a short-lived read-only connection in
  ``connection._resolve_handler_context``) and passed per call to
  ``execute_sync``.  If it were stored, a reindex with a changed build
  config would leave the executor filtering by a stale hash forever —
  silently empty result sets with no error.

External users must import via ``.context`` (re-export layer), never
from this module directly.
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Interval between lightweight connection health checks (seconds).
_HEALTH_INTERVAL_S = 60.0


class SyncQueryExecutor:
    """One SQLite connection + one lock for a single index database.

    All database access from MCP handlers for a given ``db_path`` goes
    through the executor's single connection, serialized by
    ``threading.Lock``.  Callers run in ``asyncio.to_thread`` workers;
    the lock is the ONLY serialization mechanism — there is no queue,
    no async bridge.

    Thread-safety: all public methods are safe to call from any thread.
    ``interrupt()`` is designed to be called from the asyncio event-loop
    thread while a worker thread holds the lock.
    """

    def __init__(self, db_key: str, db_path: Path) -> None:
        self._db_key = db_key
        self._db_path = db_path
        self._lock = threading.Lock()
        self._last_health = 0.0
        self._health_interval = _HEALTH_INTERVAL_S
        self._reindex_pid = db_path.parent / "reindex.pid"
        # State for reindex transition detection — see _check_reindex.
        self._reindex_was_running = False
        # (st_ino, st_mtime) of the DB file at last open — see _check_reindex.
        self._db_identity: tuple[int, float] | None = None
        self._conn = self._open_connection()
        self._last_health = time.monotonic()

    # ── Connection lifecycle ─────────────────────────────────────────

    def _open_connection(self) -> sqlite3.Connection:
        """Open and configure the executor connection.

        Deliberately differs from ``indexer.db.open_db``:

        - NO ``ensure_schema``: its unconditional ``executescript`` is a
          write transaction — paying it per open caused lock contention
          on what should be read-only request handling.
        - NO ``integrity_check``: done once at server startup
          (``server.main`` pre-marks ``_integrity_checked``); the scan
          takes 15-30 s on multi-GB databases.
        - NO progress-handler query timeout: timeout enforcement is
          ``_wrap_tool`` 300 s + ``interrupt()``.  The old 10 s progress
          handler killed legitimate BFS queries that need 15-25 s on
          large databases.
        - ``busy_timeout = 120 s`` HERE ONLY.  The CLI indexer keeps its
          10 s fail-fast in ``indexer/db/_connection.py`` — a collision
          between indexer and server must fail the indexer quickly, not
          the server.  Do NOT change the CLI value.
        - WAL pragma in try/except: setting journal_mode is a write op
          and can block behind an active reindex; the DB is already WAL
          from indexing, so failure here is harmless.
        """
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,  # handler threads share this conn under the lock
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 120000")  # 120 s — executor only, see docstring
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass  # already WAL, or a reindex holds the write lock — harmless
        conn.execute("PRAGMA cache_size = -64000")      # 64 MB page cache
        conn.execute("PRAGMA mmap_size = 268435456")    # 256 MB mmap window
        conn.execute("PRAGMA synchronous = 1")          # NORMAL — safe under WAL
        conn.execute("PRAGMA foreign_keys = ON")

        # sqlite-vec for semantic search (best effort — callers fall back
        # to the BLOB path when the extension is unavailable).
        try:
            conn.enable_load_extension(True)
            import sqlite_vec

            sqlite_vec.load(conn)
        except ImportError:
            log.debug("sqlite-vec not installed — semantic search will use legacy BLOB fallback")
        except sqlite3.OperationalError as e:
            log.debug("sqlite-vec extension failed to load (%s) — KNN unavailable, BLOB fallback", e)
        except (sqlite3.Error, RuntimeError, OSError) as e:
            log.warning("sqlite-vec load error: %s", e)

        # Record file identity on EVERY open, including the first.
        # Without the first-open record, _check_reindex would skip identity
        # detection while _db_identity is None, and a manual
        # 'fw-context index' run (which leaves no reindex.pid file) would
        # go undetected until some unrelated reconnect happened.
        try:
            st = self._db_path.stat()
            self._db_identity = (st.st_ino, st.st_mtime)
        except OSError:
            self._db_identity = None

        return conn

    def _reconnect(self) -> None:
        """Close the current connection and open a fresh one.

        Called on: fatal connection error, completed background reindex,
        or a changed DB file identity.  ``_open_connection`` records the
        new ``(st_ino, st_mtime)`` identity, so detection state stays
        consistent across reconnects.
        """
        try:
            self._conn.close()
        except sqlite3.Error:
            pass  # closing a broken connection may itself fail — ignore
        self._conn = self._open_connection()
        self._last_health = time.monotonic()

    def _health_check(self) -> None:
        """Lightweight connection health — one ``SELECT 1`` every 60 s.

        Catches dead connections (e.g. after the DB file was replaced
        underneath us) without per-query overhead.  60 s is a trade-off:
        frequent enough to detect breakage early, rare enough to be free.
        """
        now = time.monotonic()
        if now - self._last_health < self._health_interval:
            return
        try:
            self._conn.execute("SELECT 1")
            self._last_health = now
        except sqlite3.Error:
            self._reconnect()

    def _check_reindex(self) -> None:
        """Detect a completed reindex and reconnect to the new database.

        Two independent triggers, either one reconnects:

        1. TRANSITION: ``reindex.pid`` existed at the last check and is
           gone now.  The pid file exists ONLY while a reindex runs —
           testing "pid does not exist" alone would reconnect on every
           single query, because the file never exists in normal
           operation.  The correct test is the existed→vanished edge.
        2. IDENTITY CHANGE: ``(st_ino, st_mtime)`` of the DB file differs
           from the values recorded at open.  Covers DB replacement by a
           manual ``fw-context index`` run, which writes no pid file.
        """
        pid_exists = self._reindex_pid.exists()
        if self._reindex_was_running and not pid_exists:
            self._reindex_was_running = False
            self._reconnect()
            return
        self._reindex_was_running = pid_exists

        try:
            st = self._db_path.stat()
            identity = (st.st_ino, st.st_mtime)
        except OSError:
            # DB file missing (e.g. reset_index just deleted it) — the
            # health check / fatal-error path handles recovery.
            return
        if self._db_identity is not None and identity != self._db_identity:
            self._reconnect()
            return
        # Identity is recorded by _open_connection on every open,
        # including the first — nothing to do here.

    # ── Query execution ──────────────────────────────────────────────

    def execute_sync(
        self,
        query_fn: Callable[..., Any],
        config_hash: str,
        *args: Any,
    ) -> Any:
        """Run ``query_fn(conn, config_hash, *args)`` under the executor lock.

        Blocks the calling thread until the lock is free and the query
        completes.  The caller runs in an ``asyncio.to_thread`` worker;
        the lock is the only serialization — one query at a time on this
        connection.

        There is deliberately NO ``timeout`` parameter: an advisory
        number nothing enforces is a misleading API.  Timeout enforcement
        is ``_wrap_tool``'s 300 s limit plus ``interrupt()``.

        ``config_hash`` is passed PER CALL and never stored on the
        executor: after a reindex with a changed build config, a stored
        hash would filter every query by the old config — silently empty
        results forever.

        Retry semantics: on a FATAL connection error (see ``_is_fatal``)
        the executor reconnects and retries the query exactly once.  This
        is safe only because ``OperationalError('interrupted')`` is not
        fatal — retrying an interrupted query would re-run the very query
        the 300 s timeout just cancelled and block the lock for another
        full timeout window.  Callers must keep ``query_fn`` idempotent;
        the single write path (reindex_file_impl) re-applies the same
        per-file parse, which is idempotent by construction.

        Raises ``sqlite3.Error`` on database failure (after the single
        retry for fatal errors).
        """
        with self._lock:
            self._check_reindex()
            self._health_check()
            try:
                return query_fn(self._conn, config_hash, *args)
            except sqlite3.Error as exc:
                if self._is_fatal(exc):
                    self._reconnect()
                    return query_fn(self._conn, config_hash, *args)
                raise

    # ── Cancellation ─────────────────────────────────────────────────

    def interrupt(self) -> None:
        """Interrupt the currently running query via ``sqlite3_interrupt()``.

        Called from ``_wrap_tool`` (event-loop thread) when the 300 s
        timeout fires.  ``sqlite3.Connection.interrupt()`` is documented
        thread-safe — safe to call while a worker thread runs a query.
        ``task.cancel()`` alone does NOT kill an ``asyncio.to_thread``
        worker thread; without interrupt the cancelled query would keep
        running as a zombie thread holding the executor lock.  The
        interrupted query raises ``OperationalError('interrupted')``.

        KNOWN LIMITATION: ``sqlite3_interrupt()`` is per-connection, not
        per-thread.  When call A times out while its query is queued
        behind call B's running query on this executor, ``interrupt()``
        kills B's query — B's call then fails with
        ``OperationalError('interrupted')``.  Accepted: cross-kill can
        only happen after a 300 s timeout (pathological state anyway)
        and the victim's error is explicit, not silent.
        """
        if self._conn is not None:
            self._conn.interrupt()

    def shutdown(self) -> None:
        """Close the connection.  Idempotent."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── Error classification ─────────────────────────────────────────

    @staticmethod
    def _is_fatal(exc: Exception) -> bool:
        """Distinguish connection errors from query errors.

        Query errors (missing table, syntax error) must NOT trigger a
        reconnect — they are caller bugs, not connection problems, and
        retrying them would just fail twice.

        ``OperationalError('interrupted')`` is deliberately NOT fatal:
        it means ``_wrap_tool``'s 300 s timeout fired ``interrupt()`` —
        retrying would re-run the just-cancelled query and block the
        lock for another full timeout window.  Do NOT add "interrupted"
        here; let it propagate to the caller.
        """
        if not isinstance(exc, sqlite3.Error):
            return False
        msg = str(exc).lower()
        return any(
            kw in msg
            for kw in (
                "database disk image is malformed",
                "disk i/o error",
                "unable to open database file",
                "database is locked",  # busy_timeout exhausted
            )
        )


# ── Executor registry ─────────────────────────────────────────────────────

_executors: dict[str, SyncQueryExecutor] = {}
_registry_lock = threading.Lock()


def get_executor(db_path: Path) -> SyncQueryExecutor:
    """Get or create the ``SyncQueryExecutor`` for *db_path*.

    Singleton per resolved db_path, living for the process lifetime.
    Explicitly invalidated by ``invalidate_executor()`` on reindex and by
    ``reset_index``.  The registry lock makes creation thread-safe.
    ``config_hash`` is NOT part of the executor identity — it is passed
    per call to ``execute_sync``.
    """
    db_key = str(db_path.resolve())
    with _registry_lock:
        ex = _executors.get(db_key)
        if ex is not None:
            return ex
        ex = SyncQueryExecutor(db_key, db_path)
        _executors[db_key] = ex
        return ex


def invalidate_executor(db_key: str | None = None) -> None:
    """Shutdown and remove the executor for *db_key*, or all if None.

    ``reset_index`` MUST call this before deleting the DB file —
    otherwise the executor keeps a connection to an unlinked file and
    every subsequent query reads stale data with no error.
    """
    with _registry_lock:
        if db_key is None:
            for ex in _executors.values():
                ex.shutdown()
            _executors.clear()
        else:
            existing = _executors.pop(db_key) if db_key in _executors else None
            if existing is not None:
                existing.shutdown()


def interrupt_all() -> None:
    """Interrupt the running query on EVERY executor.

    Public API for ``server.py``'s ``_wrap_tool`` timeout path — server
    code must not reach into the private ``_executors`` registry (layer
    rule).  Note this also interrupts queries on unrelated databases
    (per-connection interrupt cannot be targeted) — acceptable only in
    the pathological 300 s timeout path, where the alternative is a
    zombie thread holding the lock forever.
    """
    with _registry_lock:
        executors = list(_executors.values())
    for ex in executors:
        ex.interrupt()


def shutdown_all() -> None:
    """Close all executors.  Registered via ``atexit``."""
    invalidate_executor(None)


atexit.register(shutdown_all)
