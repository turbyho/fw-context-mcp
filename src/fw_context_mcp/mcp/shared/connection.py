"""Database connection caching for MCP handlers.

Extracted from ``context.py`` — manages cached SQLite connections
with TTL-based eviction, integrity checking, and stale detection.

External users should continue importing from ``.context``
which re-exports everything from this module.
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fw_context_mcp import config
from fw_context_mcp.indexer.db import DatabaseCorruptionError, open_db

log = logging.getLogger(__name__)

# ── Connection cache ────────────────────────────────────────────────────


@dataclass
class _ConnCacheEntry:
    conn: sqlite3.Connection
    opened_at: float


_conn_cache: dict[str, _ConnCacheEntry] = {}
_conn_cache_lock = threading.Lock()
_CONN_TTL = 60  # seconds

# ── Integrity check cache ───────────────────────────────────────────────

_integrity_checked: set[str] = set()
_integrity_lock = threading.Lock()


def _invalidate_conn_cache(db_key: str | None = None) -> None:
    """Evict cached connections.  Pass ``None`` to clear all."""
    with _conn_cache_lock:
        if db_key is None:
            for entry in _conn_cache.values():
                try:
                    entry.conn.close()
                except sqlite3.Error:
                    pass
            _conn_cache.clear()
            with _integrity_lock:
                _integrity_checked.clear()
        elif db_key in _conn_cache:
            try:
                _conn_cache[db_key].conn.close()
            except sqlite3.Error:
                pass
            del _conn_cache[db_key]
            with _integrity_lock:
                _integrity_checked.discard(db_key)


def _invalidate_conn_cache_if_reindex_done(
    db_key: str, reindex_pid_file: Path
) -> None:
    """Evict cached connection when a background reindex has completed."""
    if db_key not in _conn_cache:
        return
    if not reindex_pid_file.exists():
        _invalidate_conn_cache(db_key)


def _evict_stale_entry(db_key: str) -> None:
    """Close and evict a cached connection if its TTL has expired."""
    with _conn_cache_lock:
        entry = _conn_cache.get(db_key)
        if entry is None:
            return
        if time.monotonic() - entry.opened_at > _CONN_TTL:
            try:
                entry.conn.close()
            except sqlite3.Error:
                pass
            del _conn_cache[db_key]
            with _integrity_lock:
                _integrity_checked.discard(db_key)


def _check_cached_conn(db_key: str) -> sqlite3.Connection | None:
    """Return a live cached connection, or None if dead/expired."""
    with _conn_cache_lock:
        entry = _conn_cache.get(db_key)
        if entry is None:
            return None
    try:
        entry.conn.execute("SELECT 1")
        entry.opened_at = time.monotonic()  # bump TTL on each successful use
        return entry.conn
    except sqlite3.Error:
        _invalidate_conn_cache(db_key)
        return None


def _open_and_cache(db_key: str, db_path: Path) -> sqlite3.Connection:
    """Open a database connection and cache it.

    Double-checked locking with IO outside the lock: ``open_db()``
    (which includes schema migrations and integrity checks) runs
    before acquiring ``_conn_cache_lock`` so IO-intensive work does
    not block other threads from accessing the cache.
    """
    # Fast path: connection already cached — return immediately
    with _conn_cache_lock:
        if db_key in _conn_cache:
            return _conn_cache[db_key].conn

    # Open the database OUTSIDE the cache lock — IO-intensive work
    # (schema migrations, integrity checks) should not block other threads
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = open_db(db_path.resolve())
    except DatabaseCorruptionError:
        if conn:
            conn.close()
        raise

    # Insert into cache under lock — double-check another thread didn't win
    should_check = False
    with _conn_cache_lock:
        if db_key in _conn_cache:
            # Another thread already cached a connection — close our duplicate
            try:
                conn.close()
            except sqlite3.Error:
                pass
            return _conn_cache[db_key].conn

        with _integrity_lock:
            if db_key not in _integrity_checked:
                _integrity_checked.add(db_key)
                should_check = True

        _conn_cache[db_key] = _ConnCacheEntry(conn=conn, opened_at=time.monotonic())

    # Integrity check outside the cache lock so concurrent tool calls are not blocked
    if should_check:
        # Fast check first — PRAGMA quick_check is O(1), catches most corruption
        try:
            quick_result = conn.execute("PRAGMA quick_check").fetchone()
            if quick_result and quick_result[0] != "ok":
                log.warning("quick_check failed: %s — database may be corrupt", quick_result[0])
                # Fall through to full integrity_check for details
        except sqlite3.Error:
            pass

        # Full PRAGMA integrity_check runs once per process per DB.
        # For large databases (>100 MB) this can take seconds, but it's the
        # only way to catch subtle index corruption that quick_check misses.
        # The result is cached so subsequent tool calls skip this entirely.
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                log.warning("integrity_check: %s", result[0])
        except sqlite3.Error:
            pass

    return conn


def _open_db_safe(db_path: Path) -> tuple[sqlite3.Connection | None, dict | None]:
    """Public entry point: evict stale, check cached, check reindex, open.

    Returns ``(conn, None)`` on success or ``(None, error_dict)`` when the
    database is corrupt or unreachable.

    Re-exported via ``context.py`` — the canonical import for all MCP handlers.
    """
    db_key = str(db_path.resolve())
    _evict_stale_entry(db_key)
    cached = _check_cached_conn(db_key)
    if cached is not None:
        return cached, None

    reindex_pid = db_path.parent / "reindex.pid"
    _invalidate_conn_cache_if_reindex_done(db_key, reindex_pid)

    try:
        return _open_and_cache(db_key, db_path), None
    except DatabaseCorruptionError as e:
        _integrity_checked.discard(db_key)
        return None, {
            "error": f"Database corruption detected: {e}",
            "action": "reset_index",
            "hint": "Run reset_index() then fw-context index to rebuild.",
        }


def _open_db_or_return(db_path: Path) -> tuple[sqlite3.Connection | None, list[dict] | None]:
    """Open DB via ``_open_db_safe`` and return ``(conn, None)`` or ``(None, error_result)``.

    Unlike ``_open_db_safe``, this never returns ``(None, None)`` — the
    error result is always a ready-to-return list of error dicts.
    Handlers can use this instead of the ``assert conn is not None`` pattern.
    """
    conn, err = _open_db_safe(db_path)
    if err:
        return None, [err]
    if conn is None:
        return None, [{"error": "Database connection failed. Try reset_index() then fw-context index."}]
    return conn, None


# ── HandlerContext — shared setup for all MCP handlers ────────────────

@dataclass
class HandlerContext:
    """Pre-resolved context for an MCP handler invocation.

    All fields are guaranteed non-None — callers that receive a
    ``HandlerContext`` can access every field without further checks.
    """

    conn: sqlite3.Connection
    config_hash: str
    root: Path
    cfg: "Config"
    project_id: str
    db_path: Path


def _resolve_handler_context(
    project_root: str | None,
    *,
    require_refs: bool = False,
) -> tuple[HandlerContext | None, list[dict] | None]:
    """One-call setup for MCP handlers: resolve project, open DB, load config.

    Returns ``(ctx, None)`` on success or ``(None, [error_dict])`` on failure.
    The returned ``HandlerContext.conn`` is cache-managed — callers must NOT
    close it.

    When *require_refs* is ``True``, the call also verifies that the reference
    index is populated (returns an ``info`` dict when empty).
    """
    from .readiness import _db_path, resolve_project_root

    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return None, [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    conn, err_result = _open_db_or_return(db_path)
    if err_result:
        return None, err_result

    cfg = config.load(root)
    from fw_context_mcp.config import derive_project_id

    project_id = derive_project_id(root)

    from fw_context_mcp.indexer.db import get_active_config

    with conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return None, [{"error": "No build config indexed."}]
        config_hash = cfg_data["config_hash"]

        if require_refs:
            from fw_context_mcp.indexer.db import count_refs

            if count_refs(conn, config_hash) == 0:
                return None, [{"info": (
                    "No references indexed. Refs are on by default — "
                    "they may have been disabled with [index] index_refs = false. "
                    "Re-run 'fw-context index' to rebuild."
                )}]

    return HandlerContext(
        conn=conn,
        config_hash=config_hash,
        root=root,
        cfg=cfg,
        project_id=project_id,
        db_path=db_path,
    ), None




def _cleanup_conn_cache_atexit() -> None:
    """Close all cached connections on process exit."""
    _invalidate_conn_cache(None)


atexit.register(_cleanup_conn_cache_atexit)
