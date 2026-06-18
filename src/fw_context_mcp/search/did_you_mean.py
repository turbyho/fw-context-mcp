"""Did-you-mean? suggestions for lookup_symbol when no exact match is found.

Uses difflib for fast approximate matching against the indexed symbol names.
Results are cached per-query for the lifetime of the process.
"""

from __future__ import annotations

import difflib
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Simple cache: {query: [suggestions]}
_cache: dict[str, list[str]] = {}
_MAX_CACHE = 128


def _load_names(conn: sqlite3.Connection, config_hash: str) -> list[str]:
    """Load all definition names from the index for fuzzy matching.

    Only loads definition symbols (is_definition=1) of callable kinds to keep
    the set small and relevant — these are what users typically search for.
    """
    rows = conn.execute(
        """SELECT DISTINCT name FROM symbols
           WHERE config_hash = ?
             AND is_definition = 1
             AND kind IN ('function', 'method', 'constructor', 'destructor')
           ORDER BY name""",
        (config_hash,),
    ).fetchall()
    return [r["name"] for r in rows]


def suggest(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 5,
    cutoff: float = 0.5,
) -> list[str]:
    """Return symbol names similar to *name*, or empty list.

    Args:
        conn: Open SQLite connection.
        config_hash: Build config hash.
        name: The user-provided name that didn't match.
        limit: Maximum suggestions to return.
        cutoff: Minimum similarity ratio (0.0–1.0).  Default 0.5.

    Returns:
        List of similar symbol names, best match first.
    """
    cache_key = f"{config_hash}:{name}"
    if cache_key in _cache:
        return _cache[cache_key][:limit]

    candidates = _load_names(conn, config_hash)
    if not candidates:
        return []

    matches = difflib.get_close_matches(name, candidates, n=limit, cutoff=cutoff)

    # Maintain cache size
    if len(_cache) >= _MAX_CACHE:
        _cache.pop(next(iter(_cache)))
    _cache[cache_key] = matches

    return matches
