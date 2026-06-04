"""Phase 3: Execute FTS5 queries and merge results."""

from __future__ import annotations

import logging

from fw_context_mcp.search.phases.base import Phase

log = logging.getLogger(__name__)


class FTS5SearchPhase(Phase):
    """Run all generated queries (OR + name_tokens variants) via FTS5.

    Merges results from both query styles, preferring definitions when the
    same symbol appears in both.
    """

    name = "fts5_search"

    async def run(self, ctx):
        from fw_context_mcp.indexer.db import open_db, search_symbols

        queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
        fetch_limit = max(ctx.limit * 6, 120)

        conn = open_db(ctx.db_path)
        with conn:
            rows = _search_queries(conn, queries, ctx.config_hash, fetch_limit)

        return ctx.evolve(fts5_results=rows)


def _search_queries(conn, queries: list[str], config_hash: str, fetch_limit: int) -> list[dict]:
    """Execute both OR query and name_tokens query, merge with dedup."""
    if not queries:
        return []

    or_query = " OR ".join(queries)
    nt_terms = [f"name_tokens : {kq}" for kq in queries]
    nt_query = " OR ".join(nt_terms)

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
            log.debug("FTS5 query failed (q=%r): %s", q[:60], e)

    return rows
