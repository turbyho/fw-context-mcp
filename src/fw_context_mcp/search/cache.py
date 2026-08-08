"""TTL cache for LLM-generated search keywords.

Why cache LLM responses?
    Calling Ollama to generate FTS5 search terms takes 2-10 seconds per
    query.  Users often repeat similar searches during a session ("modem
    init", then "modem connect", then "modem attach").  Without caching,
    each query variant would call Ollama again even for identical queries.

    The cache stores ``(query, config_hash)`` → ``(queries, understanding)``
    mappings with a 5-minute TTL.  This covers repeated searches within
    a session without serving stale results from a different build config.

Why an OrderedDict with TTL rather than functools.lru_cache?
    lru_cache is unbounded and process-global.  This cache is explicitly
    bounded (256 entries) with time-based eviction.  Different projects
    have different config_hashes — a process-global LRU cache would mix
    entries across projects.  The bounded TTL approach gives predictable
    memory usage and implicit project isolation via the config_hash key.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class KeywordCache:
    """Simple TTL cache for (query, config_hash) → list[str] mappings.

    Why bounded with TTL?
        Unbounded caches grow indefinitely in long-running MCP server
        processes.  The combination of max_entries (256) and TTL (300 s)
        bounds both memory and staleness.  LRU eviction via OrderedDict
        ensures frequently-accessed entries survive while one-shot queries
        are evicted quickly.

    Why thread-safe?
        The MCP server handles concurrent tool calls.  Multiple search
        requests can hit the cache simultaneously — the lock prevents
        data races on the OrderedDict.

    Evicts entries older than *ttl_s* seconds on access.  When the cache
    exceeds *max_entries*, the oldest entries are evicted first.

    Thread-safe — guarded by a re-entrant lock.

    Cache key is ``(query, config_hash)`` — a 2-element tuple.
    """
    # NOTE: get/set accept ``tuple`` (unparameterized) for caller convenience;
    # the internal store is ``OrderedDict[tuple[str, str], ...]``.

    def __init__(self, ttl_s: int = 300, max_entries: int = 256) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        # value = (ts, (queries, understanding))
        self._store: OrderedDict[tuple[str, str], tuple[float, tuple[list[str], str]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> tuple[list[str], str] | None:
        """Return cached (queries, understanding) or None if missing / expired.

        Why refresh LRU position on access?
            This implements a "read-through" LRU policy — every ``get()``
            moves the entry to the front of the OrderedDict, so entries
            that are actively used survive eviction.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self._ttl_s:
                del self._store[key]
                return None
            self._store.move_to_end(key)  # LRU refresh
            return value

    def set(self, key: tuple, queries: list[str], understanding: str = "") -> None:
        """Store (queries, understanding), evicting oldest if at capacity.

        Why popitem(last=False)?
            OrderedDict.popitem(last=False) evicts the Least-Recently-Used
            entry (oldest by insertion + access order).  This is standard
            LRU eviction — new entries push out the ones nobody has looked
            at recently.
        """
        with self._lock:
            if len(self._store) >= self._max_entries:
                # LRU eviction via OrderedDict — get() refreshes position,
                # popitem(last=False) evicts the oldest entry
                self._store.popitem(last=False)
            self._store[key] = (time.monotonic(), (queries, understanding))

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton — shared across all search invocations within the
# same process (MCP server or CLI).  One cache per process avoids redundant
# LLM calls across different tool invocations.
keyword_cache = KeywordCache()
