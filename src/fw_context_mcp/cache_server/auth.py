"""FastAPI auth middleware for the shared cache server.

Validates bearer tokens against the *meta* database and sets
``request.state.permissions`` with the resolved token info, or returns
HTTP 401 / 403.

Read endpoints require ``can_read``, write endpoints require
``can_write``, and the ``X-Cache-Overwrite`` header additionally
requires ``can_overwrite``.

Why bearer tokens (not API keys in query strings)?
--------------------------------------------------
Bearer tokens keep secrets out of URLs (which are logged by proxies,
servers, and browsers).  They also work with any HTTP client without
framework-specific auth handling — just set the ``Authorization`` header.

Rate limiting architecture
---------------------------
- In-memory sliding-window per IP (20 failures / 60 seconds).
- Reset on server restart — production rate limiting should be
  configured at the nginx level via ``limit_req_zone``.
- Auth failures are only recorded AFTER token validation fails —
  a missing ``Authorization`` header returns 401 immediately without
  consuming a rate-limit slot (no penalty for clients that don't know
  the token).

IP resolution
-------------
Direct client IP is used unless the connection comes from a trusted
proxy (loopback + private RFC 1918 ranges by default).  Trusted proxies
are parsed from ``FW_CACHE_TRUSTED_PROXIES`` env var (comma-separated
CIDRs).  When trusted, ``X-Forwarded-For`` is read — the leftmost IP
is the original client.
"""

from __future__ import annotations

