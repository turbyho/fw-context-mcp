"""Shared base handler with context resolution.

Provides ``BaseHandler`` — a mixin-style class that all MCP tool handlers
can inherit from to eliminate the repetitive resolve→open→query
boilerplate."""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.mcp.shared.context import _resolve_handler_context

if TYPE_CHECKING:
    from ...config.settings import Config

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
    cfg: Config
    project_id: str
    root: Path


class BaseHandler:
    """Mixin for MCP tool handlers — context resolution.

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
        assert ctx is not None, "HandlerContext must be non-None when err is None"
        return DbContext(
            db_path=ctx.db_path,
            conn=ctx.conn,
            config_hash=ctx.config_hash,
            cfg=ctx.cfg,
            project_id=ctx.project_id,
            root=ctx.root,
        )



    @staticmethod
    def _get_db_path(project_root: Path) -> Path:
        """Resolve index.db path for *project_root*."""
        from fw_context_mcp.mcp.shared.context import _db_path

        return _db_path(project_root)

