"""Shared base handler with context resolution.

Provides ``BaseHandler`` — a mixin-style class that all MCP tool handlers
can inherit from to eliminate the repetitive resolve→open→query boilerplate.

Why a mixin class with static methods instead of module-level functions:

- Handlers are standalone modules (each tool gets its own file) that
  need the same two operations: resolve the database context and get
  the DB path.  A base class makes these operations discoverable via
  IDE autocompletion (``BaseHandler.resolve_db_context``) and
  grep-able (``class.*BaseHandler`` finds all handlers).
- Static methods mean handlers can inherit without instantiation —
  ``BaseHandler`` is a namespace, not a stateful object.  No ``self``
  or ``cls`` reference, no constructor, no state to manage.
- The ``DbContext`` dataclass enforces that handlers never receive a
  raw ``conn`` attribute — all database access goes through
  ``executor.execute_sync(query_fn, config_hash)``.  A dataclass with
  NO ``conn`` field was a deliberate design choice: a missing
  attribute fails at import/type-check time, while ``conn=None``
  would only fail at runtime when some handler tries to use it.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.mcp.shared.context import _resolve_handler_context

if TYPE_CHECKING:
    from ...config.settings import Config
    from ..shared.executor import SyncQueryExecutor

log = logging.getLogger(__name__)



@dataclasses.dataclass
class DbContext:
    """Resolved database context for a single request.

    There is deliberately NO ``conn`` field (not even ``None``): a
    missing attribute fails loudly at the call site (and statically via
    mypy), while ``conn=None`` would only fail at runtime.  All database
    access goes through ``executor.execute_sync(query_fn, config_hash)``;
    after the executor migration, grepping for ``db.conn`` attribute
    access over the handlers must return zero hits.

    Attributes:
        db_path: Path to the SQLite index database.
        executor: The single-connection query executor for this database.
        config_hash: Active build config hash (read fresh per request).
        cfg: Full project config object.
        project_id: UUID project identifier.
        root: Resolved project root directory.
    """

    db_path: Path
    executor: SyncQueryExecutor
    config_hash: str
    cfg: Config
    project_id: str
    root: Path

    def execute_scoped(self, query_fn):
        """Run ``query_fn(conn, config_hash)`` for the selected build.

        ``config_hash`` IS the selected build.  ``resolve_build`` answers
        with one build, because a question about code is a question about
        one program — see the docstring of ``shared/variants.py`` — and
        ``_resolve_handler_context`` reads that one hash into this field.

        The call goes through ``with_stale_annotation``, thus a result that
        names a changed file carries a warning.  Without it the call-graph
        and inheritance handlers gave index data as current data, and the
        caller had no way to see the difference.  ``run_scoped_query`` in
        ``shared/variants.py`` does the same for the lookup handler.

        This used to merge the answers of several builds, interleave them and
        share one limit between them.  That machinery is gone with the
        merging it served: a bootloader and an application are separate
        programs, and one answer about both served no question.  The list
        of scopes went with it — a list that can hold one element invites
        a second pass over it, and that is how an unreachable merge branch
        outlived the merge.
        """
        from ..shared.stale import with_stale_annotation

        return with_stale_annotation(
            self.root, self.executor, query_fn, self.config_hash
        )


class BaseHandler:
    """Mixin for MCP tool handlers — context resolution.

    All methods are static so they can be used without instantiation.
    The class exists as a namespace for discoverability; handlers can
    inherit from it or call the methods directly.
    """

    # ── Context resolution ─────────────────────────────────────────

    @staticmethod
    def resolve_db_context(
        project_root: str | None = None,
        *,
        variant: str | None = None,
        image: str | None = None,
    ) -> DbContext:
        """Resolve project → config → db → executor → config_hash.

        Delegates to :func:`_resolve_handler_context` — one call replaces
        the common handler preamble.  ``variant``/``image`` narrow the
        multi-project selection (fail-closed, see ``resolve_build``).

        Returns:
            A ``DbContext`` with all resolved fields.
        Raises:
            RuntimeError: Project not initialized or index missing.
        """
        ctx, err = _resolve_handler_context(project_root, variant=variant, image=image)
        if err:
            raise RuntimeError(err[0].get("error", "Failed to resolve handler context"))
        assert ctx is not None, "HandlerContext must be non-None when err is None"
        return DbContext(
            db_path=ctx.db_path,
            executor=ctx.executor,
            config_hash=ctx.config_hash,
            cfg=ctx.cfg,
            project_id=ctx.project_id,
            root=ctx.root,
        )



    @staticmethod
    def _get_db_path(project_root: Path) -> Path:
        """Resolve index.db path for *project_root*.

        Why a thin wrapper around ``_db_path``:

        - Handlers inherit from ``BaseHandler`` and call
          ``self._get_db_path()`` — without this method every handler
          would import ``_db_path`` directly from ``.context``, creating
          a cross-layer import from handler code into shared
          infrastructure code.
        - When the database location moves (e.g., from
          ``.fw-context/index.db`` to ``.fw-context/db/index.db``),
          only ``_db_path`` in ``readiness.py`` and this thin wrapper
          change — zero handler files are touched.
        """
        from fw_context_mcp.mcp.shared.context import _db_path

        return _db_path(project_root)

