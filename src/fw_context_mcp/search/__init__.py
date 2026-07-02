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

# Materialize SMART_SEARCH at import time so it appears in __all__.
SMART_SEARCH = _build_smart_search()

__all__ = [
    "PipelineRunner",
    "PipelineConfig",
    "SEARCH_CODE",
    "SMART_SEARCH",
]
