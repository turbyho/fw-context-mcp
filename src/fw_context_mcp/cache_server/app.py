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
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from .auth import CacheAuthMiddleware, require_can_read, require_can_write

logger = logging.getLogger(__name__)


# -- Request models --

class BatchGetRequest(BaseModel):
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

def create_app(db_url: str = "", *, backend: Any = None) -> FastAPI:
    """Create and configure the FastAPI app.

    *db_url* must be the base PostgreSQL connection string (without a
    database name).  The backend will append ``/fw_cache_meta`` and
    ``/fw_cache`` internally.

    *backend* is for testing — when provided, it is used as
    ``app.state.backend`` instead of creating a new ``CacheBackend``.
    """
    if backend is None:
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

    app = FastAPI(title="fw-context Cache Server", version="0.1.0", lifespan=lifespan)
    app.state.backend = backend
    app.add_middleware(CacheAuthMiddleware)

    # -- Endpoints --

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        """Public health check — no auth required."""
        return {"status": "ok"}

    @app.post("/cache/batch")
    async def batch_get(request: Request, body: BatchGetRequest) -> dict[str, Any]:
        """Batch lookup content hashes.

        Returns ``{"results": {hash: entry | null, ...}}``.
        Requires ``can_read``.
        """
        error = require_can_read(request)
        if error is not None:
            return error

        hashes = body.hashes[:1000]  # hard cap
        results = await request.app.state.backend.batch_get(hashes)
        return {"results": results}

    @app.put("/cache/batch")
    async def batch_put(request: Request, body: BatchPutRequest) -> dict[str, Any]:
        """Batch write cache entries.

        Normal behaviour: ``INSERT ON CONFLICT DO NOTHING`` (first write wins).
        With ``X-Cache-Overwrite: true`` header (requires ``can_overwrite``):
        ``INSERT ON CONFLICT DO UPDATE`` (overwrites existing entry).

        Requires ``can_write``.
        """
        error = require_can_write(request)
        if error is not None:
            return error

        overwrite = request.headers.get("X-Cache-Overwrite", "").lower() in ("true", "1", "yes")
        entries = [
            {"hash": e.hash, "summary": e.summary, "inputs": e.inputs, "outputs": e.outputs, "model": e.model}
            for e in body.entries[:1000]  # hard cap
        ]
        inserted = await request.app.state.backend.batch_put(entries, can_overwrite=overwrite)
        return {"inserted": inserted, "total": len(entries)}

    return app
