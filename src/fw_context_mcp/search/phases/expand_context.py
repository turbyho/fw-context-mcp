"""Phase: Context expansion via call graph — add relevant neighbors to results.

Why expand context?
    Lexical search (FTS5) finds symbols by name.  Semantic search
    (embeddings) finds symbols by conceptual similarity.  But neither
    finds symbols that are CALLED BY the top results — a critical gap.

    Example: searching "modem init" finds ``modem_init()``.  But the
    developer actually wants ``modem_configure_bands()`` which is called
    only from inside ``modem_init()`` and shares no name tokens with
    the query.  Context expansion walks the call graph around the top
    seeds and surfaces these "neighbor" symbols.

Why project definitions only?
    Vendor SDK symbols (e.g. mbed-os internals) have large call graphs
    that would flood results with framework code.  Filtering to
    ``is_project=True`` AND ``is_definition=True`` ensures only team-owned
    implementations appear as neighbors.

Why mixed insertion strategy?
    Original results provide relevance; neighbors provide discoverability.
    Placing neighbors at positions 11-15 (after the top-10 original seeds,
    before remaining lower-ranked results) gives them visibility without
    displacing the highest-relevance hits.

Parameters confirmed by experiment E‑B (both_s10_mixed):
- 10 seeds (top‑10 adaptive fusion results)
- Both directions (callers + callees)
- Mixed strategy (10 original + 5 new neighbors at positions 11–15)
- Filter: project definitions only (is_project=True, is_definition=1)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase
from fw_context_mcp.search.phases.embedding_helpers import round_robin_by_kind

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


class ExpandContextPhase(Phase):
    """Walk the call graph around top results to surface related symbols.

    What it does:
        Takes the top ``SEEDS`` results from ``final_results``, queries the
        ``refs`` table for their callers and callees, and inserts up to
        ``MAX_NEIGHBORS`` new project-local definition symbols after the
        original seeds.

    Why 10 seeds?
        More seeds → more neighbors → more noise.  Fewer seeds → fewer
        neighbors → lower discoverability.  10 seeds was the best tradeoff
        in experiment E-B across all evaluation queries.

    Why both callers AND callees?
        Callers show "who depends on this" — upstream impact.  Callees
        show "what this depends on" — downstream implementation.  Both
        are relevant for understanding how a symbol fits in the system.

    Reads ``final_results`` from the context (populated by adaptive fusion),
    takes the first ``seeds`` of them, and queries the ``refs`` table for
    callers and callees.  Neighbors are filtered to project-local definitions
    only and inserted after the original results.
    """

    name = "expand_context"

    # ── Parameters (confirmed by experiment E-B) ────────────────────
    SEEDS: int = 10        # top-N adaptive fusion results used as seeds
    MAX_NEIGHBORS: int = 5  # max new neighbors inserted
    DIRECTION: str = "both"  # "callers", "callees", or "both"

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.final_results)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        results = ctx.final_results
        seeds = results[: self.SEEDS]

        # Collect seed USRs (skip seeds missing a USR — they can't
        # participate in call-graph queries)
        seed_usrs: list[str] = []
        seed_set: set[tuple] = set()
        for r in seeds:
            seed_set.add((r["name"], r.get("file_path", "")))
            usr = r.get("usr")
            if usr:
                seed_usrs.append(usr)

        if not seed_usrs:
            log.debug("ExpandContext: no seeds have USR — skipping")
            return ctx

        def _query(conn, config_hash):
            # Runs under the executor lock on the single shared
            # connection; the phase must not open its own connection.
            neighbor_usrs = _get_neighbors(conn, config_hash, seed_usrs, self.DIRECTION)
            if not neighbor_usrs:
                return None
            return _resolve_project_defs(
                conn, config_hash, neighbor_usrs, seed_set, self.MAX_NEIGHBORS
            )

        neighbors = ctx.executor.execute_sync(_query, ctx.config_hash)

        if not neighbors:
            return ctx

        # Mixed strategy: original seeds + new neighbors + remaining results.
        # Neighbors appear at positions 11-15 (after seeds, before tail).
        remaining_budget = max(0, ctx.limit - len(seeds) - len(neighbors))
        remaining = results[self.SEEDS:][:remaining_budget]
        final = list(seeds) + neighbors + list(remaining)
        return ctx.evolve(final_results=final)


def _get_neighbors(
    conn, config_hash: str, usrs: list[str], direction: str
) -> set[str]:
    """Batch query callers and/or callees for multiple USRs.

    Why batch instead of per-seed queries?
        Each seed would require 1-2 database round-trips.  With 10 seeds,
        that's 10-20 queries vs 1-2 batched queries.  The IN clause with
        parameterised placeholders does all work in one query.

    Why LIMIT 500?
        The ``refs`` table can be very large (millions of rows for a big
        codebase).  500 is far more neighbors than we'll ever insert (5),
        giving room for project-definition filtering.
    """
    ph = ",".join("?" * len(usrs))
    neighbors: set[str] = set()

    if direction in ("callers", "both"):
        rows = conn.execute(
            f"SELECT DISTINCT from_usr FROM refs "
            f"WHERE config_hash = ? AND to_usr IN ({ph}) "
            f"AND ref_kind = 'call' AND from_usr IS NOT NULL LIMIT 500",
            (config_hash, *usrs),
        ).fetchall()
        for r in rows:
            if r["from_usr"]:
                neighbors.add(r["from_usr"])

    if direction in ("callees", "both"):
        rows = conn.execute(
            f"SELECT DISTINCT to_usr FROM refs "
            f"WHERE config_hash = ? AND from_usr IN ({ph}) "
            f"AND ref_kind = 'call' LIMIT 500",
            (config_hash, *usrs),
        ).fetchall()
        for r in rows:
            if r["to_usr"]:
                neighbors.add(r["to_usr"])

    return neighbors


def _resolve_project_defs(
    conn, config_hash: str, usrs: set[str], seed_set: set[tuple], limit: int
) -> list[dict]:
    """Resolve USRs to symbol rows — project definitions only, exclude seeds.

    Why round_robin_by_kind?
        Call-graph neighbors inherit the kind distribution of the call
        graph — mostly functions calling other functions.  Round-robin
        ensures at least one struct, class, or global neighbor appears
        if the call graph contains them.

    Why exclude seeds?
        A seed that calls another seed (or is called by it) would appear
        in the neighbor set — inserting it again would duplicate results.
        The seed_set check prevents this.
    """
    ph = ",".join("?" * len(usrs))
    rows = conn.execute(
        f"SELECT usr, name, qualified_name, kind, file_path, file_id, line, "
        f"  signature, is_definition, is_project, docstring, is_virtual, "
        f"  is_pure_virtual, parent_usr, is_template, template_usr, "
        f"  summary, inputs, outputs "
        f"FROM symbols WHERE config_hash = ? AND usr IN ({ph}) "
        f"AND is_definition = 1 AND is_project = 1",
        (config_hash, *usrs),
    ).fetchall()

    rows = round_robin_by_kind(rows, limit=limit)

    neighbors: list[dict] = []
    for r in rows:
        key = (r["name"], r["file_path"])
        if key in seed_set:
            continue  # already in original results
        neighbors.append(dict(r))
        if len(neighbors) >= limit:
            break

    return neighbors
