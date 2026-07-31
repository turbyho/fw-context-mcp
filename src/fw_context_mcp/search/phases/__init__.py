"""Phase abstractions for the search pipeline."""

from fw_context_mcp.search.phases.adaptive_fusion import AdaptiveFusionPhase
from fw_context_mcp.search.phases.base import Phase
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

__all__ = [
    "AdaptiveFusionPhase",
    "DeduplicatePhase",
    "DocstringFallbackPhase",
    "EmbeddingPhase",
    "ExpandContextPhase",
    "FormatPhase",
    "FTS5SearchPhase",
    "IndividualTermsFallbackPhase",
    "LLMQueryPhase",
    "MacrosFtsFallbackPhase",
    "NameTokensFallbackPhase",
    "Phase",
    "RefinePhase",
    "RoughSearchPhase",
    "TranslatePhase",
]
