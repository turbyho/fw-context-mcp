"""HTTP client for the shared LLM analysis cache server.

``CacheClient`` communicates with a remote ``fw-cache-server`` instance
via HTTP, providing batch get/put operations with retry logic and
graceful offline fallback.  It also manages a local SQLite cache
(``~/.fw-context/llm_cache.db``) that serves as a first-tier lookup
before hitting the network.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from fw_context_mcp.utils import SAFE_EXCEPT, is_fatal
from typing import Any

logger = logging.getLogger(__name__)

_LOCAL_CACHE_DIR = Path.home() / ".fw-context"
_LOCAL_CACHE_PATH = _LOCAL_CACHE_DIR / "llm_cache.db"
_BATCH_SIZE = 100
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.5  # exponential backoff multiplier


def get_local_cache_db(readonly: bool = False) -> sqlite3.Connection:
    """Open (or create) the local cross-project LLM analysis cache.

    Returns a SQLite connection to ``~/.fw-context/llm_cache.db``.
    The database and schema are created on first access.
    """
    _LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # SQLite mode=ro (read-only) requires the file to already exist.
    # If the cache DB hasn't been created yet, silently switch to rwc
    # mode so the file is created and the schema is initialized.
    if readonly and not _LOCAL_CACHE_PATH.exists():
        readonly = False
    uri = f"file:{_LOCAL_CACHE_PATH}?mode={'ro' if readonly else 'rwc'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS llm_analysis_cache (
            content_hash TEXT PRIMARY KEY,
            summary      TEXT NOT NULL,
            inputs       TEXT NOT NULL,
            outputs      TEXT NOT NULL,
            model        TEXT NOT NULL,
            analyzed_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    return conn


def local_cache_lookup(conn: sqlite3.Connection, hashes: list[str]) -> dict[str, dict | None]:
    """Look up content hashes in the local SQLite cache.

    Returns a dict mapping each hash to the cached entry or ``None``.
    """
    if not hashes:
        return {}
    placeholders = ",".join("?" * len(hashes))
    rows = conn.execute(
        f"SELECT content_hash, summary, inputs, outputs, model, analyzed_at "
        f"FROM llm_analysis_cache WHERE content_hash IN ({placeholders})",
        hashes,
    ).fetchall()
    result: dict[str, dict | None] = {h: None for h in hashes}
    for r in rows:
        result[r[0]] = {
            "summary": r[1], "inputs": r[2], "outputs": r[3],
            "model": r[4], "analyzed_at": r[5],
        }
    return result


def local_cache_upsert(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """Insert or ignore entries into the local cache.

    Returns the number of rows inserted (not updated — uses INSERT OR IGNORE).
    """
    inserted = 0
    for e in entries:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO llm_analysis_cache "
                "(content_hash, summary, inputs, outputs, model) VALUES (?, ?, ?, ?, ?)",
                (e["hash"], e["summary"], e["inputs"], e["outputs"], e["model"]),
            )
            if cur.rowcount:
                inserted += 1
        except SAFE_EXCEPT as e:
            if is_fatal(e): raise
            logger.debug("local_cache_upsert: failed to insert entry", exc_info=True)
    conn.commit()
    return inserted


def local_cache_clear() -> int:
    """Delete the entire local cache database.  Returns 0 on success, 1 on error."""
    if _LOCAL_CACHE_PATH.exists():
        try:
            _LOCAL_CACHE_PATH.unlink()
            return 0
        except OSError:
            return 1
    return 0


def local_cache_stats() -> dict[str, Any]:
    """Return statistics about the local cache."""
    if not _LOCAL_CACHE_PATH.exists():
        return {"total_entries": 0, "path": str(_LOCAL_CACHE_PATH)}
    conn = get_local_cache_db(readonly=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM llm_analysis_cache").fetchone()[0]
        return {"total_entries": total, "path": str(_LOCAL_CACHE_PATH)}
    finally:
        conn.close()


class CacheClient:
    """HTTP client for the shared LLM analysis cache server.

    Communicates with a remote ``fw-cache-server`` instance for batch
    lookups and writes.  Gracefully handles connection errors — failures
    are logged and treated as cache misses (non-fatal).

    Attributes:
        url: Base URL of the cache server (e.g. ``https://fw-cache.example.com``).
        token: Bearer token for authentication.
        timeout: HTTP request timeout in seconds.
        force: When ``True``, sends the ``X-Cache-Overwrite`` header on
            write requests (requires ``can_overwrite`` on the server).
        batch_size: Maximum number of hashes/entries per request.
    """

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = 30.0,
        force: bool = False,
        batch_size: int = _BATCH_SIZE,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.force = force
        self.batch_size = batch_size
        self._session: Any = None
        self._connected = False
        self._can_write: bool | None = None
        self._can_overwrite: bool | None = None

    def _ensure_capabilities(self) -> None:
        """Discover token permissions via ``stats()``, once per client.

        Called lazily on the first write attempt.  When the client is
        created from config where the token may be read-only, the first
        ``batch_put`` / ``clear_remote`` triggers a ``stats()`` call to
        discover ``can_write``.  Subsequent writes are silently skipped
        when the token is read-only — no wasted network round-trips.
        """
        if self._can_write is None:
            self.stats()

    @classmethod
    def from_config(cls, cfg: object) -> CacheClient | None:
        """Create a CacheClient from a config object if cache_server URL is set.

        Returns ``None`` when ``cache_server`` is not configured or the
        URL is empty — no special config class dependency, works with
        any config object that has a ``cache_server`` attribute.
        """
        from .config.settings import CacheServerConfig

        cs: CacheServerConfig | None = getattr(cfg, "cache_server", None)
        if cs is not None and cs.url:
            try:
                return cls(
                    url=cs.url,
                    token=cs.token,
                    force=cs.force,
                    batch_size=cs.batch_size,
                )
            except (ValueError, TypeError, AttributeError) as e:
                logger.debug("CacheClient.from_config: failed to create client — %s", e)
                return None
        return None

    def _get_session(self) -> Any:
        """Lazy-init an httpx client with keep-alive."""
        if self._session is None:
            import httpx
            self._session = httpx.Client(
                base_url=self.url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.token}"},
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def batch_get(self, hashes: list[str]) -> dict[str, dict | None]:
        """Batch lookup on the remote server.

        Retries up to ``_MAX_RETRIES`` times with exponential backoff.
        On persistent failure, returns a dict with all hashes mapped to
        ``None`` (graceful offline fallback).
        """
        if not hashes:
            return {}

        # Chunk into batch_size to avoid oversized requests
        results: dict[str, dict | None] = {}
        for chunk_start in range(0, len(hashes), self.batch_size):
            chunk = hashes[chunk_start:chunk_start + self.batch_size]
            chunk_results = self._batch_get_chunk(chunk)
            results.update(chunk_results)
        return results

    def _batch_get_chunk(self, hashes: list[str]) -> dict[str, dict | None]:
        """Internal: POST one chunk of hashes, with retries."""
        for attempt in range(_MAX_RETRIES):
            try:
                session = self._get_session()
                resp = session.post("/cache/batch", json={"hashes": hashes})
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("results", {})
                if resp.status_code in (401, 403):
                    logger.warning("Cache server auth error (%d) — check token", resp.status_code)
                    return {h: None for h in hashes}
                # 5xx or 429 — retry with backoff
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    logger.debug("Cache server returned %d (attempt %d/%d), retrying in %.1fs",
                                 resp.status_code, attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server returned %d after %d retries — giving up",
                              resp.status_code, _MAX_RETRIES)
            except (httpx.HTTPError, OSError) as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    logger.debug("Cache server request failed (attempt %d/%d): %s",
                                 attempt + 1, _MAX_RETRIES, e)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server unreachable — continuing offline: %s", e)

        # All retries exhausted
        return {h: None for h in hashes}

    # NOTE: retry logic treats all 5xx errors the same.  Ideally, 400/413/422
    # should not be retried (client error), 429 should respect Retry-After,
    # and only 500/502/503 should use exponential backoff.
    def batch_put(self, entries: list[dict]) -> int:
        """Batch write to the remote server.

        Retries on transient errors.  With ``self.force=True``, sends the
        ``X-Cache-Overwrite`` header (requires ``can_overwrite`` on server).

        When the token is known to be read-only (``_can_write is False``),
        skips the write entirely and returns 0 — no network round-trip.

        Returns the number of entries inserted (best-effort — may be 0
        if the server is unreachable or the token is read-only).
        """
        if not entries:
            return 0

        self._ensure_capabilities()
        if self._can_write is False:
            logger.debug("Skipping remote cache write — token is read-only (%d entries)", len(entries))
            return 0

        total_inserted = 0
        for chunk_start in range(0, len(entries), self.batch_size):
            chunk = entries[chunk_start:chunk_start + self.batch_size]
            total_inserted += self._batch_put_chunk(chunk)
        return total_inserted

    def _batch_put_chunk(self, chunk: list[dict]) -> int:
        """Internal: PUT one chunk of entries, with retries."""
        headers = {}
        if self.force:
            headers["X-Cache-Overwrite"] = "true"

        payload = {"entries": [
            {"hash": e["hash"], "summary": e["summary"], "inputs": e["inputs"],
             "outputs": e["outputs"], "model": e["model"]}
            for e in chunk
        ]}

        for attempt in range(_MAX_RETRIES):
            try:
                session = self._get_session()
                resp = session.put("/cache/batch", json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("inserted", 0)
                if resp.status_code in (401, 403):
                    self._can_write = False
                    logger.warning("Cache server auth error (%d) on write — token is read-only", resp.status_code)
                    return 0
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    logger.debug("Cache server write returned %d (attempt %d/%d), retrying in %.1fs",
                                 resp.status_code, attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server write returned %d after %d retries — giving up",
                              resp.status_code, _MAX_RETRIES)
            except (httpx.HTTPError, OSError) as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server write failed: %s", e)

        return 0

    def stats(self) -> dict[str, Any] | None:
        """Fetch cache statistics from the remote server.

        Returns a dict with ``total_entries``, ``newest_entry``, ``oldest_entry``,
        ``models`` breakdown, plus ``can_read``, ``can_write``, ``can_overwrite``
        from the server's token permission check.  Stores the write capability
        internally so subsequent ``batch_put`` / ``clear_remote`` calls are
        silently skipped when the token is read-only.

        Returns ``None`` if the server is unreachable.
        """
        for attempt in range(_MAX_RETRIES):
            try:
                session = self._get_session()
                resp = session.get("/cache/stats")
                if resp.status_code == 200:
                    data = resp.json()
                    self._can_write = data.get("can_write", False)
                    self._can_overwrite = data.get("can_overwrite", False)
                    return data
                if resp.status_code in (401, 403):
                    logger.warning("Cache server auth error (%d) on stats — check token", resp.status_code)
                    return None
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
            except (httpx.HTTPError, OSError) as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server stats request failed: %s", e)

        return None

    def clear_remote(self, hashes: list[str]) -> int:
        """Delete cache entries from the remote server by content hash.

        Retries on transient errors. When the token is known to be
        read-only, skips the request entirely.
        """
        if not hashes:
            return 0

        self._ensure_capabilities()
        if self._can_write is False:
            logger.debug("Skipping remote cache clear — token is read-only (%d hashes)", len(hashes))
            return 0

        total_deleted = 0
        for chunk_start in range(0, len(hashes), self.batch_size):
            chunk = hashes[chunk_start:chunk_start + self.batch_size]
            total_deleted += self._clear_remote_chunk(chunk)
        return total_deleted

    def _clear_remote_chunk(self, hashes: list[str]) -> int:
        """Internal: POST one chunk of hashes to /cache/clear, with retries."""
        for attempt in range(_MAX_RETRIES):
            try:
                session = self._get_session()
                resp = session.post("/cache/clear", json={"hashes": hashes})
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("deleted", 0)
                if resp.status_code in (401, 403):
                    self._can_write = False
                    logger.warning("Cache server auth error (%d) on clear — token is read-only", resp.status_code)
                    return 0
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
            except (httpx.HTTPError, OSError) as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server clear failed: %s", e)

        return 0
