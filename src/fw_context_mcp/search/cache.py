"""TTL cache for LLM-generated search keywords.

Avoids calling Ollama repeatedly for the same (query, config_hash) pair
within a session.
"""

from __future__ import annotations

import time


class KeywordCache:
    """Simple TTL cache for (query, config_hash) → list[str] mappings.

    Evicts entries older than *ttl_s* seconds on access.  When the cache
    exceeds *max_entries*, the oldest entries are evicted first.
    """

    def __init__(self, ttl_s: int = 300, max_entries: int = 256) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._store: dict[tuple, tuple[float, list[str]]] = {}

    def get(self, key: tuple) -> list[str] | None:
        """Return cached value, or None if missing / expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts > self._ttl_s:
            del self._store[key]
            return None
        return value

    def set(self, key: tuple, value: list[str]) -> None:
        """Store a value, evicting oldest if at capacity."""
        if len(self._store) >= self._max_entries:
            # Evict oldest entry by timestamp
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# Session-level caches
keyword_cache = KeywordCache()
