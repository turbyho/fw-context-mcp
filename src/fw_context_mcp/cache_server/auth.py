"""FastAPI auth middleware for the shared cache server.

Validates bearer tokens against the *meta* database and sets
``request.state.permissions`` with the resolved token info, or returns
HTTP 401 / 403.

Read endpoints require ``can_read``, write endpoints require
``can_write``, and the ``X-Cache-Overwrite`` header additionally
requires ``can_overwrite``.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


async def _lookup_permissions(request: Request, token: str) -> dict[str, Any] | None:
    backend = request.app.state.backend
    return await backend.validate_token(token)


class CacheAuthMiddleware(BaseHTTPMiddleware):
    """Validates bearer tokens and attaches permissions to request.state."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Health check is public
        if request.url.path == "/health":
            return await call_next(request)

        token = _extract_bearer(request)
        if token is None:
            return JSONResponse({"detail": "Missing Authorization: Bearer <token>"}, status_code=401)

        perms = await _lookup_permissions(request, token)
        if perms is None:
            return JSONResponse({"detail": "Invalid or revoked token"}, status_code=401)

        request.state.permissions = perms
        response = await call_next(request)
        return response


def require_can_read(request: Request) -> JSONResponse | None:
    """Check that the authenticated token has ``can_read``."""
    perms = getattr(request.state, "permissions", {})
    if not perms.get("can_read", False):
        return JSONResponse({"detail": "Token does not have read permission"}, status_code=403)
    return None


def require_can_write(request: Request) -> JSONResponse | None:
    """Check that the authenticated token has ``can_write``.

    Also parses ``X-Cache-Overwrite`` once and stores the result in
    ``request.state.can_overwrite`` so route handlers don't re-parse it.
    """
    perms = getattr(request.state, "permissions", {})
    if not perms.get("can_write", False):
        return JSONResponse({"detail": "Token does not have write permission"}, status_code=403)

    overwrite = request.headers.get("X-Cache-Overwrite", "").lower() in ("true", "1", "yes")
    request.state.can_overwrite = overwrite
    if overwrite and not perms.get("can_overwrite", False):
        return JSONResponse({"detail": "Token does not have overwrite permission"}, status_code=403)

    return None
