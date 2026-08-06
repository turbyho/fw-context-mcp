"""Phase 4: Execute FTS5 queries and merge results."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

_SAFE_FTS5_TOKEN = re.compile(r"^[\w*]+$")  # cached at module level

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


class FTS5SearchPhase(Phase):
    """Run all generated queries (OR + name_tokens variants) via FTS5.

    Merges results from both query styles, preferring definitions when the
    same symbol appears in both.
    """

    name = "fts5_search"  #: Phase identifier used in pipeline configuration.

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Execute all generated (or rough) queries via FTS5 and merge results.

        Opens a fresh DB connection, runs both OR queries and name_tokens
        queries, and merges with deduplication preferring definitions.
        """
        queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
        fetch_limit = max(ctx.limit * 6, 120)

        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _search_queries(conn, queries, config_hash, fetch_limit)

        rows = ctx.executor.execute_sync(_query, ctx.config_hash)

        return ctx.evolve(fts5_results=rows)


def _search_queries(  # NOTE(turbyho, 2026-07-31): combine OR + name_tokens into single FTS5 expression
        conn, queries: list[str], config_hash: str, fetch_limit: int) -> list[dict]:
    """Execute both OR query and name_tokens query, merge with dedup.

    The name_tokens column filter is applied to each space-separated token
    individually — in FTS5, ``name_tokens : a b*`` would otherwise apply the
    column filter only to ``a``, leaving ``b*`` unconstrained.
    """
    if not queries:
        return []


    # Sanitize queries through safe token pattern (guard against LLM-generated
    # FTS5 syntax that could alter query semantics)
    safe_queries = [" ".join(t for t in q.split() if _SAFE_FTS5_TOKEN.match(t)) for q in queries]
    safe_queries = [q for q in safe_queries if q]
    or_query = " OR ".join(safe_queries) if safe_queries else ""

    # FTS5 column-filter syntax applies only to the immediately following
    # token.  Split multi-word queries so each token gets its own filter:
    #   "modem init*" → "name_tokens : modem AND name_tokens : init*"
    #
    # Sanitize tokens: only allow alphanumeric, underscore, and wildcard.
    # LLM-generated terms may contain FTS5 syntax that would change query
    # semantics if interpolated directly into the column filter.
    _nt_parts: list[str] = []
    for kq in queries:
        tokens = [t for t in kq.split() if _SAFE_FTS5_TOKEN.match(t)]
        if not tokens:
            continue
        if len(tokens) >= 2:
            _nt_parts.append(" AND ".join(f"name_tokens : {t}" for t in tokens))
        else:
            _nt_parts.append(f"name_tokens : {tokens[0]}")
    nt_query = " OR ".join(f"({p})" if " AND " in p else p for p in _nt_parts)

    from fw_context_mcp.indexer.db import search_symbols

    rows: list[dict] = []
    seen: dict[tuple, int] = {}

    for q in (or_query, nt_query):
        try:
            for r in search_symbols(conn, q, config_hash, limit=fetch_limit):
                k = (r["name"], r["file_path"])
                prev_idx = seen.get(k)
                if prev_idx is None:
                    seen[k] = len(rows)
                    rows.append(dict(r))
                elif r["is_definition"] and not rows[prev_idx].get("is_definition"):
                    rows[prev_idx] = dict(r)
        except sqlite3.Error:
            log.warning("FTS5 query failed (q=%r)", q[:60], exc_info=True)

    return rows
