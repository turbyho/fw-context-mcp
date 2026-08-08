"""HTTP client for the shared LLM analysis cache server.

``CacheClient`` communicates with a remote ``fw-cache-server`` instance
via HTTP, providing batch get/put operations with retry logic and
graceful offline fallback.  It also manages a local SQLite cache
(``~/.fw-context/llm_cache.db``) that serves as a first-tier lookup
before hitting the network.

Why a local + remote two-tier cache?
------------------------------------
1. **Local SQLite** — instant lookups (no network), survives offline
   operation.  Each developer's machine caches analyses they've already
   seen.  This is the FIRST tier — local hits avoid any network cost.

2. **Remote PostgreSQL** (via HTTP) — shared across all developers.
   When the local cache misses, the client queries the server.  On hit,
   the result is saved to the local SQLite for future instant access.
   This is the SECOND tier — network cost only on first encounter of
   a symbol analysis.

3. **LLM fallback** — when both caches miss, ``fw-context index --analyze``
   calls the LLM to generate a fresh analysis.  The result is pushed to
   BOTH caches (local + remote) for future reuse.

Why httpx (not requests / aiohttp)?
-----------------------------------
httpx provides both sync and async clients with a shared API.  The cache
client uses *sync* httpx because ``fw-context-mcp``'s indexer runs
synchronously (subprocess calls, not an async event loop).  httpx is the
only modern HTTP library that supports HTTP/2, connection pooling, and
timeouts in sync mode — requests library lacks connection pooling and
has poor timeout semantics.

Retry strategy
--------------
- Up to 3 retries with exponential backoff (1.5×, 2.25×, 3.375× seconds).
- Retryable: 429 (rate limit), 5xx (server errors), connection errors.
- Non-retryable: 400, 401, 403, 404, 413, 422 (client errors — retrying
  would not help).
- ``Retry-After`` header is respected for 429 responses — the server
  dictates the wait time.

Token capability discovery
--------------------------
On the first write or stats call, the client discovers whether its token
has ``can_write``.  If not, subsequent write calls are silently skipped
(saving round-trips to a server that will only return 403).  This is
important for read-only tokens (the default for developers) — the
client should not waste network calls on writes it knows will fail.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from fw_context_mcp.utils import SAFE_EXCEPT, is_fatal

logger = logging.getLogger(__name__)

_LOCAL_CACHE_DIR = Path.home() / ".fw-context"
_LOCAL_CACHE_PATH = _LOCAL_CACHE_DIR / "llm_cache.db"
_BATCH_SIZE = 100
"""Default batch size for splitting large request lists into chunks.

Why 100?  PostgreSQL's ``ANY($1::text[])`` with prepared statements
handles 100 parameters efficiently.  Larger batches (>500) increase
query plan cache pressure in asyncpg.  Smaller batches (<50) cause
too many round-trips — overhead dominates for bulk operations.
"""

_MAX_RETRIES = 3
"""Maximum retry attempts for transient HTTP errors.