import ipaddress as _ipaddress
import os as _os
import threading as _threading
import time as _time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def _extract_bearer(request: Request) -> str | None:
    """Extract the bearer token from the ``Authorization`` header.

    Returns ``None`` if the header is missing, malformed, or the token
    exceeds 1024 characters (preventing hash-DoS on large inputs).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        # Reject unreasonably large tokens before hashing.
        # SHA-256 input of 1 MB would waste server CPU — 1024 chars is
        # generous for a hex token (64 chars for 256 bits of entropy).
        if len(token) > 1024:
            return None
        return token
    return None


async def _lookup_permissions(request: Request, token: str) -> dict[str, Any] | None:
    """Query the meta database for token permissions.

    Returns a dict with ``project_id``, ``can_read``, ``can_write``,
    ``can_overwrite``, ``is_admin``, or ``None`` if the token is
    invalid or revoked.
    """
    backend = request.app.state.backend
    return await backend.validate_token(token)



# Simple in-memory IP-based rate limiter for auth failures.
# Tracks failed attempts per IP with a sliding 60s window.
#
# NOTE: This is intentionally IN-MEMORY ONLY — state resets on server
# restart.  For production deployments behind a reverse proxy (nginx),
# rate limiting should be configured at the proxy level via
# limit_req_zone / limit_req (see nginx documentation).
#
# Why in-memory (not Redis)?
# ---------------------------
# The cache server is designed to be lightweight — adding Redis as a
# dependency for rate limiting alone is disproportionate.  The nginx
# reverse proxy (always deployed in front) provides robust rate limiting.
# This in-memory limiter is a defense-in-depth measure for direct-access
# scenarios (development, internal networks without nginx).
_auth_failures: dict[str, list[float]] = {}
"""Maps IP address to list of failure timestamps (monotonic seconds)."""

_auth_lock = _threading.Lock()
"""Mutex for the ``_auth_failures`` dict — concurrent access from asyncio."""

_auth_check_count: int = 0
"""Monotonic counter — prune stale entries every ``_AUTH_PRUNE_INTERVAL`` checks."""
_AUTH_PRUNE_INTERVAL = 100  # prune stale entries every N checks
"""Why 100?  Pruning on every auth failure would add mutex contention.
   Every 100th check is frequent enough to prevent unbounded growth
   (worst case: 99 stale entries linger for one check cycle)."""


# Default trusted reverse-proxy CIDRs (loopback + private ranges).
# Override via FW_CACHE_TRUSTED_PROXIES env var (comma-separated CIDRs).
# These are the networks from which we trust X-Forwarded-For headers.
_DEFAULT_TRUSTED_PROXIES = frozenset({"127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"})


def _parse_trusted_proxies() -> frozenset[str]:
    """Parse FW_CACHE_TRUSTED_PROXIES env var, fall back to defaults.

    Returns a frozenset of CIDR strings.  Invalid entries are silently
    dropped — a typo in one CIDR should not crash the server startup.
    """
    raw = _os.environ.get("FW_CACHE_TRUSTED_PROXIES", "")
    if not raw:
        return _DEFAULT_TRUSTED_PROXIES
    cidrs: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                _ipaddress.ip_network(part)  # validate
                cidrs.add(part)
            except ValueError:
                pass
    return frozenset(cidrs) if cidrs else _DEFAULT_TRUSTED_PROXIES


_TRUSTED_PROXIES: frozenset[str] = _parse_trusted_proxies()
"""Parsed at module load time — changes require server restart."""


def _is_trusted_proxy(ip: str) -> bool:
    """Return True if *ip* is in the trusted proxy set.

    Used to decide whether to trust ``X-Forwarded-For`` from a given
    connection.  Only connections from known proxies (nginx, HAProxy)
    should have their forwarded headers trusted — otherwise an attacker
    could spoof ``X-Forwarded-For`` to bypass rate limiting or obscure
    their real IP.
    """
    if not ip:
        return False
    try:
        addr = _ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in _TRUSTED_PROXIES:
        if addr in _ipaddress.ip_network(cidr):
            return True
    return False


def _prune_auth_failures(now: float, window_s: float = 120.0) -> None:
    """Remove stale entries older than 2× the rate-limit window.

    Called periodically during ``_check_rate_limit`` to prevent the
    ``_auth_failures`` dict from growing without bound.  The 2× window
    (120 s for a 60 s rate-limit window) ensures entries are only
    removed when they are well past the rate-limit horizon — no risk
    of clearing entries still within the window.
    """
    stale = [
        ip for ip, times in _auth_failures.items()
        if not times or now - times[-1] > window_s
    ]
    for ip in stale:
        del _auth_failures[ip]


def _check_rate_limit(ip: str, max_failures: int = 20, window_s: float = 60.0) -> bool:
    """Return True if *ip* is under the rate limit, False if exceeded.

    Why 20 failures in 60 seconds?
    ------------------------------
    20 failures / 60 s is ~1 attempt every 3 seconds — much faster than
    any legitimate client would fail auth.  Legitimate clients cache
    their token and succeed on the first attempt.  Only brute-force
    attackers would hit this threshold; even then, the actual token
    space (256-bit hex) is far too large to brute-force at this rate.
    """
    global _auth_check_count
    now = _time.monotonic()
    _auth_check_count += 1
    if _auth_check_count % _AUTH_PRUNE_INTERVAL == 0:
        _prune_auth_failures(now)
    with _auth_lock:
        failures = _auth_failures.get(ip, [])
        # Remove expired entries (older than window_s)
        failures = [t for t in failures if now - t < window_s]
        if len(failures) >= max_failures:
            _auth_failures[ip] = failures
            return False
        _auth_failures[ip] = failures
        return True


def _record_auth_failure(ip: str) -> None:
    """Record a failed auth attempt for rate limiting.

    Called only after token validation fails — NOT on missing header.
    This means clients without any token (e.g. health-check probes)
    never consume rate-limit slots.
    """
    with _auth_lock:
        _auth_failures.setdefault(ip, []).append(_time.monotonic())


def _get_client_ip(request: Request) -> str:
    """Extract client IP, trusting X-Forwarded-For only from trusted proxies.

    Why not always use X-Forwarded-For?
    -----------------------------------
    Any client can set the ``X-Forwarded-For`` header.  If we blindly
    trusted it, an attacker could spoof their IP to bypass rate limiting
    or impersonate a trusted source.  We only trust the header when the
    direct connection comes from a known proxy (localhost, private network).
    """
    direct_ip = request.client.host if request.client else "unknown"
    if _is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # X-Forwarded-For: client, proxy1, proxy2, ...
            # The leftmost IP is the original client.
            return forwarded.split(",")[0].strip()
    return direct_ip


class CacheAuthMiddleware(BaseHTTPMiddleware):
    """Validates bearer tokens and attaches permissions to request.state.

    Order matters: this middleware runs BEFORE FastAPI route handlers.
    It authenticates the request and sets ``request.state.permissions``
    so route-level ``Depends`` functions (``require_can_read``, etc.)
    can do authorization without re-querying the database.

    Why Starlette middleware (not FastAPI dependency)?
    --------------------------------------------------
    FastAPI dependencies run per-route — you'd need to add ``Depends(...)``
    to every route manually.  Middleware runs on ALL requests automatically.
    Since auth is required on every endpoint except ``/health``, middleware
    is the correct layer — no risk of forgetting to add the dependency to
    a new route.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Health check is public — no auth required.
        # This is checked first to avoid a database round-trip for
        # every monitoring probe.
        if request.url.path == "/health":
            return await call_next(request)

        token = _extract_bearer(request)
        if token is None:
            return JSONResponse({"detail": "Missing Authorization header"}, status_code=401)

        # Validate token FIRST — only rate-limit actual failures.
        # A missing header returns 401 immediately without consuming
        # a rate-limit slot (no penalty for non-authenticating clients).
        perms = await _lookup_permissions(request, token)
        if perms is None:
            client_ip = _get_client_ip(request)
            if not _check_rate_limit(client_ip):
                return JSONResponse({"detail": "Too many auth attempts"}, status_code=429)
            _record_auth_failure(client_ip)
            return JSONResponse({"detail": "Invalid or revoked token"}, status_code=401)

        # Attach permissions to request.state — downstream auth checks
        # read from here without re-querying the database.
        request.state.permissions = perms
        response = await call_next(request)
        return response


def require_can_read(request: Request) -> JSONResponse | None:
    """Check that the authenticated token has ``can_read``.

    Returns ``None`` (allow) on success, or a ``JSONResponse`` (403) on
    failure.  Called as a FastAPI ``Depends`` — runs BEFORE the route
    handler's body parameter is parsed, so unauthorized clients never
    waste server CPU on request parsing.
    """
    perms = getattr(request.state, "permissions", {})
    if not perms.get("can_read", False):
        return JSONResponse({"detail": "Token does not have read permission"}, status_code=403)
    return None


def require_can_write(request: Request) -> JSONResponse | None:
    """Check that the authenticated token has ``can_write``.

    Pure permission check — no side effects on request.state.
    For write endpoints that need overwrite support, use
    ``require_can_write_with_overwrite`` instead.

    Why separate from require_can_write_with_overwrite?
    --------------------------------------------------
    Not all write endpoints use the overwrite header.  ``POST /cache/clear``
    requires ``can_write`` but never checks ``X-Cache-Overwrite``.  Having
    a simpler ``require_can_write`` avoids coupling the clear endpoint
    to overwrite logic it does not need.
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

    Why parse the header in the auth check?
    ---------------------------------------
    The header controls SQL behavior (INSERT vs UPSERT).  Validating
    permission at the auth layer means the route handler can read a
    simple boolean without repeating the permission check — single
    responsibility: auth checks auth, routes handle logic.
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
