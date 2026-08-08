"""PipelineContext — immutable-ish state flowing through search phases.

Why a frozen dataclass?
    The pipeline has 10+ phases, several of which run database queries
    and mutate state.  Without immutability, a phase could accidentally
    modify a field that another phase already processed, introducing
    subtle ordering bugs.  The frozen dataclass forces phases to return
    a new context via ``ctx.evolve(**updates)``, making data flow explicit.

Why a shared executor instead of per-phase connections?
    Each ``open_db`` call pays an ``ensure_schema`` write transaction
    (~10 ms) and installs a progress handler.  With 10+ phases, that's
    100+ ms of overhead.  Worse, multiple connections contend on the
    same SQLite WAL.  A single shared ``SyncQueryExecutor`` gives every
    phase serialised access on one connection — zero overhead per phase.

Why ``_quick_open_readonly`` for config loading?
    The ``create()`` factory reads exactly two fields from the database
    (config_hash, project_id).  A full ``open_db`` would pay the schema
    migration write transaction for a two-field read — wasteful.  The
    read-only shortcut avoids that cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from fw_context_mcp.config import Config, derive_project_id
from fw_context_mcp.config import load as load_config
from fw_context_mcp.mcp.shared.context import _quick_open_readonly, get_executor
from fw_context_mcp.mcp.shared.executor import SyncQueryExecutor
from fw_context_mcp.utils import resolve_project_root


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """State object flowing through every search phase.

    Why immutable?
        Each phase reads from this context and returns a new one via
        ``ctx.evolve(**updates)``.  This prevents phases from accidentally
        mutating state another phase depends on, and makes the data flow
        through the pipeline explicit and traceable.

    Why slots=True?
        Reduces memory overhead.  Every search creates exactly one context
        that flows through 10+ phases — slots avoid per-instance dict
        overhead.  Combined with frozen=True, this is a zero-copy-shared
        immutable record.

    ``executor`` is the shared single-connection query executor for this
    project's database.  Every phase with database access runs its queries
    through ``ctx.executor.execute_sync(...)`` — phases must NOT open
    their own connections with a bare ``open_db`` call (that paid an
    ensure_schema write transaction and a 10 s progress-handler timeout
    per phase).
    """

    # ── inputs (set at creation) ──────────────────────────────────────────
    config_hash: str
    project_root: Path
    db_path: Path
    query: str
    original_query: str
    config: Config
    executor: SyncQueryExecutor
    limit: int = 20

    # ── Phase 0 output ────────────────────────────────────────────────────
    translated_from: str | None = None

    # ── Phase 1 output ────────────────────────────────────────────────────
    rough_queries: list[str] = field(default_factory=list)
    rough_samples: list[dict] = field(default_factory=list)

    # ── Phase 2 output ────────────────────────────────────────────────────
    llm_understanding: str = ""
    generated_queries: list[str] = field(default_factory=list)

    # ── Phase 4 output ────────────────────────────────────────────────────
    fts5_results: list[dict] = field(default_factory=list)

    # ── Phase 5 output ────────────────────────────────────────────────────
    embedding_results: list[dict] = field(default_factory=list)

    # ── Phase 6 output ────────────────────────────────────────────────────
    final_results: list[dict] = field(default_factory=list)

    # ── Phase 7 output ────────────────────────────────────────────────────
    formatted_results: list[dict] = field(default_factory=list)

    # ── metadata / warnings ───────────────────────────────────────────────
    ollama_warning: dict | None = None
    warnings: list[str] = field(default_factory=list)

    # ── helpers ───────────────────────────────────────────────────────────

    def evolve(self, **kwargs) -> PipelineContext:
        """Return a new context with the given fields replaced.

        Why ``dataclasses.replace``?
            It creates a shallow copy with specific fields replaced —
            O(1) per field, no manual copy-constructor.  Combined with
            frozen=True, this is the standard pattern for immutable
            state updates in a pipeline.
        """
        return replace(self, **kwargs)

    @classmethod
    def create(
        cls,
        query: str,
        project_root: str | None = None,
        limit: int = 20,
    ) -> PipelineContext:
        """Factory: resolve project root, load config, read config_hash, get executor.

        Why a factory method?
            Creating a PipelineContext requires multiple steps (resolve root,
            load config, open DB, read config_hash, create executor) that
            would be error-prone if done manually.  The factory encapsulates
            all initialisation and validates preconditions (index exists,
            build config exists) before returning.

        The config read uses a short-lived read-only connection
        (``_quick_open_readonly``) — a full ``open_db`` would pay an
        ``ensure_schema`` write transaction for a two-field read.  All
        phase queries go through the shared ``executor``.
        """
        from fw_context_mcp.indexer.db import get_active_config

        root = resolve_project_root(project_root)
        cfg = load_config(project_root=root)
        project_id = derive_project_id(root)
        db_path = cfg.index.db_dir / project_id / "index.db"

        if not db_path.exists():
            raise ValueError(f"No index found for {root}. Run 'fw-context index' first.")

        conn = _quick_open_readonly(db_path)
        try:
            build_cfg = get_active_config(conn, project_id)
            if not build_cfg:
                raise ValueError(f"No build config indexed for {root}.")
            config_hash = build_cfg["config_hash"]
        finally:
            conn.close()

        return cls(
            config_hash=config_hash,
            project_root=root,
            db_path=db_path,
            query=query,
            original_query=query,
            config=cfg,
            executor=get_executor(db_path),
            limit=max(5, min(limit, 100)),
        )