Why 3?  With exponential backoff (1.5×), total worst-case wait is
~7 seconds (1.5 + 2.25 + 3.375).  More retries would increase
blocking time in the synchronous indexer pipeline without meaningfully
improving success rates — persistent failures (DNS resolution,
connection refused) won't resolve with more retries.
"""

_RETRY_BACKOFF = 1.5  # exponential backoff multiplier
"""Base multiplier for backoff: retry 1 → 1.5 s, retry 2 → 2.25 s, retry 3 → 3.375 s."""


# HTTP status codes that should NOT be retried (client errors).
# These indicate a problem with the request itself — retrying would
# produce the same error.
_NON_RETRYABLE = frozenset({400, 401, 403, 404, 413, 422})

# HTTP status codes that should be retried with backoff.
# These indicate a transient server-side issue that may resolve.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


def _retry_sleep(attempt: int) -> float:
    """Compute exponential backoff sleep duration.

    Returns ``base ** (attempt + 1)``.  Attempt is 0-indexed:
    attempt 0 → base^1, attempt 1 → base^2, attempt 2 → base^3.
    The +1 avoids a 1-second backoff on the first retry.
    """
    return _RETRY_BACKOFF ** (attempt + 1)


def _get_retry_after(resp, default: float) -> float:
    """Extract ``Retry-After`` header value, falling back to *default*.

    Respects server-specified delay for 429 Rate Limit responses.
    Parses both integer-second and HTTP-date formats.

    Why parse HTTP-date format?
    ---------------------------
    RFC 7231 allows ``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``.
    Some reverse proxies (nginx, Cloudflare) use this format instead
    of plain seconds.  We must handle both to avoid a ValueError
    crashing the retry loop.
    """
    value = resp.headers.get("Retry-After", "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        # HTTP-date format — parse with email.utils
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            import time as _time_module
            now = _time_module.time()
            from calendar import timegm
            wait = timegm(dt.utctimetuple()) - now
            return max(0.0, wait)
        except (ValueError, TypeError, ImportError):
            return default


def get_local_cache_db(readonly: bool = False) -> sqlite3.Connection:
    """Open (or create) the local cross-project LLM analysis cache.

    Returns a SQLite connection to ``~/.fw-context/llm_cache.db``.
    The database and schema are created on first access.

    Why WAL journal mode?
    ---------------------
    WAL (Write-Ahead Logging) allows concurrent reads while a write
    is in progress — readers don't block writers.  Without WAL, a
    read during a cache write would get ``SQLITE_BUSY``.  WAL also
    improves write throughput because it appends to the WAL file
    instead of rewriting pages in-place.

    Why ``busy_timeout=5000``?
    --------------------------
    Even with WAL, concurrent writes can conflict.  A 5-second busy
    timeout means SQLite retries for 5 seconds before giving up with
    ``SQLITE_BUSY`` — generous enough for the expected workload
    (single process, batch writes of ≤100 entries).

    Why treat readonly=True on a non-existent file as rwc?
    -------------------------------------------------------
    SQLite's ``mode=ro`` requires the file to exist — it won't create
    it.  If the cache DB hasn't been created yet (first run), silently
    switch to ``rwc`` mode so the file is created and schema is
    initialized.  Subsequent calls can use ``mode=ro`` normally.
    """
    _LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if readonly and not _LOCAL_CACHE_PATH.exists():
        readonly = False
    uri = f"file://{_LOCAL_CACHE_PATH}?mode={'ro' if readonly else 'rwc'}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        _LOCAL_CACHE_PATH.chmod(0o600)
    except OSError:
        pass
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

    Why pre-populate with None for all hashes?
    ------------------------------------------
    The caller expects a result for every input hash.  Pre-populating
    with None, then filling in hits, guarantees this contract even
    when no rows are found.  This matches the remote ``batch_get``
    API behavior (``{hash: entry | None}``).
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

    Why INSERT OR IGNORE (not INSERT OR REPLACE)?
    ---------------------------------------------
    The local cache follows the same "first write wins" semantics as
    the remote server.  If an entry already exists (from a previous
    llm-analyze run or a remote cache pull), we should NOT overwrite
    it with a potentially different analysis from a later run.  This
    prevents cache churn and preserves the first (most trustworthy)
    analysis.

    Why per-entry error handling?
    -----------------------------
    A single corrupt entry should not abort the entire batch.  Each
    entry is inserted individually with a try/except — one bad entry
    (e.g. invalid hash, excessively long text) is logged and skipped
    while the remaining entries are inserted normally.
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
            if is_fatal(e):
                raise
            logger.debug("local_cache_upsert: failed to insert entry", exc_info=True)
    conn.commit()
    return inserted


def local_cache_clear() -> int:
    """Delete the entire local cache database.  Returns 0 on success, 1 on error.

    Why delete the file (not DROP TABLE)?
    -------------------------------------
    Deleting the file is atomic (one syscall) and guaranteed to free
    disk space immediately.  ``DROP TABLE`` inside SQLite frees pages
    but the file size remains unchanged — you'd need ``VACUUM`` after
    to reclaim disk space.  Unlinking the file is simpler and faster.
    On next access, ``get_local_cache_db()`` recreates the file with
    a fresh schema.
    """
    if _LOCAL_CACHE_PATH.exists():
        try:
            _LOCAL_CACHE_PATH.unlink()
            return 0
        except OSError:
            return 1
    return 0


def local_cache_stats() -> dict[str, Any]:
    """Return statistics about the local cache.

    Why return the file path?
    -------------------------
    The path helps operators locate the cache file for inspection,
    backup, or manual deletion.  SQLite databases are opaque from
    the filesystem — including the path in stats output gives the
    operator a direct handle to the file.
    """
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
        """Tri-state: None (unknown — not checked yet), True (token allows writes), False (read-only)."""
        self._can_overwrite: bool | None = None

    def _ensure_capabilities(self) -> None:
        """Discover token permissions via ``stats()``, once per client.

        Called lazily on the first write attempt.  When the client is
        created from config where the token may be read-only, the first
        ``batch_put`` / ``clear_remote`` triggers a ``stats()`` call to
        discover ``can_write``.  Subsequent writes are silently skipped
        when the token is read-only — no wasted network round-trips.

        Why lazy discovery?
        -------------------
        Most clients on developer machines have read-only tokens —
        they never write to the remote cache.  Eagerly calling stats()
        on every ``CacheClient`` creation would be wasteful.  Lazy
        discovery defers the network call until the first write attempt,
        which may never happen for read-only clients.
        """
        if self._can_write is None:
            self.stats()

    @classmethod
    def from_config(cls, cfg: object) -> CacheClient | None:
        """Create a CacheClient from a config object if cache_server URL is set.

        Returns ``None`` when ``cache_server`` is not configured or the
        URL is empty — no special config class dependency, works with
        any config object that has a ``cache_server`` attribute.

        Why accept ``object`` (not a specific config class)?
        ----------------------------------------------------
        The cache client is used from multiple code paths (indexer,
        analyzer, CLI) that may have different config class hierarchies.
        Using ``object`` with ``getattr`` avoids a hard dependency on
        any specific config class — any object with a ``cache_server``
        attribute works, which simplifies testing and decoupling.
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
        """Lazy-init an httpx client with keep-alive.

        Why lazy initialization?
        ------------------------
        The session holds a connection pool.  Creating it at ``__init__``
        would open connections even if the client is never used (common
        for read-only clients that only use the local SQLite cache).
        Lazy init defers connection creation until the first actual
        HTTP call.

        Why keep-alive connections?
        ---------------------------
        The cache client makes batch requests in rapid succession
        (lookup → analyze → write).  Without keep-alive, each request
        does a new TCP + TLS handshake (~50-100 ms overhead).  With
        keep-alive, subsequent requests reuse the same connection
        (<<1 ms overhead).  Over hundreds of batch chunks, this
        saves seconds of wall-clock time.
        """
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
        """Close the httpx session and release connections.

        Should be called when the client is no longer needed.  Without
        explicit close, the session's connection pool holds TCP connections
        until garbage collection — which may be minutes later in long-
        running processes like the indexer.
        """
        if self._session is not None:
            self._session.close()
            self._session = None

    def _retry_http_call(self, http_call, *, label="request", on_auth_failure=None):
        """Execute *http_call* with retry + exponential backoff.

        Returns the ``Response`` on success (status 200), ``None`` on
        persistent failure or auth error (401/403).  Calls
        *on_auth_failure* before returning ``None`` on 401/403 —
        callers can use this to mark the token as read-only.
        Respects ``Retry-After`` header for 429 responses.

        Why call on_auth_failure on 401/403?
        ------------------------------------
        When a write fails with 403, the token is likely read-only.
        The caller passes a callback that sets ``_can_write = False``
        so subsequent write attempts are silently skipped — no wasted
        round-trips.  This is the mechanism that enables the lazy
        capability discovery pattern.
        """
        for attempt in range(_MAX_RETRIES):
            try:
                resp = http_call()
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (401, 403):
                    if on_auth_failure is not None:
                        on_auth_failure()
                    return None
                # Non-retryable client errors (404, 413, 422, 400) — fail immediately.
                # These indicate a permanent problem with the request —
                # retrying would produce the same error.
                if resp.status_code in _NON_RETRYABLE:
                    logger.warning("Cache server %s returned non-retryable %d",
                                   label, resp.status_code)
                    return None
                # Retryable errors (429, 5xx) with backoff.
                # 429: rate limited — wait for Retry-After or backoff.
                # 5xx: transient server error — may resolve on retry.
                if attempt < _MAX_RETRIES - 1:
                    wait = _get_retry_after(resp, _retry_sleep(attempt))
                    logger.debug("Cache server %s returned %d (attempt %d/%d), retrying",
                                 label, resp.status_code, attempt + 1, _MAX_RETRIES)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server %s returned %d after %d retries",
                               label, resp.status_code, _MAX_RETRIES)
            except (httpx.HTTPError, OSError) as e:
                # Connection errors (DNS, timeout, connection refused) —
                # these are transient and worth retrying.
                if attempt < _MAX_RETRIES - 1:
                    wait = _retry_sleep(attempt)
                    logger.debug("Cache server %s failed (attempt %d/%d): %s",
                                 label, attempt + 1, _MAX_RETRIES, e)
                    time.sleep(wait)
                    continue
                logger.warning("Cache server unreachable on %s: %s", label, e)
        return None

    def batch_get(self, hashes: list[str]) -> dict[str, dict | None]:
        """Batch lookup on the remote server.

        Retries up to ``_MAX_RETRIES`` times with exponential backoff.
        On persistent failure, returns a dict with all hashes mapped to
        ``None`` (graceful offline fallback).

        Why split into chunks?
        ----------------------
        Large hash lists (1000+) are split into ``batch_size`` chunks
        (default 100).  This keeps individual request sizes manageable
        and allows partial results — if one chunk fails (server error
        on chunk 3 of 10), chunks 1, 2, and 4-10 still succeed.  The
        caller gets partial results instead of all-None.
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
        """Internal: POST one chunk of hashes, with retries.

        On network failure, returns ``{hash: None}`` for all hashes in
        the chunk — the caller merges this into the full result dict
        and continues with the next chunk.
        """
        resp = self._retry_http_call(
            lambda: self._get_session().post("/cache/batch", json={"hashes": hashes}),
            label="batch_get"
        )
        if resp is None:
            return {h: None for h in hashes}
        data = resp.json()
        return data.get("results", {})

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
        """Internal: PUT one chunk of entries, with retries.

        On 403 (forbidden), calls ``_on_auth()`` which sets
        ``_can_write = False`` — subsequent put attempts are
        silently skipped.
        """
        headers = {}
        if self.force:
            headers["X-Cache-Overwrite"] = "true"

        payload = {"entries": [
            {"hash": e["hash"], "summary": e["summary"], "inputs": e["inputs"],
             "outputs": e["outputs"], "model": e["model"]}
            for e in chunk
        ]}

        def _on_auth():
            """Mark token as read-only when write auth fails."""
            self._can_write = False

        resp = self._retry_http_call(
            lambda: self._get_session().put("/cache/batch", json=payload, headers=headers),
            label="batch_put",
            on_auth_failure=_on_auth
        )
        if resp is None:
            return 0
        data = resp.json()
        return data.get("inserted", 0)

    def stats(self) -> dict[str, Any] | None:
        """Fetch cache statistics from the remote server.

        Returns a dict with ``total_entries``, ``newest_entry``, ``oldest_entry``,
        ``models`` breakdown, plus ``can_read``, ``can_write``, ``can_overwrite``
        from the server's token permission check.  Stores the write capability
        internally so subsequent ``batch_put`` / ``clear_remote`` calls are
        silently skipped when the token is read-only.

        Returns ``None`` if the server is unreachable.

        Why stats() as the capability discovery endpoint?
        -------------------------------------------------
        The server's ``GET /cache/stats`` response includes permission
        flags.  This is intentional: a single call tells the client
        everything it needs (cache health + its own capabilities).
        No separate "auth check" endpoint needed — the stats call
        serves double duty: cache monitoring AND capability discovery.
        """
        resp = self._retry_http_call(
            lambda: self._get_session().get("/cache/stats"),
            label="stats"
        )
        if resp is None:
            return None
        data = resp.json()
        self._can_write = data.get("can_write", False)
        self._can_overwrite = data.get("can_overwrite", False)
        return data

    def clear_remote(self, hashes: list[str]) -> int:
        """Delete cache entries from the remote server by content hash.

        Retries on transient errors. When the token is known to be
        read-only, skips the request entirely.

        Why is clear_remote a client method?
        -----------------------------------
        Cache invalidation happens during re-indexing — when a source
        file changes, its symbol analyses become stale and must be
        re-generated by the LLM.  Deleting old entries before re-analysis
        ensures the new analyses aren't blocked by "first write wins"
        semantics (existing entries would prevent insertion).
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
        def _on_auth():
            self._can_write = False

        resp = self._retry_http_call(
            lambda: self._get_session().post("/cache/clear", json={"hashes": hashes}),
            label="clear",
            on_auth_failure=_on_auth
        )
        if resp is None:
            return 0
        data = resp.json()
        return data.get("deleted", 0)
