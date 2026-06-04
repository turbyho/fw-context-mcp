"""Search pipeline — composable phases for code search.

Provides:
- PipelineContext: immutable-ish state object flowing through phases
- PipelineRunner: executes a configured sequence of phases
- PipelineConfig: which phases to run, their parameters
- Predefined pipelines: SEARCH_CODE, SMART_SEARCH
"""

from fw_context_mcp.search.pipeline import PipelineRunner, PipelineConfig, SEARCH_CODE, SMART_SEARCH

__all__ = ["PipelineRunner", "PipelineConfig", "SEARCH_CODE", "SMART_SEARCH"]
