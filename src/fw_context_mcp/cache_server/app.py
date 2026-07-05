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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import __version__
from .auth import CacheAuthMiddleware, require_can_read, require_can_write

logger = logging.getLogger(__name__)


# -- Request models --

class BatchGetRequest(BaseModel):
    hashes: list[str]


class CacheClearRequest(BaseModel):
    hashes: list[str]


class CacheEntry(BaseModel):
    hash: str
    summary: str
    inputs: str
    outputs: str
    model: str


class BatchPutRequest(BaseModel):
    entries: list[CacheEntry]


# -- Application factory --

def create_app(*, backend: Any = None) -> FastAPI:
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

    # -- Endpoints --

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        """Public health check — no auth required."""
        return {"status": "ok", "version": __version__}

    @app.post("/cache/batch", response_model=None)
    async def batch_get(request: Request, body: BatchGetRequest) -> dict[str, Any] | JSONResponse:
        """Batch lookup content hashes.

        Returns ``{"results": {hash: entry | null, ...}}``.
        Requires ``can_read``.
        """
        error = require_can_read(request)
        if error is not None:
            return error

        hashes = body.hashes[:1000]  # hard cap
        truncated = len(body.hashes) > 1000
        results = await request.app.state.backend.batch_get(hashes)
        return {"results": results, "truncated": truncated}

    @app.put("/cache/batch", response_model=None)
    async def batch_put(request: Request, body: BatchPutRequest) -> dict[str, Any] | JSONResponse:
        """Batch write cache entries.

        Normal behaviour: ``INSERT ON CONFLICT DO NOTHING`` (first write wins).
        With ``X-Cache-Overwrite: true`` header (requires ``can_overwrite``):
        ``INSERT ON CONFLICT DO UPDATE`` (overwrites existing entry).

        Requires ``can_write``.
        """
        error = require_can_write(request)
        if error is not None:
            return error

        overwrite = getattr(request.state, "can_overwrite", False)
        truncated = len(body.entries) > 1000
        entries = [
            {"hash": e.hash, "summary": e.summary, "inputs": e.inputs, "outputs": e.outputs, "model": e.model}
            for e in body.entries[:1000]  # hard cap
        ]
        inserted = await request.app.state.backend.batch_put(entries, can_overwrite=overwrite)
        return {"inserted": inserted, "total": len(entries), "truncated": truncated}

    @app.post("/cache/clear", response_model=None)
    async def cache_clear(request: Request, body: CacheClearRequest) -> dict[str, Any] | JSONResponse:
        """Delete cache entries by content hash.

        Requires ``can_write``. Returns the number of deleted entries.
        """
        error = require_can_write(request)
        if error is not None:
            return error

        hashes = body.hashes[:10000]  # hard cap
        deleted = await request.app.state.backend.cache_clear_by_hashes(hashes)
        return {"deleted": deleted, "total": len(hashes)}

    return app
