"""Phase: Execute FTS5 queries and merge results.

Why two query styles (OR + name_tokens)?
    FTS5 searches two different column groups:
    - OR query searches ``name``, ``qualified_name``, ``name_tokens``, and
      ``signature`` together.
    - name_tokens query searches ONLY ``name_tokens``, applying the FTS5
      column-filter syntax (``name_tokens : term``) which limits matches
      to the pre-computed camelCase/snake_case token column.

    The OR query is broader (catches signature matches like ``void
    uart_send(...)``).  The name_tokens query is more precise (avoids
    false matches in qualified names).  Merging both gives the best of
    both — broad recall from OR, precision from name_tokens.

Why split multi-word queries in name_tokens?
    FTS5 column-filter syntax (``name_tokens : a b*``) applies the filter
    ONLY to the immediately following token — ``a`` is filtered to
    name_tokens, but ``b*`` is unconstrained.  Splitting each term into
    its own filter (``name_tokens : a AND name_tokens : b*``) ensures
    both are constrained to the name_tokens column.

Why safe token sanitisation?
    LLM-generated query terms may contain FTS5 syntax characters (``(``,
    ``)``, ``"``) that would change query semantics or cause SQL errors.
    The ``_SAFE_FTS5_TOKEN`` regex only passes alphanumeric, underscore,
    and trailing wildcard — anything else is silently dropped.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

# Only allow alphanumeric, underscore, and trailing wildcard in FTS5 tokens.
# LLM-generated terms may contain markdown or FTS5 syntax that would alter
# query semantics if interpolated directly.
_SAFE_FTS5_TOKEN = re.compile(r"^[\w*]+$")

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


class FTS5SearchPhase(Phase):
    """Run all generated queries (OR + name_tokens variants) via FTS5.

    Why run on the executor?
        FTS5 queries require a database connection.  Using the shared
        executor avoids opening a new connection per phase, saving
        ~10 ms per phase in schema-migration overhead.

    Why prefer definitions when merging?
        The same symbol may match in both OR and name_tokens queries —
        once as a declaration (from a header), once as a definition.
        Keeping the definition version is strictly more useful.

    Merges results from both query styles, preferring definitions when the
    same symbol appears in both.
    """

    name = "fts5_search"  #: Phase identifier used in pipeline configuration.

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """Execute all generated (or rough) queries via FTS5 and merge results.

        Falls back to ``rough_queries`` when no ``generated_queries`` exist
        (e.g. SEARCH_CODE pipeline with no LLM phase).
        """
        queries = ctx.generated_queries if ctx.generated_queries else ctx.rough_queries
        # Overfetch: multiply by 6 to account for noise — FTS5 returns
        # many false positives, and the deduplication/scoring phase will
        # filter down to ctx.limit
        fetch_limit = max(ctx.limit * 6, 120)

        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            return _search_queries(conn, queries, config_hash, fetch_limit)

        rows = ctx.executor.execute_sync(_query, ctx.config_hash)

        return ctx.evolve(fts5_results=rows)


def _search_queries(
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
                    # Replace declaration with definition
                    rows[prev_idx] = dict(r)
        except sqlite3.Error:
            log.warning("FTS5 query failed (q=%r)", q[:60], exc_info=True)

    return rows
