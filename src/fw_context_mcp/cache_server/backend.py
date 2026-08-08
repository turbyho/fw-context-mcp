"""PostgreSQL backend for the shared LLM analysis cache.

Provides connection pools for the meta database (projects, tokens) and
the cache database (``llm_analysis_cache``), plus batch get/put operations.

Write behaviour is controlled by *can_overwrite*:
- ``False`` (default) — ``INSERT ON CONFLICT DO NOTHING`` (first write wins)
- ``True`` — ``INSERT ON CONFLICT DO UPDATE`` (overwrite with newer analysis)

Why PostgreSQL (not SQLite or Redis)?
-------------------------------------
1. **Concurrent writes** — multiple developers can push analyses
   simultaneously without SQLite's single-writer lock.
2. **Network access** — the cache server runs as a separate process;
   PostgreSQL is accessible over TCP (unlike SQLite which requires
   file-system access).
3. **Built-in types** — BYTEA for token hashes, TIMESTAMPTZ for
   timestamps, ON CONFLICT for upsert — all without extra libraries.
4. **Connection pooling** — asyncpg provides native async connection
   pools with prepared statement caching.

Why two databases (meta + cache)?
---------------------------------
Separation of concerns: the meta database (projects, tokens) has
different backup/restore and access patterns than the cache database
(high-volume reads/writes).  Separate databases also allow different
PostgreSQL configurations (e.g. cache DB can use aggressive
autovacuum settings).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Any

logger = logging.getLogger(__name__)


def _redact_url(url: str) -> str:
    """Replace password in a PostgreSQL URL with '****' for logging.

    PostgreSQL URLs contain the password in plain text
    (``postgresql://user:password@host/db``).  Logging them would
    leak credentials to log files, stdout, and error reporters.
    This function redacts only the password portion — the host and
    database name remain visible for debugging.
    """
    return re.sub(r'://([^:]+):([^@]+)@', r'://\1:****@', url)



class CacheStorageBackend:
    """Abstract interface for the LLM analysis cache storage.

    Implementations must provide async connection management and
    batch read/write operations.  The default implementation is
    ``CacheBackend`` which uses PostgreSQL via ``asyncpg``.

    To swap the storage backend (e.g. SQLite, Redis), implement
    this interface and pass your instance to ``create_app(backend=...)``.

    Why an abstract interface?
    --------------------------
    Testing: unit tests inject an in-memory dict backend instead of
    requiring a running PostgreSQL instance.  Production: organizations
    with existing Redis/MySQL infrastructure can swap the backend
    without changing any route or auth code.
    """

    async def connect(self) -> None:
        """Open connections to the storage backend. Must be called before any operations."""
        raise NotImplementedError

    async def close(self) -> None:
        """Close all connections. Must be called at shutdown to release resources."""
        raise NotImplementedError

    async def init_schema(self) -> None:
        """Create tables if they do not exist. Idempotent — safe to call on every startup.

        NOTE: checks is_nullable on every startup.  Could cache
        a schema_version flag to skip this after first migration.
        """
        raise NotImplementedError

    async def batch_get(self, hashes: list[str]) -> dict:
        """Look up multiple content hashes. Returns {hash: entry | None}."""
        raise NotImplementedError

    async def batch_put(self, entries: list, *, can_overwrite: bool = False) -> int:
        """Insert multiple entries. Returns count of rows inserted/updated."""
        raise NotImplementedError

    async def cache_stats(self) -> dict:
        """Return cache statistics: total entries, timestamps, model breakdown."""
        raise NotImplementedError

    async def cache_clear_by_hashes(self, hashes: list[str]) -> int:
        """Delete entries by content hash. Returns count deleted."""
        raise NotImplementedError


class CacheBackend(CacheStorageBackend):
    """Async PostgreSQL backend implementing the CacheStorageBackend interface.

    Manages two connection pools — one for ``fw_cache_meta`` and one for
    ``fw_cache`` — derived from a single *base_url* by appending the
    database name (e.g. ``postgresql://user:pass@localhost:5432`` +
    ``/fw_cache_meta`` and ``/fw_cache``).

    All methods are async — designed for FastAPI's async route handlers.

    Why asyncpg (not psycopg2)?
    ---------------------------
    asyncpg is ~3× faster than psycopg2 for bulk operations because it
    implements the PostgreSQL frontend/backend protocol directly in
    Python+Cython, avoiding libpq overhead.  It also supports binary
    protocol for prepared statements (psycopg2 uses text protocol),
    which matters for batch operations with 1000+ parameters.
    """

    def __init__(self, base_url: str, min_size: int = 2, max_size: int = 10):
        """*base_url* is the PostgreSQL connection string *without* a
        database component (e.g. ``postgresql://user:pass@localhost:5432``).
        The two individual pools will append ``/fw_cache_meta`` and
        ``/fw_cache`` respectively.

        *min_size* / *max_size* control the pool size for each pool.

        Why split pool sizes?
        --------------------
        The meta database has low traffic (token validation per request),
        so 2–5 connections suffice.  The cache database handles batch
        reads/writes which are heavier — 5–10 connections prevent
        queuing under concurrent developer load.
        """
        self._base_url = base_url.rstrip("/")
        self._min_size = min_size
        self._max_size = max_size
        self._meta_pool: Any = None
        self._cache_pool: Any = None
        self._connected = False

    @property
    def meta_url(self) -> str:
        """Full URL for the meta database."""
        return self._db_url("fw_cache_meta")

    @property
    def cache_url(self) -> str:
        """Full URL for the cache database."""
        return self._db_url("fw_cache")

    def _db_url(self, db_name: str) -> str:
        """Build a database-specific URL from the base connection string.

        Handles URLs with query parameters (e.g. ``?host=/tmp`` for
        Unix-socket connections) correctly — the database name is
        inserted before the query string.

        Why not just string concatenation?
        ---------------------------------
        PostgreSQL URLs can have query parameters after ``?``:
        ``postgresql://user:pass@localhost:5432?sslmode=require``.
        Appending ``/dbname`` after ``?`` would produce an invalid URL.
        This method splits on ``?`` and inserts the database name in
        the correct position.
        """
        base = self._base_url
        if "?" in base:
            prefix, params = base.split("?", 1)
            return f"{prefix}/{db_name}?{params}"
        else:
            return f"{base}/{db_name}"

    async def connect(self) -> None:
        """Create both connection pools (call once at startup).

        Why validate with ``SELECT 1``?
        -------------------------------
        ``asyncpg.create_pool`` is lazy — it doesn't actually connect
        until the first query.  Without explicit validation, bad
        credentials would cause the first *client* request to fail
        with a connection error, rather than failing fast at startup
        where the operator can see the error immediately.
        """
        import asyncpg

        self._meta_pool = await asyncpg.create_pool(
            self.meta_url, min_size=self._min_size, max_size=self._max_size,
            command_timeout=30,
        )
        self._cache_pool = await asyncpg.create_pool(
            self.cache_url, min_size=self._min_size, max_size=self._max_size,
            command_timeout=30,
        )
        # Validate connections — create_pool is lazy, this catches bad credentials early
        async with self._meta_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        async with self._cache_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        self._connected = True
        logger.info("CacheBackend connected — meta=%s cache=%s", _redact_url(self.meta_url), _redact_url(self.cache_url))

    async def close(self) -> None:
        """Close both pools (call at shutdown).

        Why close pools explicitly?
        ---------------------------
        asyncpg pools hold TCP connections to PostgreSQL.  If not closed
        at shutdown, the connections linger until the OS times them out
        (typically hours), wasting PostgreSQL server resources and
        connection slots.
        """
        if self._meta_pool:
            await self._meta_pool.close()
        if self._cache_pool:
            await self._cache_pool.close()
        self._connected = False

    async def init_schema(self) -> None:
        """Create tables in both databases (idempotent).

        Called on every server startup.  All DDL uses ``IF NOT EXISTS``
        so repeated calls are safe — no errors on re-initialization.

        DESIGN NOTE: checks ``is_nullable`` on every startup.  Could cache
        a ``schema_version`` flag to skip this after first migration.
        The current approach (check-then-conditionally-alter) trades a
        millisecond of startup time for never needing a migration tool.
        """

        meta = await self._meta_pool.acquire()
        try:
            await meta.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id          TEXT PRIMARY KEY,
                    description TEXT,
                    created_at  TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            await meta.execute(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    id            SERIAL PRIMARY KEY,
                    token_hash    BYTEA NOT NULL UNIQUE,
                    project_id    TEXT REFERENCES projects(id) ON DELETE CASCADE,
                    can_read      BOOLEAN NOT NULL DEFAULT true,
                    can_write     BOOLEAN NOT NULL DEFAULT false,
                    can_overwrite BOOLEAN NOT NULL DEFAULT false,
                    is_admin     BOOLEAN NOT NULL DEFAULT false,
                    description   TEXT,
                    created_at    TIMESTAMPTZ DEFAULT now(),
                    revoked_at    TIMESTAMPTZ
                )
                """
            )
            await meta.execute("CREATE INDEX IF NOT EXISTS idx_tokens_project ON tokens(project_id)")
            # Ensure project_id is nullable (admin tokens use NULL).
            # Check information_schema first — skip ALTER when already nullable
            # to avoid an ACCESS EXCLUSIVE lock on every startup.
            #
            # Why check before ALTER?
            # -----------------------
            # ``ALTER COLUMN DROP NOT NULL`` acquires an ACCESS EXCLUSIVE
            # lock on the table — blocks all reads/writes while it runs.
            # Checking ``information_schema`` first avoids this lock on
            # every startup after the initial migration.  This is important
            # for zero-downtime restarts where other servers may be active.
            is_nullable = await meta.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='tokens' AND column_name='project_id'"
            )
            if is_nullable and is_nullable.upper() != "YES":
                await meta.execute("ALTER TABLE tokens ALTER COLUMN project_id DROP NOT NULL")
            # Ensure is_admin column exists (added in v0.24.1).
            # ``ADD COLUMN IF NOT EXISTS`` is PostgreSQL 9.6+ — safe for
            # all supported versions.
            await meta.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false")
            # Existing tokens with NULL project_id are admin tokens.
            # Backfill on first run — after this, new admin tokens are
            # created with is_admin=true directly.
            await meta.execute("UPDATE tokens SET is_admin = true WHERE project_id IS NULL AND NOT is_admin")
        finally:
            await self._meta_pool.release(meta)

        # ── llm_analysis_cache ──────────────────────────────────────────
        # DESIGN NOTE: This table intentionally has NO project_id scoping.
        # Analyses are keyed by content_hash (SHA-256 of symbol body +
        # qualified_name + signature + docstring) and SHARED across all
        # projects.  Two projects indexing the same SDK symbol (same hash)
        # will share the same cached analysis — no duplicate LLM work.
        #
        # Why share across projects?
        # -------------------------
        # SDK symbols (mbed-os, Zephyr, STM32 HAL) are identical across
        # all projects that use the same SDK version.  Content-addressing
        # ensures that ``mbed::I2C::write`` from project A and project B
        # have the same hash → same cached analysis.  Without sharing,
        # each project would independently LLM-analyze the same SDK
        # symbols — wasting GPU time and the operator's money.
        #
        # Token-scoped project_id exists in the meta.tokens table for
        # auth/permission control only.  Cache reads/writes are global —
        # "first write wins" per content_hash across all projects.
        cache = await self._cache_pool.acquire()

        try:
            await cache.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_analysis_cache (
                    content_hash TEXT PRIMARY KEY,
                    summary      TEXT NOT NULL,
                    inputs       TEXT NOT NULL,
                    outputs      TEXT NOT NULL,
                    model        TEXT NOT NULL,
                    analyzed_at  TIMESTAMPTZ DEFAULT now()
                )
                """
            )
        finally:
            await self._cache_pool.release(cache)

        logger.info("Schema initialized in meta + cache databases")

    async def validate_token(self, token: str) -> dict[str, Any] | None:
        """Look up a bearer token and return permissions.

        Returns a dict with keys ``project_id``, ``can_read``,
        ``can_write``, ``can_overwrite``, ``is_admin``, or ``None`` if the
        token is not found / revoked.

        Why SHA-256 hash the token?
        ---------------------------
        Tokens are stored as ``BYTEA(token_hash)`` — never as plain text.
        If the database is compromised, the attacker gets SHA-256 hashes
        of random 256-bit tokens, which are not reversible.  Plain-text
        tokens would give the attacker direct access to the API.
        """
        token_hash = hashlib.sha256(token.encode()).digest()
        async with self._meta_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT project_id, can_read, can_write, can_overwrite, is_admin "
                "FROM tokens WHERE token_hash = $1 AND revoked_at IS NULL",
                token_hash,
            )
            if row is None:
                return None
            return {
                "project_id": row["project_id"],
                "can_read": row["can_read"],
                "can_write": row["can_write"],
                "can_overwrite": row["can_overwrite"],
                "is_admin": row["is_admin"],
            }

    async def batch_get(self, hashes: list[str]) -> dict[str, dict[str, Any] | None]:
        """Look up multiple content hashes in one query.

        Returns a dict mapping each hash to either the cached entry
        (summary, inputs, outputs, model, analyzed_at) or ``None``.

        Why ``ANY($1::text[])`` instead of multiple queries?
        ----------------------------------------------------
        A single query with an array parameter avoids N network
        round-trips and allows PostgreSQL to use the primary key index
        efficiently.  For 1000 hashes, this is ~1 ms vs ~200 ms for
        1000 individual ``SELECT ... WHERE content_hash = $1`` queries.
        """
        if not hashes:
            return {}
        async with self._cache_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content_hash, summary, inputs, outputs, model, "
                "analyzed_at::text AS analyzed_at "
                "FROM llm_analysis_cache WHERE content_hash = ANY($1::text[])",
                hashes,
            )
        # Pre-populate with None for all requested hashes — hashes not
        # found in the database remain None (cache miss).
        result: dict[str, dict[str, Any] | None] = {h: None for h in hashes}
        for row in rows:
            result[row["content_hash"]] = {
                "summary": row["summary"],
                "inputs": row["inputs"],
                "outputs": row["outputs"],
                "model": row["model"],
                "analyzed_at": row["analyzed_at"],
            }
        return result

    async def batch_put(
        self,
        entries: list[dict[str, str]],
        *,
        can_overwrite: bool = False,
    ) -> int:
        """Insert multiple cache entries in a single round trip.

        When *can_overwrite* is ``False`` (default), uses ``INSERT ON
        CONFLICT DO NOTHING`` — the first analysis wins.

        When *can_overwrite* is ``True``, uses ``INSERT ON CONFLICT DO
        UPDATE`` — overwrites existing entries with newer analysis.

        Returns the actual number of rows newly inserted or updated.

        Why ``unnest()`` with arrays instead of ``execute_values()``?
        -------------------------------------------------------------
        PostgreSQL's ``unnest()`` with parallel arrays is the fastest
        bulk-insert method in asyncpg — it sends all values as binary
        protocol arrays in a single message.  ``execute_values()`` in
        psycopg2 is comparable, but asyncpg's binary protocol is ~2×
        faster for 1000+ row batches.

        Why UPDATE analyzed_at?
        -----------------------
        When overwriting, the ``analyzed_at`` timestamp is reset to
        ``now()``.  This is important for cache freshness — a
        re-analyzed entry should appear as the newest entry, not
        retain its old timestamp.  Without this, cache purging based
        on ``analyzed_at`` would incorrectly delete freshly re-analyzed
        entries.
        """
        if not entries:
            return 0

        # Extract columns into parallel arrays for unnest()
        hashes = [e["hash"] for e in entries]
        summaries = [e["summary"] for e in entries]
        inputs_list = [e["inputs"] for e in entries]
        outputs_list = [e["outputs"] for e in entries]
        models = [e["model"] for e in entries]

        if can_overwrite:
            stmt = (
                "INSERT INTO llm_analysis_cache (content_hash, summary, inputs, outputs, model) "
                "SELECT h, s, i, o, m FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[]) "
                "AS t(h, s, i, o, m) "
                "ON CONFLICT (content_hash) DO UPDATE SET "
                "summary = EXCLUDED.summary, inputs = EXCLUDED.inputs, "
                "outputs = EXCLUDED.outputs, model = EXCLUDED.model, "
                "analyzed_at = now() "
                "RETURNING content_hash"
            )
        else:
            stmt = (
                "INSERT INTO llm_analysis_cache (content_hash, summary, inputs, outputs, model) "
                "SELECT h, s, i, o, m FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[]) "
                "AS t(h, s, i, o, m) "
                "ON CONFLICT (content_hash) DO NOTHING "
                "RETURNING content_hash"
            )

        async with self._cache_pool.acquire() as conn:
            rows = await conn.fetch(stmt, hashes, summaries, inputs_list, outputs_list, models)

        # RETURNING content_hash gives us the exact count of affected rows
        return len(rows)

    # -- Admin methods (direct PostgreSQL access, not via HTTP) --

    async def create_project(self, project_id: str, description: str = "") -> str | None:
        """Create a project and return a generated write token, or ``None`` if exists.

        Why generate a write token automatically?
        -----------------------------------------
        Every project needs at least one token with write permission
        (to push analyses to the cache).  Creating the project and its
        first write token in a single transaction prevents the race
        condition where a project exists but has no usable token —
        which would require admin intervention to fix.

        Why a transaction?
        ------------------
        If the ``INSERT INTO projects`` succeeds but ``INSERT INTO tokens``
        fails, the project exists with no tokens — an unrecoverable state
        for the operator.  Wrapping both in a transaction ensures either
        both succeed or neither does.
        """
        token = secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode()).digest()

        async with self._meta_pool.acquire() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO projects (id, description) VALUES ($1, $2)",
                        project_id, description,
                    )
                    await conn.execute(
                        "INSERT INTO tokens (token_hash, project_id, can_read, can_write, can_overwrite) "
                        "VALUES ($1, $2, true, true, true)",
                        token_hash, project_id,
                    )
            except Exception as e:
                # Only mask duplicate key violations — let other errors propagate.
                # sqlstate=23505 is unique_violation (project already exists).
                if hasattr(e, "sqlstate") and getattr(e, "sqlstate", None) == "23505":
                    return None  # project already exists
                raise
        return token

    async def create_token(
        self,
        project_id: str,
        *,
        can_read: bool = True,
        can_write: bool = False,
        can_overwrite: bool = False,
        description: str = "",
    ) -> str:
        """Create a token for a project.  Returns the plain-text token.

        Why ``secrets.token_hex(32)``?
        ------------------------------
        ``secrets`` uses the OS CSPRNG — appropriate for authentication
        tokens.  ``token_hex(32)`` produces a 64-character hex string
        with 256 bits of entropy — far beyond brute-force feasibility.
        This is the same entropy as a SHA-256 hash but in a human-
        readable format that can be copy-pasted.
        """
        token = secrets.token_hex(32)

        token_hash = hashlib.sha256(token.encode()).digest()

        async with self._meta_pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO tokens (token_hash, project_id, can_read, can_write, can_overwrite, description) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    token_hash, project_id, can_read, can_write, can_overwrite, description,
                )
            except Exception as e:
                # sqlstate=23503 is foreign_key_violation — project doesn't exist
                if hasattr(e, "sqlstate") and getattr(e, "sqlstate", None) == "23503":
                    raise ValueError(f"Project '{project_id}' does not exist") from e
                raise
        return token

    async def revoke_token(self, token: str) -> bool:
        """Revoke a token by its plain-text value.  Returns True if found.

        Why revoke instead of delete?
        -----------------------------
        Revocation (setting ``revoked_at``) preserves an audit trail.
        Deleted tokens leave no trace — you cannot answer "when was
        this token created and by whom?"  Revoked tokens keep their
        record with a timestamp — useful for security audits and
        incident response.
        """
        token_hash = hashlib.sha256(token.encode()).digest()
        async with self._meta_pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE tokens SET revoked_at = now() WHERE token_hash = $1 AND revoked_at IS NULL "
                "RETURNING 1",
                token_hash,
            )
            return row is not None

    async def list_projects(self) -> list[dict[str, Any]]:
        """List all projects with creation timestamps.

        Returns a list of dicts with keys ``id``, ``description``, ``created_at``.
        """
        async with self._meta_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, created_at::text AS created_at FROM projects ORDER BY id"
            )
        return [dict(r) for r in rows]

    async def list_tokens(self, project_id: str) -> list[dict[str, Any]]:
        """List tokens for a project (shows last 8 chars of hash).

        Why encode token_hash as hex in the query?
        ------------------------------------------
        BYTEA columns return Python ``bytes`` objects — not directly
        printable.  Encoding to hex in the SQL query avoids per-row
        Python conversions in the result set.  The returned hex string
        is NOT the original token — it's the SHA-256 hash, which cannot
        be reversed to recover the plain-text token.
        """
        async with self._meta_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, encode(token_hash, 'hex') AS token_hash, can_read, can_write, "
                "can_overwrite, description, created_at::text AS created_at, "
                "revoked_at::text AS revoked_at "
                "FROM tokens WHERE project_id = $1 ORDER BY id",
                project_id,
            )
        return [dict(r) for r in rows]

    async def remove_project(self, project_id: str) -> bool:
        """Delete a project and its tokens. Cache entries are NOT deleted.

        Why preserve cache entries?
        ---------------------------
        Cache entries are global (not project-scoped).  Deleting a
        project should not delete shared analyses that other projects
        still depend on.  The ``ON DELETE CASCADE`` on tokens ensures
        all tokens for the deleted project are cleaned up, but the
        ``llm_analysis_cache`` table is in a separate database and
        unaffected.

        Why parse DELETE result tag?
        ----------------------------
        asyncpg's ``conn.execute()`` returns a command tag string like
        ``"DELETE 5"``.  Parsing this is the only way to determine how
        many rows were affected without a ``RETURNING`` clause.
        """
        async with self._meta_pool.acquire() as conn:
            result = await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
            # DELETE returns "DELETE N" (2 elements, unlike INSERT's 3)
            tag = result.split()
            return len(tag) >= 2 and int(tag[1]) > 0

    async def cache_stats(self) -> dict[str, Any]:
        """Return cache statistics: total entries, timestamps, model breakdown.

        Why aggregate model counts?
        ---------------------------
        Different LLM models produce analyses of varying quality.
        An admin can see "80% of entries are from model X, 20% from
        model Y" and decide whether to purge old-model entries after
        upgrading to a better model.
        """
        async with self._cache_pool.acquire() as conn:
            total = await conn.fetchval("SELECT count(*) FROM llm_analysis_cache")
            newest = await conn.fetchval("SELECT max(analyzed_at) FROM llm_analysis_cache")
            oldest = await conn.fetchval("SELECT min(analyzed_at) FROM llm_analysis_cache")
            models = await conn.fetch("SELECT model, count(*) AS cnt FROM llm_analysis_cache GROUP BY model")
        return {
            "total_entries": total,
            "newest_entry": str(newest) if newest else None,
            "oldest_entry": str(oldest) if oldest else None,
            "models": {r["model"]: r["cnt"] for r in models},
        }

    async def cache_clear_by_hashes(self, hashes: list[str]) -> int:
        """Delete cache entries by content hash. Returns number deleted.

        Why accept a list of hashes (not a single hash)?
        ------------------------------------------------
        Cache invalidation often needs to clear multiple entries at
        once (e.g. all symbols in a changed file).  Batching deletes
        avoids N round-trips for N hashes.
        """
        if not hashes:
            return 0
        deleted = 0
        async with self._cache_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM llm_analysis_cache WHERE content_hash = ANY($1::text[])",
                hashes,
            )
            tag = result.split()
            if len(tag) >= 2:
                deleted = int(tag[1])
        return deleted

    async def cache_purge_older_than(self, days: int) -> int:
        """Delete cache entries older than *days*.  Returns rows deleted.

        Why ``interval '1 day' * $1`` instead of f-string?
        --------------------------------------------------
        Using parameterized queries prevents SQL injection and allows
        PostgreSQL to cache the query plan.  An f-string like
        ``f"... interval '{days} days'"`` would create a unique query
        for each value, bloating the prepared statement cache.
        """
        async with self._cache_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM llm_analysis_cache WHERE analyzed_at < now() - interval '1 day' * $1",
                days,
            )
            # DELETE returns "DELETE N" (2 elements)
            tag = result.split()
            return int(tag[1]) if len(tag) >= 2 else 0

    async def create_admin_token(self) -> str:
        """Create an admin token not scoped to any project (``project_id IS NULL``).

        Used once during ``fw-cache-server init`` to bootstrap the first
        admin token.  Returns the plain-text token.

        Why ``project_id IS NULL`` for admin tokens?
        --------------------------------------------
        Admin tokens should work across all projects — they manage
        the cache server itself, not a specific project's data.
        ``project_id IS NULL`` means "not scoped to any project" —
        the admin can list/create/delete ALL projects.
        """
        token = secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode()).digest()

        async with self._meta_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tokens (token_hash, project_id, can_read, can_write, can_overwrite, is_admin, description) "
                "VALUES ($1, NULL, true, true, true, true, 'admin (setup)')",
                token_hash,
            )
        return token
