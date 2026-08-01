"""MCP shared context — thin re-export layer.

Implementation lives in:
- ``connection.py`` — connection caching, integrity checks
- ``readiness.py`` — project readiness, staleness detection

Import from here as before::

    from .context import _open_db_safe, _resolve_context
"""

from .connection import (
    HandlerContext,
    _invalidate_conn_cache,
    _open_db_or_return,
    _open_db_safe,
    _resolve_handler_context,
)
from .readiness import (
    _check_server_ready,
    _db_path,
    _detect_build_system,
    _is_stale,
    _resolve_context,
)

__all__ = [
    "_check_server_ready",
    "_db_path",
    "_detect_build_system",
    "_is_stale",
    "_open_db_or_return",
    "_open_db_safe",
    "_resolve_context",
    "_invalidate_conn_cache",
    "_resolve_handler_context",
    "HandlerContext",
]
