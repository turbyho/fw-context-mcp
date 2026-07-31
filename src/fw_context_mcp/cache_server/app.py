"""FastAPI application for the shared LLM analysis cache server.

Registers three endpoints:

- ``GET  /health``      — public health check
- ``POST /cache/batch`` — batch lookup (requires ``can_read``)
- ``PUT  /cache/batch`` — batch write (requires ``can_write``;
  ``X-Cache-Overwrite`` requires ``can_overwrite``)

Startup/shutdown hooks manage the asyncpg connection pool lifecycle.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from .auth import CacheAuthMiddleware, require_can_read, require_can_write, require_can_write_with_overwrite

logger = logging.getLogger(__name__)


# -- Request models --

class BatchGetRequest(BaseModel):
    hashes: list[str]


class CacheClearRequest(BaseModel):
    hashes: list[str]


class CacheEntry(BaseModel):
    hash: str
    summary: str = Field(max_length=5000)
    inputs: str = Field(max_length=100000)
    outputs: str = Field(max_length=100000)
    model: str = Field(max_length=100)


class BatchPutRequest(BaseModel):
    entries: list[CacheEntry]


# -- Application factory --

def create_app(*, backend: "CacheStorageBackend | None" = None) -> FastAPI:
    """Create and configure the FastAPI app.

    *backend* is for testing — when provided, it is used as
    ``app.state.backend`` instead of creating a new ``CacheBackend``.
    ``FW_CACHE_DB_URL`` must be set in the environment when *backend*
    is ``None``.
    """
    if backend is None:
        db_url = os.environ.get("FW_CACHE_DB_URL", "")
        if not db_url:
            raise RuntimeError("FW_CACHE_DB_URL environment variable is required")
        from .backend import CacheBackend

        backend = CacheBackend(db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.backend.connect()
        await app.state.backend.init_schema()
        logger.info("Cache server started — listening on configured port")
        yield
        await app.state.backend.close()
        logger.info("Cache server stopped")

    app = FastAPI(title="fw-context Cache Server", version=__version__, lifespan=lifespan)
    app.state.backend = backend
    app.add_middleware(CacheAuthMiddleware)

    # Limit request body size to prevent DoS (10 MB max for batch operations)
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            max_bytes = 10 * 1024 * 1024  # 10 MB
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                return JSONResponse(
                    {"detail": "Request body too large (max 10 MB)"}, status_code=413,
                )
            return await call_next(request)

    app.add_middleware(_BodySizeLimitMiddleware)

    # -- Auth dependencies (run BEFORE body parsing via FastAPI Depends) --

    async def _auth_read(request: Request):
        """Require can_read — raises HTTPException so body is not parsed on auth failure."""
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
        """
        error = require_can_write_with_overwrite(request)
        if error is not None:
            raise HTTPException(status_code=403, detail="Token does not have write/overwrite permission")
    # -- Endpoints --

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        """Public health check — no auth required."""
        return {"status": "ok", "version": __version__}

    @app.post("/cache/batch", response_model=None)
    async def batch_get(request: Request, body: BatchGetRequest, _auth=Depends(_auth_read)) -> dict[str, Any]:
        """Batch lookup content hashes.

        Returns ``{"results": {hash: entry | null, ...}}``.
        Requires ``can_read``.
        """

        lookup_hashes = body.hashes[:1000]  # hard cap
        truncated = len(body.hashes) > 1000
        results = await request.app.state.backend.batch_get(lookup_hashes)
        return {"results": results, "truncated": truncated}

    @app.put("/cache/batch", response_model=None)
    async def batch_put(request: Request, body: BatchPutRequest, _auth=Depends(_auth_write_with_overwrite)) -> dict[str, Any]:
        """Batch write cache entries.

        Normal behaviour: ``INSERT ON CONFLICT DO NOTHING`` (first write wins).
        With ``X-Cache-Overwrite: true`` header (requires ``can_overwrite``):
        ``INSERT ON CONFLICT DO UPDATE`` (overwrites existing entry).

        Requires ``can_write``.
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
    async def cache_stats(request: Request, _auth=Depends(_auth_read)) -> dict[str, Any]:
        """Return cache statistics (total entries, models breakdown).

        Requires ``can_read``.
        """

        perms = getattr(request.state, "permissions", {})
        stats = await request.app.state.backend.cache_stats()
        stats["can_read"] = perms.get("can_read", False)
        stats["can_write"] = perms.get("can_write", False)
        stats["can_overwrite"] = perms.get("can_overwrite", False)
        return stats

    @app.post("/cache/clear", response_model=None)
    async def cache_clear(request: Request, body: CacheClearRequest, _auth=Depends(_auth_write)) -> dict[str, Any]:
        """Delete cache entries by content hash.

        Requires ``can_write``. Returns the number of deleted entries.
        """

        hashes = body.hashes[:10000]  # hard cap
        deleted = await request.app.state.backend.cache_clear_by_hashes(hashes)
        return {"deleted": deleted, "total": len(hashes)}

    return app
