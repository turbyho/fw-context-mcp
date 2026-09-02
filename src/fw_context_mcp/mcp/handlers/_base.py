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
    scopes: list[dict] = dataclasses.field(default_factory=list)
    multi: bool = False

    def execute_scoped(self, query_fn):
        """Run ``query_fn(conn, config_hash)`` per build scope, merge + annotate.

        Single-scope calls keep the result shape of ``executor.execute_sync``.
        Multi-scope calls merge per-build outputs: list items and dict records
        gain a ``variant``/``image`` key so the LLM always knows which build
        produced a result.

        Every scope goes through ``with_stale_annotation``, thus a result that
        names a changed file carries a warning.  Without it the call-graph and
        inheritance handlers gave index data as current data, and the caller
        had no way to see the difference.  ``run_scoped_query`` in
        ``shared/variants.py`` does the same for the lookup handler.
        """
        from ..shared.stale import with_stale_annotation

        if len(self.scopes) <= 1:
            return with_stale_annotation(
                self.root, self.executor, query_fn, self.config_hash
            )
        merged: list = []
        # Every scope reports staleness on its own, and the scopes usually
        # share the same changed files.  Collect the notices and emit each
        # distinct one once, otherwise a three-variant project shows the same
        # warning three times and reads like three separate problems.
        notices: list[str] = []
        for index, scope in enumerate(self.scopes):
            # Only the first scope diagnoses an empty result.  That answer
            # describes the project — files changed on disk, sources absent
            # from compile_commands.json — not one build of it, so every
            # scope reaches the same conclusion and the dedup below drops
            # all but one.  It is not cheap to reach: a scan of every
            # indexed file plus a listing of the source tree, and it runs
            # inside the executor lock.  Nine configs on the Zephyr project
            # paid for it nine times and used one.
            part = with_stale_annotation(
                self.root, self.executor, query_fn, scope["config_hash"],
                diagnose_empty=(index == 0),
            )
            if isinstance(part, list):
                for r in part:
                    if isinstance(r, dict) and set(r) == {"warning"}:
                        notices.append(r["warning"])
                        continue
                    if isinstance(r, dict) and "error" not in r and "warning" not in r:
                        r = dict(r)
                        r["variant"] = scope["variant"]
                        r["image"] = scope["image"]
                    merged.append(r)
            elif isinstance(part, dict):
                if "error" in part:
                    merged.append(part)
                else:
                    d = dict(part)
                    d["variant"] = scope["variant"]
                    d["image"] = scope["image"]
                    merged.append(d)
            else:
                merged.append(part)
        if notices:
            merged[:0] = [{"warning": w} for w in dict.fromkeys(notices)]
        return merged


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
        """Resolve project → config → db → executor → config_hash (+ scopes).

        Delegates to :func:`_resolve_handler_context` — one call replaces
        the common handler preamble.  ``variant``/``image`` narrow the
        multi-project selection (fail-closed, see ``resolve_scopes``).

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
            scopes=ctx.scopes,
            multi=ctx.multi,
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

