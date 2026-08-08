"""Shared embedding helpers — table checks and brute-force cosine similarity.

Why a separate helpers module?
    Both ``rough_search`` and ``embedding`` phases need to:
    - Check whether vec0/embeddings tables exist and have data
    - Compute brute-force cosine similarity against stored BLOB embeddings
    - Apply kind-diverse round-robin selection for balanced result sets

    Extracting these avoids ~40 lines of duplicated code across the two
    phases and provides a single place to fix table-existence bugs.

Why round_robin_by_kind?
    Embedding similarity alone tends to crowd results with functions
    (the most common symbol kind).  A query about a struct or global
    variable would get 20 function results and 0 structs regardless of
    relevance.  Round-robin by kind ensures the top-N results include
    at least one of each major kind, surface diverse symbols.

Used by both ``rough_search`` and ``embedding`` phases to avoid ~40 lines
of duplicated code.
"""

from __future__ import annotations

import math
import re

# Safe table name pattern — prevents SQL injection when interpolating
# table names into SQL strings (needed for sqlite_master queries where
# table name can't be parameterised).
_SAFE_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

def table_exists(conn, name: str) -> bool:
    """Check whether a table or virtual table exists.

    Why sqlite_master?
        Virtual tables (like vec0) appear in sqlite_master, not just
        regular tables.  ``PRAGMA table_info`` would miss virtual tables.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def table_has_rows(conn, name: str) -> bool:
    """Check whether a table exists and contains at least one row.

    Why validate the name?
        The table name is interpolated into SQL (not parameterised) because
        SQLite doesn't support parameterised table names.  The regex guard
        prevents injection from config values that might feed into table names.
    """
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

    Why brute-force instead of KNN?
        Legacy indexes store embeddings as BLOBs in a regular table without
        a KNN index (sqlite-vec vec0).  The only way to search them is to
        load all embeddings into memory and compute cosine similarity
        manually.  This is acceptable for rough sampling (where only top-20
        are needed) but too slow for precise search (>5000 embeddings).

    Why cosine similarity?
        Embedding models (mxbai-embed-large, qwen3-embedding) are trained
        with cosine-similarity loss.  Euclidean distance would produce
        different rankings for the same model.

    Returns ``(symbol_id, similarity)`` pairs sorted by similarity
    descending, only including results above *threshold*.
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

    Why round-robin instead of ORDER BY CASE?
        SQL ORDER BY CASE can weight kinds but can't guarantee at least
        one of each kind in the top-N.  With 20 results and 6 kind groups,
        ORDER BY CASE typically produces 15 functions, 3 methods, 2 classes,
        and 0 structs/globals.  Round-robin guarantees each group gets at
        least floor(20/6)=3 slots, surfacing diverse symbol kinds.

    Why fixed order of groups?
        Functions and methods are most commonly searched — they appear
        first.  Structs and globals are less common targets — they appear
        later but are still guaranteed slots.

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
