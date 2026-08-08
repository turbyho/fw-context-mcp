"""MCP shared context — single import hub for all handler dependencies.

Why a re-export layer instead of direct imports from submodules:

- Handler files (``_lookup.py``, ``_search.py``, ``_callgraph.py``, …)
  need the same six to eight shared symbols from three different modules
  (``connection.py``, ``readiness.py``, ``executor.py``).  Without a
  single import target, every handler would have three import blocks
  and every refactor would touch N files instead of one.
- The re-export layer is the single place where ``__all__`` is
  maintained — adding or removing a shared symbol updates exactly one
  line in one file, and static analysis tools see the complete public
  API in one place.
- The thin-wrapper pattern (``_server_init_error``, ``_normalize_file_path_query``)
  lives here because it is truly shared — every tool handler uses the
  sentinel and every lookup tool uses path normalisation.  Putting them
  here avoids circular imports (``connection.py`` does not import
  handler-level state).

Implementation lives in:
- ``connection.py`` — connection caching, integrity checks,
  ``HandlerContext``, ``_resolve_handler_context``
- ``readiness.py`` — project readiness, staleness detection, build
  system detection
- ``executor.py`` — ``SyncQueryExecutor`` (single-connection synchronous
  query runner), ``get_executor``, ``invalidate_executor``, ``interrupt_all``

Import from here as before::

    from .context import _quick_open_readonly, _resolve_context, get_executor
"""

from __future__ import annotations

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

# ── Server init error sentinel ─────────────────────────────────────────────
#
# Sentinel for detecting an unready project at MCP server startup.
# Set in main() before mcp.run().  When the project is not initialized
# or has no index, all tool handlers return the same error message.
#
# Why a module-level sentinel instead of per-request readiness checks:
#
# - The project readiness decision (has .fw-context/ dir?  has index.db?)
#   is made once at startup and cannot change without a reindex or init —
#   both are explicit operator actions, not runtime events.
# - Running the readiness check on every tool call adds ~2 ms of file
#   stat calls + config parsing per request — waste for 100% of requests
#   when the project is ready.
# - The sentinel provides early feedback: the MCP client sees the error
#   on the very first tool call, not after a timeout.
# - If readiness changes (operator initialises the project), the server
#   must restart anyway — the IndexTracker is already running.
_server_init_error: str | None = None


def _set_server_init_error(message: str) -> None:
    """Set the error message that all tool handlers see.

    Call only in main() before the server starts.  After the sentinel
    is set, every tool handler fails with this message — the LLM
    forwards it to the user.
    """
    global _server_init_error
    _server_init_error = message


def _normalize_file_path_query(path: str) -> str:
    """Normalize a file path for DB lookup.

    The index stores paths with OS-native separators. On Windows this
    converts forward slashes to backslashes so user-supplied paths
    match the stored format. On Linux/macOS it is a no-op.

    Why centralised versus per-handler normalisation:

    - Every tool handler that accepts a ``file_path`` parameter must
      call this before querying the index.  Inlining the
      ``os.name == "nt"`` check in 12 handlers would scatter a
      one-line platform concern.
    - If the normalisation rule changes (e.g., canonical case for
      case-insensitive filesystems), only this function is updated.
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
    "_server_init_error",
    "_set_server_init_error",
    "HandlerContext",
    "get_executor",
    "invalidate_executor",
    "interrupt_all",
]
