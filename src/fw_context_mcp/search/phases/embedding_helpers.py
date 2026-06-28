"""Shared embedding helpers — table checks and brute-force cosine similarity.

Used by both ``rough_search`` and ``embedding`` phases to avoid ~40 lines
of duplicated code.
"""

from __future__ import annotations

import math


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
