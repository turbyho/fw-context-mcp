"""Shared base handler with context resolution and staleness helpers.

Provides ``BaseHandler`` — a mixin-style class that all MCP tool handlers
can inherit from to eliminate the repetitive resolve→open→query→stale
boilerplate.

Usage::

    class MyHandler(BaseHandler):
        def my_tool(self, project_root: str | None = None) -> dict:
            db_ctx = self.resolve_db_context(project_root)
            results = self._query(db_ctx.conn, db_ctx.config_hash)
            return self.handle_staleness(results, db_ctx)
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pathlib import Path

from fw_context_mcp.mcp.shared.context import _resolve_handler_context
from fw_context_mcp.mcp.shared.stale import _stale_files, _with_stale_recovery
from fw_context_mcp.utils import abs_path, resolve_project_root

log = logging.getLogger(__name__)


@dataclasses.dataclass
class DbContext:
    """Resolved database context for a single request.

    Attributes:
        db_path: Path to the SQLite index database.
        conn: Open, integrity-checked connection.
        config_hash: Active build config hash.
        cfg: Full project config object.
        project_id: UUID project identifier.
        root: Resolved project root directory.
    """

    db_path: Path
    conn: sqlite3.Connection
    config_hash: str
    cfg: object  # FullConfig — avoided import to prevent circular dependency
    project_id: str
    root: Path


class BaseHandler:
    """Mixin for MCP tool handlers — context resolution + staleness wrapping.

    All methods are static so they can be used without instantiation.
    The class exists as a namespace for discoverability; handlers can
    inherit from it or call the methods directly.
    """

    # ── Context resolution ─────────────────────────────────────────

    @staticmethod
    def resolve_db_context(project_root: str | None = None) -> DbContext:
        """Resolve project → config → db → connection → config_hash.

        Delegates to :func:`_resolve_handler_context` — one call replaces
        the common handler preamble.

        Returns:
            A ``DbContext`` with all resolved fields.
        Raises:
            RuntimeError: Project not initialized or index missing.
        """
        ctx, err = _resolve_handler_context(project_root)
        if err:
            raise RuntimeError(err[0].get("error", "Failed to resolve handler context"))
        return DbContext(
            db_path=ctx.db_path,
            conn=ctx.conn,
            config_hash=ctx.config_hash,
            cfg=ctx.cfg,
            project_id=ctx.project_id,
            root=ctx.root,
        )

    @staticmethod
    def resolve_db_context_optional(project_root: str | None = None) -> DbContext | None:
        """Like :meth:`resolve_db_context` but returns None on error.

        Use in tools that handle the missing-index case themselves
        (e.g. ``reindex_file_impl``).
        """
        try:
            return BaseHandler.resolve_db_context(project_root)
        except (RuntimeError, sqlite3.Error, OSError):
            return None

    # ── Staleness handling ─────────────────────────────────────────

    @staticmethod
    def handle_staleness(
        results: list[dict],
        db_ctx: DbContext,
        *,
        stale_msg: str = "",
    ) -> list[dict]:
        """Check result files for staleness and prepend a warning if stale.

        When stale files are found, triggers the background daemon so
        the index catches up asynchronously.  The original results are
        always returned — the daemon handles the fix in the background.

        Args:
            results: Query result rows (each must have a ``file`` key).
            db_ctx: Resolved context from :meth:`resolve_db_context`.
            stale_msg: Optional custom warning message prefix.

        Returns:
            List of dicts — ``[{"warning": ...}, *results]`` if stale,
            otherwise just ``results``.
        """
        file_paths = [abs_path(db_ctx.root, r["file"]) for r in results if "file" in r]
        if not file_paths:
            return results

        stale = _stale_files(db_ctx.conn, db_ctx.config_hash, file_paths, db_ctx.root)
        if not stale:
            return results

        from fw_context_mcp.mcp.background import _ensure_daemon_running

        _ensure_daemon_running(db_ctx.root)
        prefix = stale_msg or (
            f"Results may be stale — {len(stale)} file(s) changed. "
            "Background reindex in progress. Run 'fw-context index' to force full update."
        )
        return [{"warning": prefix}] + results

    # ── Convenience: full query wrapper ────────────────────────────

    @staticmethod
    def with_stale_recovery(
        project_root: str | None,
        query_fn,
        *,
        stale_msg: str = "",
    ) -> list[dict]:
        """Execute *query_fn(conn, config_hash)* with automatic stale recovery.

        Delegates to :func:`fw_context_mcp.mcp.shared.stale._with_stale_recovery`.

        Args:
            project_root: Project root directory (or None for CWD).
            query_fn: Callable ``(conn, config_hash) -> list[dict]``.
            stale_msg: Optional custom warning message.

        Returns:
            List of result dicts, possibly with a leading warning entry.
        """
        root = resolve_project_root(project_root)
        db_path = BaseHandler._get_db_path(root)
        return _with_stale_recovery(root, db_path, query_fn, stale_msg=stale_msg)

    @staticmethod
    def _get_db_path(project_root: Path) -> Path:
        """Resolve index.db path for *project_root*."""
        from fw_context_mcp.mcp.shared.context import _db_path

        return _db_path(project_root)
