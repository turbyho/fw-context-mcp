"""Phase 4: Execute FTS5 queries and merge results."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

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
        from fw_context_mcp.indexer.db import open_db

        queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
        fetch_limit = max(ctx.limit * 6, 120)

        conn = open_db(ctx.db_path)
        with conn:
            rows = _search_queries(conn, queries, ctx.config_hash, fetch_limit)

        return ctx.evolve(fts5_results=rows)


def _search_queries(conn, queries: list[str], config_hash: str, fetch_limit: int) -> list[dict]:
    """Execute both OR query and name_tokens query, merge with dedup.

    The name_tokens column filter is applied to each space-separated token
    individually — in FTS5, ``name_tokens : a b*`` would otherwise apply the
    column filter only to ``a``, leaving ``b*`` unconstrained.
    """
    if not queries:
        return []

    import re

    or_query = " OR ".join(queries)

    # FTS5 column-filter syntax applies only to the immediately following
    # token.  Split multi-word queries so each token gets its own filter:
    #   "modem init*" → "name_tokens : modem AND name_tokens : init*"
    #
    # Sanitize tokens: only allow alphanumeric, underscore, and wildcard.
    # LLM-generated terms may contain FTS5 syntax that would change query
    # semantics if interpolated directly into the column filter.
    _SAFE_TOKEN = re.compile(r"^[\w*]+$")
    _nt_parts: list[str] = []
    for kq in queries:
        tokens = [t for t in kq.split() if _SAFE_TOKEN.match(t)]
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
        except Exception as e:
            log.warning("FTS5 query failed (q=%r): %s", q[:60], e)

    return rows
