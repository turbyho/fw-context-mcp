"""Search pipeline — composable phases for code search.

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


# Lazily materialize SMART_SEARCH so embedding imports are deferred.
def __getattr__(name):
    if name == "SMART_SEARCH":
        return _build_smart_search()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Lazily materialized via __getattr__ for backward compat (appears in __all__)

__all__ = [
    "PipelineRunner",
    "PipelineConfig",
    "SEARCH_CODE",
    "SMART_SEARCH",
]
