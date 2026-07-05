"""PostgreSQL backend for the shared LLM analysis cache.

Provides connection pools for the meta database (projects, tokens) and
the cache database (``llm_analysis_cache``), plus batch get/put operations.

Write behaviour is controlled by *can_overwrite*:
- ``False`` (default) — ``INSERT ON CONFLICT DO NOTHING`` (first write wins)
- ``True`` — ``INSERT ON CONFLICT DO UPDATE`` (overwrite with newer analysis)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CacheBackend:
    """Async PostgreSQL backend for the shared LLM analysis cache.

    Manages two connection pools — one for ``fw_cache_meta`` and one for
    ``fw_cache`` — derived from a single *base_url* by appending the
    database name (e.g. ``postgresql://user:pass@localhost:5432`` +
    ``/fw_cache_meta`` and ``/fw_cache``).

    All methods are synchronous for compatibility with FastAPI's
    non-async route handlers.  Uses ``asyncpg`` internally via
    ``asyncio.run``.
    """

    def __init__(self, base_url: str, min_size: int = 2, max_size: int = 10):
        """*base_url* is the PostgreSQL connection string *without* a
        database component (e.g. ``postgresql://user:pass@localhost:5432``).
        The two individual pools will append ``/fw_cache_meta`` and
        ``/fw_cache`` respectively.

        *min_size* / *max_size* control the pool size for each pool.
        """
        self._base_url = base_url.rstrip("/")
        self._min_size = min_size
        self._max_size = max_size
        self._meta_pool: Any = None
        self._cache_pool: Any = None
        self._connected = False

    @property
    def meta_url(self) -> str:
        return self._db_url("fw_cache_meta")

    @property
    def cache_url(self) -> str:
        return self._db_url("fw_cache")

    def _db_url(self, db_name: str) -> str:
        """Build a database-specific URL from the base connection string.

        Handles URLs with query parameters (e.g. ``?host=/tmp`` for
        Unix-socket connections) correctly — the database name is
        inserted before the query string.
        """
        base = self._base_url
        if "?" in base:
            prefix, params = base.split("?", 1)
            return f"{prefix}/{db_name}?{params}"
        else:
            return f"{base}/{db_name}"

    async def connect(self) -> None:
        """Create both connection pools (call once at startup)."""
        import asyncpg

        self._meta_pool = await asyncpg.create_pool(
            self.meta_url, min_size=self._min_size, max_size=self._max_size
        )
        self._cache_pool = await asyncpg.create_pool(
            self.cache_url, min_size=self._min_size, max_size=self._max_size
        )
        self._connected = True
        logger.info("CacheBackend connected — meta=%s cache=%s", self.meta_url, self.cache_url)

    async def close(self) -> None:
        """Close both pools (call at shutdown)."""
        if self._meta_pool:
            await self._meta_pool.close()
        if self._cache_pool:
            await self._cache_pool.close()
        self._connected = False

    async def init_schema(self) -> None:
        """Create tables in both databases (idempotent)."""

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
                    description   TEXT,
                    created_at    TIMESTAMPTZ DEFAULT now(),
                    revoked_at    TIMESTAMPTZ
                )
                """
            )
            await meta.execute("CREATE INDEX IF NOT EXISTS idx_tokens_project ON tokens(project_id)")
            # Ensure project_id is nullable (admin tokens use NULL)
            await meta.execute("ALTER TABLE tokens ALTER COLUMN project_id DROP NOT NULL")
        finally:
            await self._meta_pool.release(meta)

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
        ``can_write``, ``can_overwrite``, or ``None`` if the token is
        not found / revoked.
        """
        import hashlib

        token_hash = hashlib.sha256(token.encode()).digest()
        async with self._meta_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT project_id, can_read, can_write, can_overwrite "
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
            }

    async def batch_get(self, hashes: list[str]) -> dict[str, dict[str, Any] | None]:
        """Look up multiple content hashes in one query.

        Returns a dict mapping each hash to either the cached entry
        (summary, inputs, outputs, model, analyzed_at) or ``None``.
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
        """Insert multiple cache entries.

        When *can_overwrite* is ``False`` (default), uses ``INSERT ON
        CONFLICT DO NOTHING`` — the first analysis wins.

        When *can_overwrite* is ``True``, uses ``INSERT ON CONFLICT DO
        UPDATE`` — overwrites existing entries with newer analysis.

        Returns the number of rows inserted or updated.
        """
        if not entries:
            return 0

        if can_overwrite:
            stmt = (
                "INSERT INTO llm_analysis_cache (content_hash, summary, inputs, outputs, model) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (content_hash) DO UPDATE SET "
                "summary = EXCLUDED.summary, inputs = EXCLUDED.inputs, "
                "outputs = EXCLUDED.outputs, model = EXCLUDED.model, "
                "analyzed_at = now()"
            )
        else:
            stmt = (
                "INSERT INTO llm_analysis_cache (content_hash, summary, inputs, outputs, model) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (content_hash) DO NOTHING"
            )

        inserted = 0
        async with self._cache_pool.acquire() as conn:
            for entry in entries:
                result = await conn.execute(
                    stmt,
                    entry["hash"],
                    entry["summary"],
                    entry["inputs"],
                    entry["outputs"],
                    entry["model"],
                )
                # Parse the command tag — "INSERT 0 1" or "INSERT 0 0"
                tag = result.split()
                if len(tag) >= 3:
                    inserted += int(tag[2])

        return inserted

    # -- Admin methods (direct PostgreSQL access, not via HTTP) --

    async def create_project(self, project_id: str, description: str = "") -> str | None:
        """Create a project and return a generated write token, or ``None`` if exists."""
        import secrets

        token = secrets.token_hex(32)
        import hashlib

        token_hash = hashlib.sha256(token.encode()).digest()

        async with self._meta_pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO projects (id, description) VALUES ($1, $2)",
                    project_id, description,
                )
            except Exception:
                return None  # already exists
            await conn.execute(
                "INSERT INTO tokens (token_hash, project_id, can_read, can_write, can_overwrite) "
                "VALUES ($1, $2, true, true, true)",
                token_hash, project_id,
            )
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
        """Create a token for a project.  Returns the plain-text token."""
        import secrets

        token = secrets.token_hex(32)
        import hashlib

        token_hash = hashlib.sha256(token.encode()).digest()

        async with self._meta_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tokens (token_hash, project_id, can_read, can_write, can_overwrite, description) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                token_hash, project_id, can_read, can_write, can_overwrite, description,
            )
        return token

    async def revoke_token(self, token: str) -> bool:
        """Revoke a token by its plain-text value.  Returns True if found."""
        import hashlib

        token_hash = hashlib.sha256(token.encode()).digest()
        async with self._meta_pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE tokens SET revoked_at = now() WHERE token_hash = $1 AND revoked_at IS NULL",
                token_hash,
            )
            # UPDATE returns "UPDATE N" (2 elements, unlike INSERT's 3)
            tag = result.split()
            return len(tag) >= 2 and int(tag[1]) > 0

    async def list_projects(self) -> list[dict[str, Any]]:
        """List all projects."""
        async with self._meta_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, created_at::text AS created_at FROM projects ORDER BY id"
            )
        return [dict(r) for r in rows]

    async def list_tokens(self, project_id: str) -> list[dict[str, Any]]:
        """List tokens for a project (shows last 8 chars of hash)."""
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
        """Delete a project and its tokens. Cache entries are NOT deleted."""
        async with self._meta_pool.acquire() as conn:
            result = await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
            # DELETE returns "DELETE N" (2 elements, unlike INSERT's 3)
            tag = result.split()
            return len(tag) >= 2 and int(tag[1]) > 0

    async def cache_stats(self) -> dict[str, Any]:
        """Return cache statistics."""
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
        """Delete cache entries by content hash. Returns number deleted."""
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
        """Delete cache entries older than *days*.  Returns rows deleted."""
        async with self._cache_pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM llm_analysis_cache WHERE analyzed_at < now() - make_interval(days => $1)",
                days,
            )
            # DELETE returns "DELETE N" (2 elements)
            tag = result.split()
            return int(tag[1]) if len(tag) >= 2 else 0

    async def create_admin_token(self) -> str:
        """Create an admin token not scoped to any project (``project_id IS NULL``).

        Used once during ``fw-cache-server init`` to bootstrap the first
        admin token.  Returns the plain-text token.
        """
        import hashlib
        import secrets

        token = secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode()).digest()

        async with self._meta_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tokens (token_hash, project_id, can_read, can_write, can_overwrite, description) "
                "VALUES ($1, NULL, true, true, true, 'admin (setup)')",
                token_hash,
            )
        return token
