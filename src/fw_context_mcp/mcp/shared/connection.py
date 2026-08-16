"""Database connection management for MCP handlers.

The former connection pool (``_ConnCacheEntry``, round-robin cursor,
TTL eviction, lazy expansion — up to 4 connections per database) was
REMOVED and replaced by ``SyncQueryExecutor`` in ``executor.py``:

- Multiple connections on a single SQLite file provide no real
  parallelism — the SQLite-internal mutex plus shared disk I/O serialize
  them anyway.  Under parallel MCP load the pool produced I/O
  contention that made every query slower (observed: all BFS call-graph
  queries timing out on a 350K-reference database).
- The pool's warm-connection benefit is preserved: the executor holds
  its single connection for the process lifetime, so the expensive
  ``open_db`` work (``ensure_schema`` write transaction, integrity
  scan, extension loading) is paid once, not per request.
- ``_open_db_safe`` / ``_open_db_or_return`` were removed together with
  the pool — all handler queries go through
  ``executor.execute_sync(...)``; quick config reads use
  ``_quick_open_readonly``.

Do NOT reintroduce a connection pool here.

What remains in this module:

- ``_integrity_checked`` — set of db_keys pre-marked at server startup
  (``server.main``); the integrity scan (15-30 s on multi-GB databases)
  must never run during request handling.
- ``_quick_open_readonly`` — short-lived read-only connections for
  per-request config reads.
- ``HandlerContext`` + ``_resolve_handler_context`` — one-call context
  resolution for all MCP handlers.

External users should continue importing from ``.context``
which re-exports everything from this module.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp import config

from .executor import SyncQueryExecutor, get_executor

if TYPE_CHECKING:
    from fw_context_mcp.config.settings import Config

log = logging.getLogger(__name__)

# ── Integrity check cache ───────────────────────────────────────────────
# db_keys whose database was already integrity-checked (pre-marked by
# server.main at startup).  Kept for the startup path; request-path code
# never runs integrity checks.

_integrity_checked: set[str] = set()
_integrity_lock = threading.Lock()


def _quick_open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the index database read-only for quick config reads.

    Skips everything ``open_db`` pays for: integrity check, schema
    migration, ``ensure_schema`` (whose unconditional ``executescript``
    is a write transaction — a hidden source of lock contention when
    paid per request), WAL pragmas, and extension loading.  A read-only
    open is sub-millisecond and never takes a write lock.

    Use ONLY for short config/metadata reads (e.g. ``get_active_config``,
    ``count_refs`` in ``_resolve_handler_context``).  NOT for general
    queries — those go through the ``SyncQueryExecutor``.

    ``as_uri()`` percent-encodes spaces, '?' and '#' — a plain
    ``f"file:{path}"`` URI breaks on such paths.

    WAL caveat: a read-only open of a WAL database needs a writable
    ``-shm`` file (wal-index); otherwise SQLite falls back to a heap
    wal-index recovery = full ``-wal`` scan per open.  The server process
    owns the index directory, so ``-shm`` is writable in practice.

    The caller MUST close the returned connection.  Do not use
    ``with conn:`` — the connection is read-only, there is nothing to
    commit.
    """
    abs_path = db_path.resolve()
    conn = sqlite3.connect(f"{abs_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


# ── HandlerContext — shared setup for all MCP handlers ────────────────

@dataclass
class HandlerContext:
    """Pre-resolved context for an MCP handler invocation.

    All fields are guaranteed non-None — callers that receive a
    ``HandlerContext`` can access every field without further checks.

    There is deliberately NO ``conn`` field (not even ``None``): a
    missing attribute fails loudly at the call site (and statically via
    mypy), while ``conn=None`` would only fail at runtime.  All database
    access goes through ``executor.execute_sync(query_fn, config_hash)``
    — handlers must not open their own connections.
    """

    executor: SyncQueryExecutor
    config_hash: str
    root: Path
    cfg: Config
    project_id: str
    db_path: Path
    scopes: list[dict]
    multi: bool


def _resolve_handler_context(
    project_root: str | None,
    *,
    require_refs: bool = False,
    variant: str | None = None,
    image: str | None = None,
) -> tuple[HandlerContext | None, list[dict] | None]:
    """One-call setup for MCP handlers: resolve project, read config, get executor.

    Two-step design:

    1. ``_quick_open_readonly`` — a short-lived read-only connection
       reads ``config_hash`` (and checks refs when *require_refs*).
       Read-only means no ``ensure_schema`` write transaction per
       request.  No ``with conn:`` — there is nothing to commit on a
       read-only connection.
    2. ``get_executor(db_path)`` — the long-lived single connection all
       queries run on.  ``config_hash`` is NOT stored on the executor;
       it is re-read here on every request and passed per call, so a
       reindex with a changed build config cannot leave queries
       filtering by a stale hash.

    Why the two-step split exists:

    - Step 1 opens a short-lived R/O connection, reads metadata, and
      closes it — sub-millisecond, no write lock ever held.
    - Step 2 gets or creates the executor whose single connection is
      shared for the process lifetime.  If step 2 opened the executor
      first and then read config_hash from it, the executor open
      would pay the WAL setup cost before we even knew the project
      is valid.

    Returns ``(ctx, None)`` on success or ``(None, [error_dict])`` on failure.

    When *require_refs* is ``True``, the call also verifies that the reference
    index is populated (returns an ``info`` dict when empty).
    """
    from .readiness import _db_path, resolve_project_root

    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return None, [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    cfg = config.load(root)
    from fw_context_mcp.config import derive_project_id

    project_id = derive_project_id(root)

    conn = _quick_open_readonly(db_path)
    try:
        from .variants import resolve_scopes

        scopes, multi, err = resolve_scopes(conn, project_id, cfg, variant or "", image or "")
        if err:
            return None, [{"error": err}]
        if not scopes:
            return None, [{"error": "No build config indexed."}]
        config_hash = scopes[0]["config_hash"]

        if require_refs:
            from fw_context_mcp.indexer.db import count_refs

            if count_refs(conn, config_hash) == 0:
                return None, [{"info": (
                    "No references indexed. Refs are on by default — "
                    "they may have been disabled with [index] index_refs = false. "
                    "Re-run 'fw-context index' to rebuild."
                )}]
    finally:
        conn.close()

    executor = get_executor(db_path)

    return HandlerContext(
        executor=executor,
        config_hash=config_hash,
        root=root,
        cfg=cfg,
        project_id=project_id,
        db_path=db_path,
        scopes=scopes,
        multi=multi,
    ), None
