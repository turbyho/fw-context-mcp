"""FastAPI application for the shared LLM analysis cache server.

Registers three endpoints:

- ``GET  /health``      — public health check (no auth)
- ``POST /cache/batch`` — batch lookup (requires ``can_read``)
- ``PUT  /cache/batch`` — batch write (requires ``can_write``;
  ``X-Cache-Overwrite`` requires ``can_overwrite``)
- ``GET  /cache/stats`` — cache statistics with token permissions
- ``POST /cache/clear`` — delete entries by hash (requires ``can_write``)

Startup/shutdown hooks manage the asyncpg connection pool lifecycle.

Why FastAPI (not Flask / aiohttp)?
----------------------------------
FastAPI provides built-in async support, automatic request validation
via Pydantic models (hash format, field length limits), and a clean
dependency-injection system for auth.  Asyncpg pools are created at
startup and closed at shutdown via the lifespan context manager —
no manual pool management needed per request.

Hard caps on all batch operations (1000 hashes/entries) prevent a
single request from consuming unbounded memory on the server.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from .. import __version__
from .auth import CacheAuthMiddleware, require_can_read, require_can_write, require_can_write_with_overwrite
from .backend import CacheStorageBackend

logger = logging.getLogger(__name__)


# -- Body size limit middleware --


class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request body size to prevent DoS (10 MB max for batch operations).

    Why a custom middleware (not uvicorn --limit-max-requests)?
    -----------------------------------------------------------
    Uvicorn's request limits apply to *all* requests equally.  The cache
    server handles batch operations that can be large (up to 1000 entries),
    so we need a per-request body size limit that is large enough for
    legitimate batch requests (10 MB) but rejects deliberate over-sizing.
    This is checked BEFORE body parsing — FastAPI/Pydantic validation
    never sees oversized payloads.
    """

    MAX_BYTES = 10 * 1024 * 1024  # 10 MB

    async def dispatch(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                content_len = int(content_length)
            except (ValueError, TypeError):
                return JSONResponse(
                    {"detail": "Invalid Content-Length header"}, status_code=400,
                )
            if content_len > self.MAX_BYTES:
                return JSONResponse(
                    {"detail": "Request body too large (max 10 MB)"}, status_code=413,
                )
        else:
            # No Content-Length — chunked transfer.  Read body with limit.
            # Without this path, a chunked upload of unlimited size could
            # exhaust server memory before FastAPI ever validates.
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > self.MAX_BYTES:
                    return JSONResponse(
                        {"detail": "Request body too large (max 10 MB)"}, status_code=413,
                    )
            # Reconstruct request with the read body for downstream consumers
            from starlette.requests import Request as StarletteRequest
            scope = dict(request.scope)
            request = StarletteRequest(scope, receive=lambda: {"type": "http.request", "body": body})
        return await call_next(request)


# -- Request models --

class BatchGetRequest(BaseModel):
    """Request model for ``POST /cache/batch`` (lookup)."""
    hashes: list[str]


class CacheClearRequest(BaseModel):
    """Request model for ``POST /cache/clear`` (delete)."""
    hashes: list[str] = Field(min_length=1, max_length=10000)


class CacheEntry(BaseModel):
    """A single cache entry for batch write operations.

    Why enforce field length limits?
    --------------------------------
    ``summary`` at 5 KB and ``inputs``/``outputs`` at 100 KB prevent
    a single entry from bloating the cache table.  LLM analyses are
    typically <2 KB for summary and <50 KB for inputs/outputs — these
    limits are generous enough for real data but reject pathological
    payloads that would waste database space.
    """
    hash: str
    summary: str = Field(max_length=5000)
    inputs: str = Field(max_length=100000)
    outputs: str = Field(max_length=100000)
    model: str = Field(max_length=100)

    @field_validator("hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        """Validate that *hash* is a 64-character hex string (SHA-256).

        Why validate client-side?
        -------------------------
        FastAPI validates BEFORE the SQL query runs.  Without this
        validator, a malformed hash would reach PostgreSQL's ``WHERE
        content_hash = ANY($1::text[])`` and either cause a type error
        or silently return no results — both worse than a clear 422
        response at the API boundary.
        """
        if not re.match(r"^[a-f0-9]{64}$", v):
            raise ValueError("hash must be a 64-character hex string (SHA-256)")
        return v


class BatchPutRequest(BaseModel):
    """Request model for ``PUT /cache/batch`` (write)."""
    entries: list[CacheEntry]


# -- Application factory --

def create_app(*, backend: CacheStorageBackend | None = None) -> FastAPI:
    """Create and configure the FastAPI app.

    *backend* is for testing — when provided, it is used as
    ``app.state.backend`` instead of creating a new ``CacheBackend``.
    ``FW_CACHE_DB_URL`` must be set in the environment when *backend*
    is ``None``.

    Why a factory function (not a module-level ``app``)?
    ----------------------------------------------------
    Testing needs a fresh app per test to avoid state leakage between
    test cases.  The factory pattern also allows injecting a mock
    backend — the ``CacheStorageBackend`` interface lets tests swap
    PostgreSQL for an in-memory dict without changing any route code.
    """
    if backend is None:
        db_url = os.environ.get("FW_CACHE_DB_URL", "")
        if not db_url:
            raise RuntimeError("FW_CACHE_DB_URL environment variable is required")
        from .backend import CacheBackend

        backend = CacheBackend(db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Manage the asyncpg connection pool lifecycle.

        Why async context manager?
        --------------------------
        FastAPI's ``lifespan`` protocol replaces the old ``on_event("startup")``
        / ``on_event("shutdown")`` pattern.  The async context manager ensures
        ``connect()`` runs before any request and ``close()`` runs after all
        requests finish — even on crash/exception, because the ``yield`` is
        wrapped in a try/finally by FastAPI.
        """
        await app.state.backend.connect()
        await app.state.backend.init_schema()
        logger.info("Cache server started — listening on configured port")
        yield
        await app.state.backend.close()
        logger.info("Cache server stopped")

    app = FastAPI(title="fw-context Cache Server", version=__version__, lifespan=lifespan)
    app.state.backend = backend
    # Auth middleware first — rejects unauthenticated requests before
    # body parsing or rate limiting runs.
    app.add_middleware(CacheAuthMiddleware)
    app.add_middleware(_BodySizeLimitMiddleware)

    # -- Auth dependencies (run BEFORE body parsing via FastAPI Depends) --

    async def _auth_read(request: Request):
        """Require can_read — raises HTTPException so body is not parsed on auth failure.

        Why raise before Pydantic validation?
        --------------------------------------
        FastAPI's ``Depends`` runs before the route handler's body
        parameter is parsed.  Rejecting on auth failure avoids wasting
        CPU on Pydantic validation of payloads that should never be
        processed by an unauthorized client.
        """
        error = require_can_read(request)
        if error is not None:
            raise HTTPException(status_code=403, detail="Token does not have read permission")

    async def _auth_write(request: Request):
        """Require can_write — raises HTTPException so body is not parsed on auth failure."""
        error = require_can_write(request)
        if error is not None:
            raise HTTPException(status_code=403, detail="Token does not have write permission")

    async def _auth_write_with_overwrite(request: Request):
        """Require can_write AND parse X-Cache-Overwrite header.

        Raises HTTPException so body is not parsed on auth failure.
        Sets ``request.state.can_overwrite`` so route handlers don't re-parse.

        Why store ``can_overwrite`` on request.state?
        ----------------------------------------------
        The ``X-Cache-Overwrite`` header controls INSERT vs UPSERT
        behavior in the backend.  Storing the parsed boolean on
        ``request.state`` means the route handler reads a simple
        attribute — no need to re-parse the header or re-check
        permissions in the route body.
        """
        error = require_can_write_with_overwrite(request)
        if error is not None:
            raise HTTPException(status_code=403, detail="Token does not have write/overwrite permission")

    # Pre-compute dependency objects — each Depends() is a callable that
    # FastAPI invokes per request.  Creating them once at app startup
    # avoids per-request allocation overhead.
    _dep_read = Depends(_auth_read)
    _dep_write = Depends(_auth_write)
    _dep_write_overwrite = Depends(_auth_write_with_overwrite)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        """Public health check — no auth required.

        Why public?
        -----------
        Load balancers and monitoring tools (Uptime Robot, healthchecks.io)
        need an unauthenticated endpoint to verify the server is alive.
        This endpoint returns only version and status — no sensitive data.
        """
        return {"status": "ok", "version": __version__}

    @app.post("/cache/batch", response_model=None)
    async def batch_get(request: Request, body: BatchGetRequest, _auth=_dep_read) -> dict[str, Any]:
        """Batch lookup content hashes.

        Returns ``{"results": {hash: entry | null, ...}}``.
        Requires ``can_read``.

        Why hard cap to 1000 hashes?
        ----------------------------
        PostgreSQL's ``ANY($1::text[])`` with 10,000 parameters causes
        a prepared-statement explosion.  Limiting to 1000 keeps the
        query plan cache small and the response time predictable (<200ms
        for a cold cache, <50ms for warm).
        """
        lookup_hashes = body.hashes[:1000]  # hard cap
        truncated = len(body.hashes) > 1000
        results = await request.app.state.backend.batch_get(lookup_hashes)
        return {"results": results, "truncated": truncated}

    @app.put("/cache/batch", response_model=None)
    async def batch_put(request: Request, body: BatchPutRequest, _auth=_dep_write_overwrite) -> dict[str, Any]:
        """Batch write cache entries.

        Normal behaviour: ``INSERT ON CONFLICT DO NOTHING`` (first write wins).
        With ``X-Cache-Overwrite: true`` header (requires ``can_overwrite``):
        ``INSERT ON CONFLICT DO UPDATE`` (overwrites existing entry).

        Requires ``can_write``.

        Why first-write-wins as default?
        -------------------------------
        The first analysis of a symbol is the most urgent — subsequent
        analyses from other developers are duplicative.  Only when a
        project maintainer explicitly re-analyzes (e.g. after an LLM
        model upgrade) should overwrites be allowed.  This prevents
        accidental cache churn.
        """
        overwrite = getattr(request.state, "can_overwrite", False)
        truncated = len(body.entries) > 1000
        entries = [
            {"hash": e.hash, "summary": e.summary, "inputs": e.inputs, "outputs": e.outputs, "model": e.model}
            for e in body.entries[:1000]  # hard cap
        ]
        inserted = await request.app.state.backend.batch_put(entries, can_overwrite=overwrite)
        return {"inserted": inserted, "total": len(entries), "truncated": truncated}

    @app.get("/cache/stats", response_model=None)
    async def cache_stats(request: Request, _auth=_dep_read) -> dict[str, Any]:
        """Return cache statistics (total entries, models breakdown).

        Requires ``can_read``.

        Why include ``can_read``/``can_write``/``can_overwrite`` in the response?
        -------------------------------------------------------------------------
        The client uses ``stats()`` to discover its own token permissions.
        Without this, the client would need to attempt a write to discover
        it has a read-only token — wasting a round-trip and leaving a 403
        in server logs.  Including permissions in the stats response is a
        single call that tells the client everything it needs to know.
        """
        perms = getattr(request.state, "permissions", {})
        stats = await request.app.state.backend.cache_stats()
        stats["can_read"] = perms.get("can_read", False)
        stats["can_write"] = perms.get("can_write", False)
        stats["can_overwrite"] = perms.get("can_overwrite", False)
        return stats

    @app.post("/cache/clear", response_model=None)
    async def cache_clear(request: Request, body: CacheClearRequest, _auth=_dep_write) -> dict[str, Any]:
        """Delete cache entries by content hash.

        Requires ``can_write``. Returns the number of deleted entries.

        Why is this a separate endpoint (not part of batch_put)?
        --------------------------------------------------------
        Deletion is a distinct operation from writing — it requires
        different authorization (can_write alone, no overwrite needed)
        and has different semantics (irreversible vs idempotent write).
        Separating them keeps the API surface clear.
        """
        hashes = body.hashes[:10000]  # hard cap — higher than batch ops because deletion is cheap
        deleted = await request.app.state.backend.cache_clear_by_hashes(hashes)
        return {"deleted": deleted, "total": len(hashes)}

    @app.exception_handler(Exception)
    async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Convert unhandled exceptions to 503 to avoid leaking stack traces.

        Why a global handler?
        ---------------------
        Without this, FastAPI's default exception handler returns a JSON
        with the full Python traceback in development mode — leaking
        internal file paths, SQL queries, and library versions.  In
        production, unhandled errors become opaque 500s.  This handler
        ensures consistent 503 responses with logging on every unhandled
        path.
        """
        import traceback

        logger.error(
            "Unhandled exception in %s %s: %s\n%s",
            request.method, request.url.path, exc,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "Internal server error"},
        )

    return app
