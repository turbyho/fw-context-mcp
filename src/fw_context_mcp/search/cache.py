"""TTL cache for LLM-generated search keywords.

Avoids calling Ollama repeatedly for the same (query, config_hash) pair
within a session.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
import time


class KeywordCache:
    """Simple TTL cache for (query, config_hash) → list[str] mappings.

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
        self._store: "OrderedDict[tuple[str, str], tuple[float, tuple[list[str], str]]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> tuple[list[str], str] | None:
        """Return cached (queries, understanding) or None if missing / expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self._ttl_s:
                del self._store[key]
                return None
            return value

    def set(self, key: tuple, queries: list[str], understanding: str = "") -> None:
        """Store (queries, understanding), evicting oldest if at capacity."""
        with self._lock:
            if len(self._store) >= self._max_entries:
                # FIFO eviction via OrderedDict — O(1) instead of O(n) min() scan
                self._store.popitem(last=False)
            self._store[key] = (time.monotonic(), (queries, understanding))

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# Session-level caches
keyword_cache = KeywordCache()
