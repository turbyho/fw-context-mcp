"""Shared embedding helpers — table checks and brute-force cosine similarity.

Used by both ``rough_search`` and ``embedding`` phases to avoid ~40 lines
of duplicated code.
"""

from __future__ import annotations

import math
import re

_SAFE_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

def table_exists(conn, name: str) -> bool:
    """Check whether a table or virtual table exists."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def table_has_rows(conn, name: str) -> bool:
    """Check whether a table exists and contains at least one row."""
    if not table_exists(conn, name):
        return False
    if not _SAFE_TABLE_NAME.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
    row = conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
    return row is not None


def brute_force_search(
    query_vec: list[float],
    stored: dict[int, list[float]],
    threshold: float = 0.5,
) -> list[tuple[int, float]]:
    """Compute cosine similarity between *query_vec* and every stored embedding.

    Returns ``(symbol_id, similarity)`` pairs sorted by similarity descending,
    only including results above *threshold*.
    """
    norm_a = math.sqrt(sum(x * x for x in query_vec))
    scored: list[tuple[int, float]] = []
    for sym_id, emb_vec in stored.items():
        dot = sum(x * y for x, y in zip(query_vec, emb_vec, strict=True))
        norm_b = math.sqrt(sum(x * x for x in emb_vec))
        sim = (dot / (norm_a * norm_b)) if norm_a and norm_b else 0.0
        if sim > threshold:
            scored.append((sym_id, sim))
    scored.sort(key=lambda x: -x[1])
    return scored


def round_robin_by_kind(rows: list, limit: int = 20) -> list:
    """Select *limit* rows with kind diversity via round-robin.

    Groups rows by kind (function, method, class, struct, varglobal, other),
    then round-robins one from each group until *limit* is reached.  Avoids
    the ORDER BY CASE problem where functions crowd out structs/globals.
    Preserves the original order within each group (already sorted by
    vector similarity from search_similar_vec).

    Each row must be dict-like with a ``kind`` key (str or None).
    """
    groups: dict[str, list] = {
        "function": [], "method": [], "class": [], "struct": [],
        "varglobal": [], "other": [],
    }
    for r in rows:
        kind = r["kind"] or "other"
        if kind in groups:
            groups[kind].append(r)
        else:
            groups["other"].append(r)

    result: list = []
    indices = {k: 0 for k in groups}
    while len(result) < limit:
        added = False
        for kind in ("function", "method", "class", "struct", "varglobal", "other"):
            idx = indices[kind]
            if idx < len(groups[kind]):
                result.append(groups[kind][idx])
                indices[kind] += 1
                added = True
                if len(result) >= limit:
                    break
        if not added:
            break  # all groups exhausted
    return result
