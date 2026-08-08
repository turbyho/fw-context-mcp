"""Fallback search phases for the search pipeline.

Why pipeline-level fallback phases?
    The ``SEARCH_CODE`` pipeline includes four fallback phases that run
    sequentially after the primary FTS5 search.  Each fallback widens the
    search scope — from precise name-token matching to broad macro lookup.
    The progressive approach ensures the most relevant strategy is tried
    first, with broader strategies only activated when narrower ones fail.

Why ``should_run`` checks ``fts5_results`` emptiness?
    Each fallback phase only activates when the previous phase produced
    no results.  This sequential gating prevents fallback results from
    cluttering output when the primary FTS5 search already succeeded.

Why adapter wrappers for backward compatibility?
    Tests and handler code call ``_do_name_tokens_fallback()`` etc.
    directly.  The adapter wrappers delegate to the shared functions
    in ``shared_fallbacks.py``, keeping a single canonical implementation
    while maintaining the old function signatures for existing callers.

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

    Why this runs second (after primary FTS5)?
        When FTS5 returns zero results, the query terms may not exist as
        FTS5 tokens.  For example, "HardFault" is tokenised by FTS5 as
        "hardfault" but the name_tokens column stores "hard fault" —
        FTS5 fails, but LIKE on name_tokens succeeds.

    Why N-1 minimum matches?
        When FTS5 failed entirely, at least one term is likely misspelled
        or uses a different convention.  Requiring all N terms would miss
        partial matches; requiring only 1 would be too noisy.  N-1 is the
        pragmatic middle ground.

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

    Why single-term only?
        Multi-term LIKE over docstrings is a full table scan per term —
        the docstring column has no index.  One scan is acceptable for
        single-word queries; multiple scans would be unacceptably slow.

    Why docstring search at all?
        Some concepts only appear in documentation text, not in symbol
        names.  A query like "power consumption" may only match the
        docstring of ``get_load_power``, not its name.  LIKE catches
        these cases.

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

    Why individual search instead of OR?
        FTS5 OR queries are scored by term frequency.  A dominant term
        that matches 1000 times will crowd out results for other terms.
        Searching each term separately and merging ensures each term
        contributes equally.

    Why 2+ words required?
        A single-word query has nothing to "or" — individual search is
        identical to the primary FTS5 path for single terms.

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

    Why last?
        Macros are rarely the target of a code search.  But when they are
        (e.g. searching for ``configMAX_PRIORITIES`` or ``#define UART_BAUD``),
        no symbol-level strategy will match because macros are preprocessor
        constructs, not symbols.  This fallback runs only when all symbol-
        level strategies returned nothing.

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
    """Thin adapter — delegates to :func:`_search_code_name_tokens`.

    Why keep these?
        Existing tests and handler code call these functions directly.
        Removing them would break those callers.  The adapter keeps a
        single canonical implementation in ``shared_fallbacks`` while
        maintaining backward compatibility.
    """
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
