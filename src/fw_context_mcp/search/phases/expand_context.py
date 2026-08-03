"""Phase: Context expansion via call graph — add relevant neighbors to results.

After RRF fusion, takes the top seeds and walks their callers + callees to
find project-local symbols that are contextually related but were missed by
lexical and semantic search.

Parameters confirmed by experiment E‑B (both_s10_mixed):
- 10 seeds (top‑10 RRF results)
- Both directions (callers + callees)
- Mixed strategy (10 original + 5 new neighbors at positions 11–15)
- Filter: project definitions only (is_project=True, is_definition=1)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fw_context_mcp.search.phases.base import Phase

if TYPE_CHECKING:
    from fw_context_mcp.search.context import PipelineContext

log = logging.getLogger(__name__)


class ExpandContextPhase(Phase):
    """Walk the call graph around top results to surface related symbols.

    Reads ``final_results`` from the context (populated by RRF fusion), takes
    the first ``seeds`` of them, and queries the ``refs`` table for callers
    and callees.  Neighbors are filtered to project-local definitions only
    and inserted after the original results.
    """

    name = "expand_context"

    # ── Parameters (confirmed by experiment E-B) ────────────────────
    SEEDS: int = 10        # top-N RRF results used as seeds
    MAX_NEIGHBORS: int = 5  # max new neighbors inserted
    DIRECTION: str = "both"  # "callers", "callees", or "both"

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.final_results)

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        from fw_context_mcp.indexer.db import open_db

        results = ctx.final_results
        seeds = results[: self.SEEDS]

        # Collect seed USRs (skip seeds missing a USR)
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

        conn = open_db(ctx.db_path)
        try:
            with conn:
                neighbor_usrs = _get_neighbors(conn, ctx.config_hash, seed_usrs, self.DIRECTION)
        finally:
            conn.close()

        if not neighbor_usrs:
            return ctx

        # Resolve neighbor USRs to symbol rows — project definitions only
        conn = open_db(ctx.db_path)
        try:
            with conn:
                neighbors = _resolve_project_defs(
                    conn, ctx.config_hash, neighbor_usrs, seed_set, self.MAX_NEIGHBORS
                )
        finally:
            conn.close()

        if not neighbors:
            return ctx

        # Mixed strategy: original seeds + new neighbors + remaining results
        remaining = results[self.SEEDS:][: ctx.limit - len(seeds) - len(neighbors)]
        final = list(seeds) + neighbors + list(remaining)
        return ctx.evolve(final_results=final)


def _get_neighbors(
    conn, config_hash: str, usrs: list[str], direction: str
) -> set[str]:
    """Batch query callers and/or callees for multiple USRs."""
    ph = ",".join("?" * len(usrs))
    neighbors: set[str] = set()

    if direction in ("callers", "both"):
        rows = conn.execute(
            f"SELECT DISTINCT from_usr FROM refs "
            f"WHERE config_hash = ? AND to_usr IN ({ph}) "
            f"AND ref_kind = 'call' AND from_usr IS NOT NULL",
            (config_hash, *usrs),
        ).fetchall()
        for r in rows:
            if r["from_usr"]:
                neighbors.add(r["from_usr"])

    if direction in ("callees", "both"):
        rows = conn.execute(
            f"SELECT DISTINCT to_usr FROM refs "
            f"WHERE config_hash = ? AND from_usr IN ({ph}) "
            f"AND ref_kind = 'call'",
            (config_hash, *usrs),
        ).fetchall()
        for r in rows:
            if r["to_usr"]:
                neighbors.add(r["to_usr"])

    return neighbors


def _resolve_project_defs(
    conn, config_hash: str, usrs: set[str], seed_set: set[tuple], limit: int
) -> list[dict]:
    """Resolve USRs to symbol rows — project definitions only, exclude seeds."""
    ph = ",".join("?" * len(usrs))
    rows = conn.execute(
        f"SELECT usr, name, qualified_name, kind, file_path, file_id, line, "
        f"  signature, is_definition, is_project, docstring, is_virtual, "
        f"  is_pure_virtual, parent_usr, is_template, template_usr, "
        f"  summary, inputs, outputs "
        f"FROM symbols WHERE config_hash = ? AND usr IN ({ph}) "
        f"AND is_definition = 1 AND is_project = 1 "
        f"ORDER BY CASE kind WHEN 'function' THEN 0 WHEN 'method' THEN 1 "
        f"  WHEN 'constructor' THEN 2 WHEN 'destructor' THEN 3 "
        f"  WHEN 'varglobal' THEN 4 ELSE 5 END",
        (config_hash, *usrs),
    ).fetchall()

    neighbors: list[dict] = []
    for r in rows:
        key = (r["name"], r["file_path"])
        if key in seed_set:
            continue  # already in original results
        neighbors.append(dict(r))
        if len(neighbors) >= limit:
            break

    return neighbors
