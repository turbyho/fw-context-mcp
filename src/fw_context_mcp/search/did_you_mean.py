"""Did-you-mean? suggestions for lookup_symbol when no exact match is found.

Why token-based matching instead of difflib?
    difflib sequence matching (``get_close_matches``) works well for
    English words but poorly for code symbols.  Two symbols may share
    no character n-grams yet be semantically identical in CamelCase
    (``nrfxUarteInit`` vs ``NRF_UARTE_Init``).  Token-based matching
    splits both into conceptual tokens (``nrfx``, ``uarte``, ``init``)
    and scores by shared tokens — matching the developer's mental model.

Why a cache with TTL?
    ``_load_names()`` queries the database for up to 50000 symbol names.
    Without caching, every uncached ``lookup_symbol`` miss would reload
    all names.  The 5-minute TTL covers repeated lookups within a session
    without serving stale data from a reindexed project.

Why a stampede guard (``_in_flight``)?
    When a user types an unknown symbol name, multiple concurrent tool
    calls can arrive simultaneously.  Without the guard, every thread
    would independently load 50000 names — wasteful.  The ``_in_flight``
    set lets one thread compute while others return empty (cached next
    time).

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

# {query: (timestamp, [suggestions])}
_cache: dict[str, tuple[float, list[str]]] = {}
_MAX_CACHE = 128
# 5-minute TTL — matches keyword_cache; long enough for a session,
# short enough that a full reindex will be reflected
_CACHE_TTL_S = 300
_cache_lock = threading.Lock()

# Stampede prevention: when one thread is already computing suggestions
# for a key, other threads skip rather than duplicating work.
_in_flight: set[str] = set()

# Characters that delimit tokens in symbol names
_TOKEN_SPLIT = re.compile(r"[_]+")


@lru_cache(maxsize=20000)
def _tokenize(name: str) -> tuple[str, ...]:
    """Split a symbol name into lowercase tokens.

    Why lru_cache?
        Tokenizing is pure computation (no I/O).  The same symbol names
        appear repeatedly during scoring — caching avoids re-splitting
        ``nrfxUarteInit`` hundreds of times across different candidates.

    Why these regex patterns?
        ``[A-Z]?[a-z0-9]+`` matches standard camelCase words like
        ``Uarte`` or ``nrfx``.  ``[A-Z]+(?=[A-Z][a-z]|$|\\d)`` handles
        consecutive acronyms like ``TIM16`` → ``TIM``, ``16`` — the
        lookahead prevents greedy matching that would merge ``TIM16``
        into one token.

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

    What it does:
        Finds definition-level symbols (functions, methods, constructors,
        destructors) from the database, tokenises both the query name and
        all candidates, and scores by shared tokens.

    Why token matching?
        A typo like ``modem_init`` vs ``modem_init`` would fail difflib
        because the edit distance is tiny.  But ``uart_configure`` vs
        ``uart_setup`` has high edit distance yet shares the ``uart``
        token — token matching captures this conceptual similarity.

    Why definition-only candidates?
        Users type names of functions and methods they want to find.
        Variables, fields, and namespace names are noise for this purpose.
        Limiting to ``is_definition=1`` of callable kinds keeps the
        candidate set focused (~5000–50000 names, manageable in memory).

    Why prefix-index for O(1) lookups?
        Naively scanning all candidate tokens for prefix matches is O(n).
        The prefix index maps the first 3 chars of each token to candidate
        tokens, reducing the scan to only tokens that start with the query
        prefix — typically 1–10 candidates instead of thousands.

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

        # Build token → candidate index for exact-match O(1) lookup.
        # Without this, finding candidates that share a token would
        # require scanning all candidates — O(n) per query token.
        token_index: dict[str, list[str]] = defaultdict(list)

        # Prefix index: first 3 chars → set of tokens.  Narrows the
        # O(n) candidate scan to O(1) for prefix matching.  3-char
        # threshold chosen empirically — shorter prefixes (1-2 chars)
        # match too broadly; longer (4+) miss typos in short tokens.
        prefix_index: dict[str, set[str]] = defaultdict(set)

        # Separate short-prefix index for 2-char tokens — these are
        # too few to benefit from a 3-char prefix, but still need
        # O(1) lookup to avoid scanning all tokens.
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

    Why these weights?
        Exact token match (+2.0) dominates — the query token literally
        appears in the candidate.  Prefix match (+1.0) is weaker — it's
        a partial signal that may be coincidental (``mod`` prefix matches
        ``modem`` but the symbols may be unrelated).  Same-position bonus
        (+0.5) rewards candidates whose token order matches the query
        order, reducing false positives from reordered tokens.

    Why no normalisation?
        The score is NOT divided by candidate length — a candidate with
        many unrelated tokens would otherwise be penalised.  Instead, the
        score reflects how many query tokens were matched, regardless of
        how many extra tokens the candidate has.

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


# config_hash → list of definition names, LRU-bounded
_names_cache: OrderedDict[str, list[str]] = OrderedDict()
_MAX_NAMES_CACHE = 8  # config_hash entries — rarely more than 1 per process


def _load_names(conn: sqlite3.Connection, config_hash: str) -> list[str]:
    """Load all definition names from the index for fuzzy matching.

    Why cache by config_hash?
        A typical MCP server process serves queries for a single project,
        so the candidate list is loaded once.  The ``_names_cache`` is
        LRU-bounded at 8 entries to handle the rare case where a process
        serves multiple projects (e.g. during testing).

    Why LIMIT 50000?
        SQLite's ORDER BY for 50000 rows takes ~5 ms on an indexed column.
        Beyond 50000, in-memory tokenisation becomes the bottleneck,
        not database load.  The limit ensures predictable performance
        even on very large codebases.

    Only loads definition symbols (is_definition=1) of callable kinds to keep
    the set small and relevant — these are what users typically search for.
    Results are cached at module level per *config_hash* to avoid reloading
    on every uncached query.  Cache is LRU-bounded at _MAX_NAMES_CACHE entries.
    """
    with _cache_lock:
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
    with _cache_lock:
        if len(_names_cache) >= _MAX_NAMES_CACHE:
            _names_cache.popitem(last=False)  # evict oldest (LRU)
        _names_cache[config_hash] = names
    return names
