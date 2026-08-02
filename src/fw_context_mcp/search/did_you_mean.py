"""Did-you-mean? suggestions for lookup_symbol when no exact match is found.

Uses token-based matching (snake_case + camelCase split) instead of difflib
sequence matching.  Token matching prefers candidates that share multiple
tokens with the query, with exact-token and prefix-token weights.

Results are cached per-query for the lifetime of the process.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from functools import lru_cache

log = logging.getLogger(__name__)

# Simple cache with TTL: {query: (timestamp, [suggestions])}
_cache: dict[str, tuple[float, list[str]]] = {}
_MAX_CACHE = 128
_CACHE_TTL_S = 300  # Invalidate after 5 minutes (matches keyword_cache)
_cache_lock = threading.Lock()
# Set of cache keys currently being computed — prevents stampede when
# multiple threads query the same unmatched name simultaneously.
_in_flight: set[str] = set()

# Characters that delimit tokens in symbol names
_TOKEN_SPLIT = re.compile(r"[_]+")


@lru_cache(maxsize=20000)
def _tokenize(name: str) -> tuple[str, ...]:
    """Split a symbol name into lowercase tokens.

    Splits on ``_`` first, then splits each part on camelCase boundaries.
    Returns deduplicated, lowercased tokens.
    """
    tokens: list[str] = []
    for part in _TOKEN_SPLIT.split(name):
        if not part:
            continue
        # Split camelCase: "nrfxUarteInit" → ["nrfx", "Uarte", "Init"]
        # Second alternative uses [A-Z]+ (not [A-Z0-9]+) so digits after an
        # acronym become their own token: "TIM16Config" → ["TIM", "16", "Config"].
        subtokens = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z][a-z]|$|\d)", part)
        for t in subtokens:
            t = t.lower()
            if t and t not in tokens:
                tokens.append(t)
    return tuple(tokens)


def suggest(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 5,
) -> list[str]:
    """Return symbol names similar to *name*, or empty list.

    Uses token-based matching: splits both query and candidates into tokens
    (on ``_`` and camelCase boundaries), then scores candidates by how many
    query tokens they share.  Exact token matches score higher than prefix
    matches.  At least one token must match exactly or by prefix.

    Args:
        conn: Open SQLite connection.
        config_hash: Build config hash.
        name: The user-provided name that didn't match.
        limit: Maximum suggestions to return.

    Returns:
        List of similar symbol names, best match first.
    """
    cache_key = f"{config_hash}:{name}"
    with _cache_lock:
        if cache_key in _cache:
            ts, cached = _cache[cache_key]
            if time.monotonic() - ts < _CACHE_TTL_S:
                return cached[:limit]
            del _cache[cache_key]
        # Stampede guard: another thread is already computing this key.
        # Return empty early — the result will be cached for next time.
        if cache_key in _in_flight:
            return []
        _in_flight.add(cache_key)

    try:
        t0 = time.monotonic()
        candidates = _load_names(conn, config_hash)
        if not candidates:
            return []

        query_tokens = _tokenize(name)
        log.debug("did-you-mean: loaded %d candidates in %.2fs for '%s'",
                  len(candidates), time.monotonic() - t0, name)
        if not query_tokens:
            return []

        # Build token → candidate index for efficient filtering
        token_index: dict[str, list[str]] = defaultdict(list)
        # Prefix index: first 3 chars → set of tokens — narrows O(n) scan to O(1)
        prefix_index: dict[str, set[str]] = defaultdict(set)
        # Short prefix index for 2-char tokens — avoids O(n) scan over all tokens
        prefix_index_short: dict[str, set[str]] = defaultdict(set)
        for c in candidates:
            for t in _tokenize(c):
                token_index[t].append(c)
                if len(t) >= 3:
                    prefix_index[t[:3]].add(t)
                elif len(t) == 2:
                    prefix_index_short[t].add(t)

        # Score candidates that share at least one token with the query
        scored: dict[str, float] = {}
        for qt in query_tokens:
            # Exact matches
            for c in token_index.get(qt, []):
                if c not in scored:
                    scored[c] = 0.0
            # Narrow scan for prefix matches by token length tier
            if len(qt) >= 3:
                candidate_tokens = prefix_index.get(qt[:3], set())
            elif len(qt) == 2:
                candidate_tokens = prefix_index_short.get(qt, set())
            else:
                candidate_tokens = set()  # 1-char: exact matches only
            for token in candidate_tokens:
                if token.startswith(qt) and token != qt:
                    for c in token_index.get(token, []):
                        if c not in scored:
                            scored[c] = 0.0

        # Score each shortlisted candidate
        results: list[tuple[str, float]] = []
        for c, _ in scored.items():
            c_tokens = _tokenize(c)
            score = _token_score(list(query_tokens), list(c_tokens))
            if score > 0:
                results.append((c, score))

        # Sort by score descending, then by name length (shorter = less noise)
        results.sort(key=lambda x: (-x[1], len(x[0])))

        matches = [r[0] for r in results[:limit]]

        with _cache_lock:
            if _cache:
                if len(_cache) >= _MAX_CACHE:
                    _cache.pop(next(iter(_cache)))
            _cache[cache_key] = (time.monotonic(), matches)

        return matches
    finally:
        with _cache_lock:
            _in_flight.discard(cache_key)


def _token_score(query_tokens: list[str], candidate_tokens: list[str]) -> float:
    """Score a candidate against query tokens.

    Exact token match: +2.0
    Prefix token match: +1.0
    Same-position match: +0.5 bonus

    Score is normalized against query token count so a candidate matching
    all tokens scores >= 2.0 regardless of query length.
    """
    if not query_tokens:
        return 0.0

    score = 0.0
    matched_any = False

    for qi, qt in enumerate(query_tokens):
        for ci, ct in enumerate(candidate_tokens):
            if ct == qt:
                score += 2.0
                if qi == ci:
                    score += 0.5  # same position bonus
                matched_any = True
                break
            elif ct.startswith(qt) and len(qt) >= 3:
                score += 1.0
                if qi == ci:
                    score += 0.5
                matched_any = True
                break

    return score if matched_any else 0.0


_names_cache: OrderedDict[str, list[str]] = OrderedDict()
_MAX_NAMES_CACHE = 8  # config_hash entries — rarely more than 1 per process


def _load_names(conn: sqlite3.Connection, config_hash: str) -> list[str]:
    """Load all definition names from the index for fuzzy matching.

    Only loads definition symbols (is_definition=1) of callable kinds to keep
    the set small and relevant — these are what users typically search for.
    Results are cached at module level per *config_hash* to avoid reloading
    on every uncached query.  Cache is LRU-bounded at _MAX_NAMES_CACHE entries.
    """
    if config_hash in _names_cache:
        _names_cache.move_to_end(config_hash)
        return _names_cache[config_hash]
    rows = conn.execute(
        """SELECT DISTINCT name FROM symbols
           WHERE config_hash = ?
             AND is_definition = 1
             AND kind IN ('function', 'method', 'constructor', 'destructor')
           ORDER BY name
           LIMIT 50000""",
        (config_hash,),
    ).fetchall()
    names = [r["name"] for r in rows]
    if len(_names_cache) >= _MAX_NAMES_CACHE:
        _names_cache.popitem(last=False)  # evict oldest (LRU)
    _names_cache[config_hash] = names
    return names
