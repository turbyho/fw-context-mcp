"""PipelineRunner — executes a configured sequence of search phases."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from fw_context_mcp.search.phases.base import Phase

log = logging.getLogger(__name__)

# ── Phase registry ──────────────────────────────────────────────────────────
# Maps phase name strings → Phase instances.  Built lazily on first use so
# imports don't pull in libclang before it's needed.

_REGISTRY: dict[str, Phase] = {}
_REGISTRY_LOCK = threading.Lock()


def _build_registry() -> dict[str, Phase]:
    """Lazy-load all phase classes into the registry."""
    if _REGISTRY:
        return _REGISTRY

    with _REGISTRY_LOCK:
        if _REGISTRY:  # double-check: another thread may have populated
            return _REGISTRY

        from fw_context_mcp.search.phases.adaptive_fusion import AdaptiveFusionPhase
        from fw_context_mcp.search.phases.deduplicate import DeduplicatePhase
        from fw_context_mcp.search.phases.embedding import EmbeddingPhase
        from fw_context_mcp.search.phases.expand_context import ExpandContextPhase
        from fw_context_mcp.search.phases.format import FormatPhase
        from fw_context_mcp.search.phases.fts5_search import FTS5SearchPhase
        from fw_context_mcp.search.phases.llm_query import LLMQueryPhase
        from fw_context_mcp.search.phases.refine import RefinePhase
        from fw_context_mcp.search.phases.rough_search import RoughSearchPhase
        from fw_context_mcp.search.phases.search_fallbacks import (
            DocstringFallbackPhase,
            IndividualTermsFallbackPhase,
            MacrosFtsFallbackPhase,
            NameTokensFallbackPhase,
        )
        from fw_context_mcp.search.phases.translate import TranslatePhase

        for cls in [
            TranslatePhase,
            RoughSearchPhase,
            LLMQueryPhase,
            RefinePhase,
            FTS5SearchPhase,
            EmbeddingPhase,
            AdaptiveFusionPhase,
            DeduplicatePhase,
            ExpandContextPhase,
            FormatPhase,
            NameTokensFallbackPhase,
            DocstringFallbackPhase,
            IndividualTermsFallbackPhase,
            MacrosFtsFallbackPhase,
        ]:
            instance = cls()  # type: ignore[abstract]  # runtime check below
            if not hasattr(instance, 'run') or not callable(instance.run):
                raise TypeError(
                    f"{cls.__name__} is listed as a Phase subclass but does "
                    f"not implement abstract 'run' method"
                )
            _REGISTRY[instance.name] = instance

    return _REGISTRY


# ── Config ──────────────────────────────────────────────────────────────────


@dataclass
class PipelineConfig:
    """Which phases to run, in what order.

    Each element can be either a phase name string (looked up in the registry)
    or a pre-configured ``Phase`` instance (for custom parameters).

    Use the predefined constants ``SEARCH_CODE`` and ``SMART_SEARCH``
    for standard configurations, or build a custom one.
    """

    phases: list = field(default_factory=list)


# Predefined pipelines
SEARCH_CODE = PipelineConfig(
    phases=[
        "rough_search",
        "fts5_search",
        "name_tokens_fallback",
        "docstring_fallback",
        "individual_terms_fallback",
        "macros_fts_fallback",
        "deduplicate",
        "format",
    ],
)


def _build_smart_search() -> PipelineConfig:
    """Lazy-built SMART_SEARCH config — imports Phase classes on first use."""
    from fw_context_mcp.search.phases.embedding import EmbeddingPhase

    return PipelineConfig(
        phases=[
            "translate",
            "rough_search",
            "llm_query",
            "fts5_search",
            "refine",
            EmbeddingPhase(independent=True, threshold=0.5, overfetch=50),
            "adaptive_fusion",
            "deduplicate",
            "expand_context",
            "format",
        ],
    )


# Semantic search pipeline — built lazily because threshold & overfetch
# depend on the caller's parameters.


def _build_semantic_search(threshold: float, overfetch: int) -> PipelineConfig:
    """Lazy-built SEMANTIC_SEARCH config.

    Standalone embedding with source boosting for project-code ranking.
    """
    from fw_context_mcp.search.phases.embedding import EmbeddingPhase

    return PipelineConfig(
        phases=[
            EmbeddingPhase(
                independent=True,
                threshold=threshold,
                overfetch=overfetch,
                source_boost=True,
            ),
            "format",
        ],
    )


# ── Runner ──────────────────────────────────────────────────────────────────


class PipelineRunner:
    """Execute a configured sequence of phases against a PipelineContext.

    Usage::

        ctx = PipelineContext.create(query="modem init", project_root="/path")
        config = _build_smart_search()
        runner = PipelineRunner(config)
        result_ctx = await runner.run(ctx)
        return result_ctx.formatted_results
    """

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.registry = _build_registry()  # lazy on first use

    async def run(self, ctx):
        """Run all configured phases sequentially, returning the final context.

        Each phase that ``should_run()`` returns True for receives the context,
        runs, and returns an updated context.  Skipped phases pass through.
        """
        for item in self.config.phases:
            if isinstance(item, Phase):
                phase = item
                phase_name = item.name
            else:
                phase = self.registry.get(item)
                phase_name = item

            if phase is None:
                log.warning("Unknown phase %r — skipping", phase_name)
                continue

            if not phase.should_run(ctx):
                log.debug("Phase %r skipped", phase_name)
                continue

            t0 = time.monotonic()
            try:
                ctx = await phase.run(ctx)
                elapsed = time.monotonic() - t0
                log.debug("Phase %r completed in %.2fs", phase_name, elapsed)
            except BaseException as exc:
                from fw_context_mcp.utils import is_fatal
                if is_fatal(exc):
                    raise
                if isinstance(exc, (ValueError, TypeError, AttributeError, RuntimeError)):
                    log.exception("Phase %r crashed — this is likely a bug", phase_name)
                else:
                    log.warning("Phase %r failed: %s", phase_name, exc)
                ctx = ctx.evolve(warnings=ctx.warnings + [f"Phase {phase_name!r} failed: {exc}"])
                # Continue with remaining phases — one phase failure
                # shouldn't break the entire search.

        return ctx
