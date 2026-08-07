"""MCP shared context — thin re-export layer.

Implementation lives in:
- ``connection.py`` — connection caching, integrity checks
- ``readiness.py`` — project readiness, staleness detection
- ``executor.py`` — single-connection sync query executor (all handler
  DB access goes through ``get_executor(...).execute_sync(...)``)

Import from here as before::

    from .context import _quick_open_readonly, _resolve_context, get_executor
"""

import os

from .connection import (
    HandlerContext,
    _integrity_checked,  # noqa: F401 — re-exported for server.py
    _quick_open_readonly,
    _resolve_handler_context,
)
from .executor import (
    get_executor,
    interrupt_all,
    invalidate_executor,
)
from .readiness import (
    _check_server_ready,
    _db_path,
    _detect_build_system,
    _is_stale,
    _resolve_context,
)


def _normalize_file_path_query(path: str) -> str:
    """Normalize a file path for DB lookup.

    The index stores paths with OS-native separators. On Windows this converts
    forward slashes to backslashes so user-supplied paths match the stored format.
    On Linux/macOS it is a no-op.
    """
    if os.name == "nt":
        return path.replace("/", "\\")
    return path

__all__ = [
    "_check_server_ready",
    "_db_path",
    "_detect_build_system",
    "_is_stale",
    "_normalize_file_path_query",
    "_quick_open_readonly",
    "_resolve_context",
    "_resolve_handler_context",
    "HandlerContext",
    "get_executor",
    "invalidate_executor",
    "interrupt_all",
]
