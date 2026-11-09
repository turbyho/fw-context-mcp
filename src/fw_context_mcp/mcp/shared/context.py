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
- The thin wrapper ``_normalize_file_path_query`` lives here because it
  is truly shared — every lookup tool uses path normalisation.  Putting it
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
from pathlib import Path

from ...path_key import index_key
from .connection import (
    HandlerContext,
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


def _normalize_file_path_query(path: str, root: Path) -> str:
    """Give the index key of the file *path* that a caller wrote, for the project at *root*.

    The index keys come from :func:`fw_context_mcp.path_key.index_key`: the
    separator ``/`` on each system, the case of the disk, relative to the
    root inside the project.  The query must get the same form:

    - On Windows a ``\\`` of the caller becomes ``/``.  WHY only there: on
      Linux and macOS a backslash is a character of a file name.
    - The path then goes through ``index_key`` with *root*: a relative path
      is relative to the root, ``..`` goes away, an absolute path inside
      the project becomes relative, and each part that exists gets the case
      of the disk.
    - A bare name that is not in the root (``main.cpp``) keeps its text.
      The lookup then finds it by the last part of the key.

    Why centralised versus per-handler normalisation: every tool handler
    that accepts a ``file_path`` parameter must call this before it queries
    the index, and a change of the key rule must change one place.
    """
    path = _written_path(path)
    # A NUL byte is not part of any file name, and the file system calls of
    # index_key raise ValueError for it: the lookup then finds no file.
    if not path or "\0" in path:
        return path
    return index_key(root / path, root)


def _written_path(path: str) -> str:
    """Give *path* as the caller wrote it, with the separator ``/``.

    The lookup of a bare name (``_resolve_indexed_file``) uses this text
    and not the key: ``index_key`` resolves links, and a link in the root
    can make a partial path absolute.  On Windows ``\\`` becomes ``/``; on
    Linux and macOS a backslash is a character of a file name.
    """
    return path.replace("\\", "/") if os.name == "nt" else path


__all__ = [
    "HandlerContext",
    "_check_server_ready",
    "_db_path",
    "_detect_build_system",
    "_is_stale",
    "_normalize_file_path_query",
    "_quick_open_readonly",
    "_resolve_context",
    "_resolve_handler_context",
    "_written_path",
    "get_executor",
    "interrupt_all",
    "invalidate_executor",
]
