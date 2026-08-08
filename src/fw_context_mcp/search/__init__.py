"""Search pipeline — composable phases for code search.

Why a pipeline architecture?
    The search system must support multiple search modes (quick FTS5 search,
    full semantic+lexical smart search, semantic-only search) that share
    overlapping phases.  A composable pipeline lets each mode assemble the
    phases it needs without duplicating logic.

    Each phase reads from a shared ``PipelineContext`` and returns an
    updated context — phases are isolated, testable, and reusable across
    pipeline configurations.

Why lazy module-level constants?
    ``SMART_SEARCH`` imports ``EmbeddingPhase`` which pulls in ``sqlite-vec``
    and sentence-transformers.  These are heavy dependencies not needed
    for simple ``SEARCH_CODE`` queries.  Using ``__getattr__`` defers the
    import until ``SMART_SEARCH`` is actually accessed.

Provides:
- PipelineContext: immutable-ish state object flowing through phases
- PipelineRunner: executes a configured sequence of phases
- PipelineConfig: which phases to run, their parameters
- Predefined pipelines: SEARCH_CODE (quick search), SMART_SEARCH (full pipeline)
"""

from fw_context_mcp.search.pipeline import (
    SEARCH_CODE,
    PipelineConfig,
    PipelineRunner,
    _build_smart_search,
)


# Lazy materialization defers embedding imports (sqlite-vec, sentence-transformers)
# until SMART_SEARCH is actually accessed.  This keeps import times fast for
# simple FTS5-only searches that don't need the embedding stack.
def __getattr__(name):
    if name == "SMART_SEARCH":
        return _build_smart_search()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "PipelineRunner",
    "PipelineConfig",
    "SEARCH_CODE",
    "SMART_SEARCH",
]
