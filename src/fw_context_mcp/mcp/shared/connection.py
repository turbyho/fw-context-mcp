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
from typing import TYPE_CHECKING

from fw_context_mcp import config
from fw_context_mcp.indexer.db import DatabaseCorruptionError, open_db

if TYPE_CHECKING:
    from fw_context_mcp.config.settings import Config

log = logging.getLogger(__name__)

# ── Connection pool ─────────────────────────────────────────────────────
#
# Each db_key gets a pool of N read-only connections (N=4) for concurrent
# request handling.  Pool connections beyond the first skip integrity
# checks — the primary connection already verified the DB.  WAL mode
# allows multiple concurrent readers without blocking.
#
# Round-robin allocation ensures requests are distributed evenly across
# pool connections, preventing a single slow query from starving others.


@dataclass
class _ConnCacheEntry:
    conn: sqlite3.Connection
    opened_at: float


_conn_pool: dict[str, list[_ConnCacheEntry]] = {}
_conn_pool_lock = threading.Lock()
_pool_index: dict[str, int] = {}  # round-robin cursor per db_key
_POOL_SIZE = 4
_CONN_TTL = 300  # seconds — pool lives for the server lifetime (was 60)

# ── Integrity check cache ───────────────────────────────────────────────

_integrity_checked: set[str] = set()
_integrity_lock = threading.Lock()


def _invalidate_conn_cache(db_key: str | None = None) -> None:
    """Evict cached connections.  Pass ``None`` to clear all."""
    with _conn_pool_lock:
        if db_key is None:
            for pool in _conn_pool.values():
                for entry in pool:
                    try:
                        entry.conn.close()
                    except sqlite3.Error:
                        pass
            _conn_pool.clear()
            _pool_index.clear()
            with _integrity_lock:
                _integrity_checked.clear()
        elif db_key in _conn_pool:
            for entry in _conn_pool[db_key]:
                try:
                    entry.conn.close()
                except sqlite3.Error:
                    pass
            del _conn_pool[db_key]
            _pool_index.pop(db_key, None)
            with _integrity_lock:
                _integrity_checked.discard(db_key)


def _invalidate_conn_cache_if_reindex_done(
    db_key: str, reindex_pid_file: Path
) -> None:
    """Evict cached connections when a background reindex has completed."""
    if db_key not in _conn_pool:
        return
    if not reindex_pid_file.exists():
        _invalidate_conn_cache(db_key)


def _evict_stale_entries(db_key: str) -> None:
    """Close and evict stale connections from the pool for *db_key*."""
    now = time.monotonic()
    with _conn_pool_lock:
        pool = _conn_pool.get(db_key)
        if pool is None:
            return
        live: list[_ConnCacheEntry] = []
        for entry in pool:
            if now - entry.opened_at > _CONN_TTL:
                try:
                    entry.conn.close()
                except sqlite3.Error:
                    pass
            else:
                live.append(entry)
        if live:
            _conn_pool[db_key] = live
        else:
            del _conn_pool[db_key]
            _pool_index.pop(db_key, None)
            with _integrity_lock:
                _integrity_checked.discard(db_key)


def _check_pool_health(db_key: str) -> sqlite3.Connection | None:
    """Verify all pool connections are live; return an arbitrary live one or None."""
    with _conn_pool_lock:
        pool = _conn_pool.get(db_key)
        if pool is None or not pool:
            return None
        entries = list(pool)  # snapshot under lock — avoids iterating stale list
    now = time.monotonic()
    for entry in entries:
        try:
            entry.conn.execute("SELECT 1")
            entry.opened_at = now
            return entry.conn
        except sqlite3.Error:
            pass
    _invalidate_conn_cache(db_key)
    return None


