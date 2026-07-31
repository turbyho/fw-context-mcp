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
        token = auth[7:].strip()
        # Reject unreasonably large tokens before hashing
        if len(token) > 1024:
            return None
        return token
    return None


async def _lookup_permissions(request: Request, token: str) -> dict[str, Any] | None:
    backend = request.app.state.backend
    return await backend.validate_token(token)



# Simple in-memory IP-based rate limiter for auth failures.
# Tracks failed attempts per IP with a sliding 60s window.
_auth_failures: dict[str, list[float]] = {}
import time as _time
import threading as _threading
_auth_lock = _threading.Lock()

def _check_rate_limit(ip: str, max_failures: int = 20, window_s: float = 60.0) -> bool:
    """Return True if *ip* is under the rate limit, False if exceeded."""
    now = _time.monotonic()
    with _auth_lock:
        failures = _auth_failures.get(ip, [])
        # Remove expired entries
        failures = [t for t in failures if now - t < window_s]
        if len(failures) >= max_failures:
            _auth_failures[ip] = failures
            return False
        _auth_failures[ip] = failures
        return True

def _record_auth_failure(ip: str) -> None:
    """Record a failed auth attempt for rate limiting."""
    with _auth_lock:
        _auth_failures.setdefault(ip, []).append(_time.monotonic())


class CacheAuthMiddleware(BaseHTTPMiddleware):
    """Validates bearer tokens and attaches permissions to request.state."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Health check is public
        if request.url.path == "/health":
            return await call_next(request)

        token = _extract_bearer(request)
        if token is None:
            return JSONResponse({"detail": "Missing Authorization header"}, status_code=401)

        # Rate-limit auth failures per IP (defense-in-depth)
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            return JSONResponse({"detail": "Too many auth attempts"}, status_code=429)

        perms = await _lookup_permissions(request, token)
        if perms is None:
            _record_auth_failure(client_ip)
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

    Pure permission check — no side effects on request.state.
    For write endpoints that need overwrite support, use
    ``require_can_write_with_overwrite`` instead.
    """
    perms = getattr(request.state, "permissions", {})
    if not perms.get("can_write", False):
        return JSONResponse({"detail": "Token does not have write permission"}, status_code=403)
    return None


def require_can_write_with_overwrite(request: Request) -> JSONResponse | None:
    """Check ``can_write`` AND parse ``X-Cache-Overwrite`` header.

    Parses the header once and stores the boolean result in
    ``request.state.can_overwrite`` so route handlers don't re-parse it.
    Also checks ``can_overwrite`` — returns 403 when the header is set
    but the token lacks the permission.
    """
    error = require_can_write(request)
    if error is not None:
        return error

    perms = getattr(request.state, "permissions", {})
    overwrite = request.headers.get("X-Cache-Overwrite", "").lower() in ("true", "1", "yes")
    request.state.can_overwrite = overwrite
    if overwrite and not perms.get("can_overwrite", False):
        return JSONResponse({"detail": "Token does not have overwrite permission"}, status_code=403)
    return None
