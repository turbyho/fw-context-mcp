"""PipelineContext — immutable-ish state flowing through search phases.

Each phase receives a context, reads what it needs, and returns a NEW context
with its output fields populated.  The dataclass is frozen so phases cannot
accidentally mutate state they don't own.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from fw_context_mcp.config import Config, derive_project_id
from fw_context_mcp.config import load as load_config
from fw_context_mcp.utils import resolve_project_root


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """State object flowing through every search phase.

    Phases read from this context and return a new one via ``ctx.evolve(**updates)``.
    """

    # ── inputs (set at creation) ──────────────────────────────────────────
    config_hash: str
    project_root: Path
    db_path: Path
    query: str
    original_query: str
    config: Config
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
        """Return a new context with the given fields replaced."""
        return replace(self, **kwargs)

    @classmethod
    def create(
        cls,
        query: str,
        project_root: str | None = None,
        limit: int = 20,
    ) -> PipelineContext:
        """Factory: resolve project root, load config, open DB.

        Returns a context ready for pipeline execution, or raises ValueError
        when no index exists.
        """
        from fw_context_mcp.indexer.db import get_active_config, open_db

        # NOTE: opens DB just for config_hash — phases open their own connections.
        # Acceptable trade-off for simplicity; deferred to future refactoring.
        root = resolve_project_root(project_root)
        cfg = load_config(project_root=root)
        project_id = derive_project_id(root)
        db_path = cfg.index.db_dir / project_id / "index.db"

        if not db_path.exists():
            raise ValueError(f"No index found for {root}. Run 'fw-context index' first.")

        conn = open_db(db_path)
        try:
            with conn:
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
            limit=max(5, min(limit, 100)),
        )
