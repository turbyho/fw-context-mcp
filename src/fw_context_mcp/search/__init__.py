"""Search pipeline — composable phases for code search.

Provides:
- PipelineContext: immutable-ish state object flowing through phases
- PipelineRunner: executes a configured sequence of phases
- PipelineConfig: which phases to run, their parameters
- Predefined pipelines: SEARCH_CODE (quick search), _build_smart_search() (full pipeline)
"""

from fw_context_mcp.search.pipeline import (
    SEARCH_CODE,
    PipelineConfig,
    PipelineRunner,
)

__all__ = [
    "PipelineRunner",
    "PipelineConfig",
    "SEARCH_CODE",
]
