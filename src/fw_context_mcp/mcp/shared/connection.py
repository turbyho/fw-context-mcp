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
                except Exception:
                    pass
            _conn_cache.clear()
            with _integrity_lock:
                _integrity_checked.clear()
        elif db_key in _conn_cache:
            try:
                _conn_cache[db_key].conn.close()
            except Exception:
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
            except Exception:
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

    Double-checked locking: re-checks the cache after acquiring the lock.
    Runs ``PRAGMA integrity_check`` once per database path per process
    (outside the cache lock so other tools are not blocked).
    Note: calls open_db() while holding _conn_cache_lock -- IO under
    lock is acceptable for read-heavy workloads (open <1ms on modern FS).
    """
    should_check = False
    with _conn_cache_lock:
        if db_key in _conn_cache:
            return _conn_cache[db_key].conn  # race: another thread cached first

        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = open_db(db_path.resolve())
        except DatabaseCorruptionError:
            if conn:
                conn.close()
            raise

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


def _cleanup_conn_cache_atexit() -> None:
    """Close all cached connections on process exit."""
    _invalidate_conn_cache(None)


atexit.register(_cleanup_conn_cache_atexit)
