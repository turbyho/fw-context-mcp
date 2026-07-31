"""MCP shared context — thin re-export layer.

Implementation lives in:
- ``connection.py`` — connection caching, integrity checks
- ``readiness.py`` — project readiness, staleness detection

Import from here as before::

    from .context import _open_db_safe, _resolve_context
"""

from .connection import (
    _check_cached_conn,
    _cleanup_conn_cache_atexit,
    _ConnCacheEntry,
    _conn_cache,
    _conn_cache_lock,
    _CONN_TTL,
    _evict_stale_entry,
    _integrity_checked,
    _integrity_lock,
    _invalidate_conn_cache,
    _invalidate_conn_cache_if_reindex_done,
    _open_and_cache,
    _open_db_or_return,
    _open_db_safe,
    _resolve_handler_context,
    HandlerContext,
)

from .readiness import (
    _check_server_ready,
    _db_path,
    _detect_build_system,
    _is_stale,
    _PROJECT_READY_TTL,
    _project_ready_cache,
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
