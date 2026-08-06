"""Fallback search phases for the search pipeline.

Each phase implements one fallback strategy from
:mod:`fw_context_mcp.search.shared_fallbacks`.  When the primary
FTS5 search returns no results, these phases try progressively broader
strategies: name-token matching, docstring LIKE, individual-term search,
and macro FTS lookup.

All phases respect the progressive-fallback contract: ``should_run()``
returns ``True`` only when ``fts5_results`` is empty, and ``run()``
populates ``fts5_results`` only when matches are found.
"""

from __future__ import annotations

import logging

from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.shared_fallbacks import (
    _search_code_docstring,
    _search_code_individual_terms,
    _search_code_macros_fts,
    _search_code_name_tokens,
)

if __import__("typing").TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


# ── Fallback phases ──────────────────────────────────────────────────────────


class NameTokensFallbackPhase(Phase):
    """Fallback: FTS5 symbol-name token search with substring LIKE matching.

    Splits the query into terms and matches them against the pre-computed
    ``name_tokens`` column (CamelCase/snake_case split).  Requires at
    least N-1 of N query terms to match.
    """

    name = "name_tokens_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _search_code_name_tokens(
                conn, ctx.query, config_hash,
                ctx.limit, None, False, ctx.project_root,
            )

        result = ctx.executor.execute_sync(_query, ctx.config_hash)
        if result is not None:
            return ctx.evolve(fts5_results=result[0])
        return ctx


class DocstringFallbackPhase(Phase):
    """Fallback: single-term docstring LIKE search.

    Only runs when the query is a single word — does a raw LIKE over
    the ``docstring`` column to catch terms the FTS5 tokeniser missed.
    """

    name = "docstring_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _search_code_docstring(
                conn, ctx.query, config_hash,
                ctx.limit, None, False, ctx.project_root,
            )

        result = ctx.executor.execute_sync(_query, ctx.config_hash)
        if result is not None:
            return ctx.evolve(fts5_results=result[0])
        return ctx


class IndividualTermsFallbackPhase(Phase):
    """Fallback: search each query word individually and merge results.

    Only runs when the query has 2+ words — searches each word separately
    via FTS5 and merges de-duplicated results.
    """

    name = "individual_terms_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _search_code_individual_terms(
                conn, ctx.query, config_hash,
                ctx.limit, None, False, ctx.project_root,
            )

        result = ctx.executor.execute_sync(_query, ctx.config_hash)
        if result is not None:
            return ctx.evolve(fts5_results=result[0])
        return ctx


class MacrosFtsFallbackPhase(Phase):
    """Fallback: FTS5 search over the ``macros_fts`` table.

    Matches ``#define`` names and expansion values — the last resort
    when no symbol matched any of the previous strategies.
    """

    name = "macros_fts_fallback"

    def should_run(self, ctx: PipelineContext) -> bool:
        return not ctx.fts5_results

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _search_code_macros_fts(
                conn, ctx.query, config_hash,
                ctx.limit, None, False, ctx.project_root,
            )

        result = ctx.executor.execute_sync(_query, ctx.config_hash)
        if result is not None:
            return ctx.evolve(fts5_results=result[0])
        return ctx


# ── Adapter wrappers (backward compat for tests) ─────────────────────────────


def _do_name_tokens_fallback(
    c, query: str, config_hash: str, limit: int,
    project_only: bool = False, root=None,
) -> list[dict]:
    """Thin adapter — delegates to :func:`_search_code_name_tokens`."""
    result = _search_code_name_tokens(c, query, config_hash, limit, None, project_only, root)
    return result[0] if result else []


def _do_docstring_fallback(
    c, query: str, config_hash: str, limit: int,
    project_only: bool = False, root=None,
) -> list[dict]:
    """Thin adapter — delegates to :func:`_search_code_docstring`."""
    result = _search_code_docstring(c, query, config_hash, limit, None, project_only, root)
    return result[0] if result else []


def _do_individual_terms_fallback(
    c, query: str, config_hash: str, limit: int,
    project_only: bool = False, root=None,
) -> list[dict]:
    """Thin adapter — delegates to :func:`_search_code_individual_terms`."""
    result = _search_code_individual_terms(c, query, config_hash, limit, None, project_only, root)
    return result[0] if result else []


def _do_macros_fts_fallback(
    c, query: str, config_hash: str, limit: int,
    project_only: bool = False, root=None,
) -> list[dict]:
    """Thin adapter — delegates to :func:`_search_code_macros_fts`."""
    result = _search_code_macros_fts(c, query, config_hash, limit, None, project_only, root)
    return result[0] if result else []
