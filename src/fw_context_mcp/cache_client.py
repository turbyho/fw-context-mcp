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
            conn.execute(
                "INSERT OR IGNORE INTO llm_analysis_cache "
                "(content_hash, summary, inputs, outputs, model) VALUES (?, ?, ?, ?, ?)",
                (e["hash"], e["summary"], e["inputs"], e["outputs"], e["model"]),
            )
            if conn.total_changes > inserted:
                inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


def local_cache_clear() -> int:
    """Delete the local cache database.  Returns 0 on success, 1 on error."""
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


def migrate_from_index_db(index_db_path: Path) -> int:
    """Copy ``llm_analysis_cache`` rows from a per-project ``index.db``
    to the local global cache.  Deduplicates by content_hash.

    Returns the number of rows migrated.
    """
    if not index_db_path.exists():
        return 0
    try:
        src = sqlite3.connect(str(index_db_path))
        rows = src.execute(
            "SELECT content_hash, summary, inputs, outputs, model FROM llm_analysis_cache"
        ).fetchall()
        src.close()
    except Exception:
        return 0

    if not rows:
        return 0

    local = get_local_cache_db()
    inserted = 0
    for r in rows:
        try:
            local.execute(
                "INSERT OR IGNORE INTO llm_analysis_cache "
                "(content_hash, summary, inputs, outputs, model) VALUES (?, ?, ?, ?, ?)",
                r,
            )
            inserted += 1
        except Exception:
            pass
    local.commit()
    local.close()
    logger.info("Migrated %d cache entries from %s", inserted, index_db_path)
    return inserted


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
                    time.sleep(wait)
                    continue
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    logger.debug("Cache server request failed (attempt %d/%d): %s",
                                 attempt + 1, _MAX_RETRIES, e)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server unreachable — continuing offline: %s", e)

        # All retries exhausted
        return {h: None for h in hashes}

    def batch_put(self, entries: list[dict]) -> int:
        """Batch write to the remote server.

        Retries on transient errors.  With ``self.force=True``, sends the
        ``X-Cache-Overwrite`` header (requires ``can_overwrite`` on server).

        Returns the number of entries inserted (best-effort — may be 0
        if the server is unreachable).
        """
        if not entries:
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
                    logger.warning("Cache server auth error (%d) on write — check token", resp.status_code)
                    return 0
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_BACKOFF ** (attempt + 1)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server write failed: %s", e)

        return 0