def _open_and_cache(db_key: str, db_path: Path) -> sqlite3.Connection:
    """Ensure the connection pool exists and return a connection from it.

    The first call creates 1 connection.  On subsequent calls, if the pool
    has fewer than *POOL_SIZE* entries AND the round-robin cursor wraps to
    the first connection, a new connection is added lazily — this amortises
    ``open_db`` cost across requests instead of paying it all upfront.
    """
    with _conn_pool_lock:
        pool = _conn_pool.get(db_key)
        if pool:
            idx = _pool_index.get(db_key, 0)
            _pool_index[db_key] = idx + 1
            if idx >= len(pool):
                idx = 0
                _pool_index[db_key] = 1
            return pool[idx].conn

    db_path.parent.mkdir(parents=True, exist_ok=True)
    skip_integrity = db_key in _integrity_checked

    try:
        primary_conn = open_db(
            db_path.resolve(),
            check_same_thread=False,
            skip_integrity_check=skip_integrity,
        )
    except DatabaseCorruptionError:
        raise

    with _integrity_lock:
        if db_key not in _integrity_checked:
            _integrity_checked.add(db_key)

    now = time.monotonic()
    entries = [_ConnCacheEntry(conn=primary_conn, opened_at=now)]

    with _conn_pool_lock:
        if db_key in _conn_pool:
            try:
                primary_conn.close()
            except sqlite3.Error:
                pass
            pool = _conn_pool[db_key]
            idx = _pool_index.get(db_key, 0) % len(pool)
            _pool_index[db_key] = idx + 1
            return pool[idx].conn

        _conn_pool[db_key] = entries
        _pool_index[db_key] = 1

    if not skip_integrity:
        _check_integrity(primary_conn)

    return primary_conn


def _maybe_expand_pool(db_key: str, db_path: Path) -> None:
    """Add a new connection to the pool if under *POOL_SIZE* and the cursor
    has wrapped to trigger it.  Called after each request to grow lazily."""
    with _conn_pool_lock:
        pool = _conn_pool.get(db_key)
        if pool is None or len(pool) >= _POOL_SIZE:
            return

    try:
        c = open_db(
            db_path.resolve(),
            check_same_thread=False,
            skip_integrity_check=True,
        )
    except (sqlite3.Error, DatabaseCorruptionError) as exc:
        log.debug("Lazy pool expansion failed for %s: %s", db_key, exc)
        return

    with _conn_pool_lock:
        pool = _conn_pool.get(db_key)
        if pool is not None and len(pool) < _POOL_SIZE:
            pool.append(_ConnCacheEntry(conn=c, opened_at=time.monotonic()))


def _check_integrity(conn: sqlite3.Connection) -> None:
    """Run PRAGMA quick_check + integrity_check on *conn* (one-time)."""
    try:
        quick_result = conn.execute("PRAGMA quick_check").fetchone()
        if quick_result and quick_result[0] != "ok":
            log.warning("quick_check failed: %s — database may be corrupt", quick_result[0])
    except sqlite3.Error:
        pass

    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result and result[0] != "ok":
            log.warning("integrity_check: %s", result[0])
    except sqlite3.Error:
        pass


def _open_db_safe(db_path: Path) -> tuple[sqlite3.Connection | None, dict | None]:
    """Public entry point: check pool health, check reindex, open if needed.

    Returns ``(conn, None)`` on success or ``(None, error_dict)`` when the
    database is corrupt or unreachable.  Lazily expands the connection pool
    on each call, up to *POOL_SIZE*.

    Re-exported via ``context.py`` — the canonical import for all MCP handlers.
    """
    db_key = str(db_path.resolve())
    _evict_stale_entries(db_key)
    cached = _check_pool_health(db_key)
    if cached is not None:
        _maybe_expand_pool(db_key, db_path)
        return cached, None

    reindex_pid = db_path.parent / "reindex.pid"
    _invalidate_conn_cache_if_reindex_done(db_key, reindex_pid)

    try:
        conn = _open_and_cache(db_key, db_path)
        _maybe_expand_pool(db_key, db_path)
        return conn, None
    except DatabaseCorruptionError as e:
        with _integrity_lock:
            _integrity_checked.discard(db_key)
        if getattr(e, "locked", False):
            return None, {
                "error": f"Database locked: {e.details}",
                "action": "retry",
                "hint": "Stop any running fw-context index process and retry.",
            }
        return None, {
            "error": f"Database corruption detected: {e}",
            "action": "reset_index",
            "hint": "Run reset_index() then fw-context index to rebuild.",
        }


def _open_db_or_return(db_path: Path) -> tuple[sqlite3.Connection | None, list[dict] | None]:
    """Open DB via ``_open_db_safe`` and return ``(conn, None)`` or ``(None, error_result)``.

    Unlike ``_open_db_safe``, this never returns ``(None, None)`` — the
    error result is always a ready-to-return list of error dicts.
    Handlers should narrow with ``if err_result is not None: return err_result``
    followed by ``assert conn is not None`` for mypy.
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
    cfg: Config
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
    if err_result is not None:
        return None, err_result
    assert conn is not None

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
