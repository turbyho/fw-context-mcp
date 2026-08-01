"""fw-context-mcp — build-aware code intelligence for embedded projects."""

import importlib.metadata
import sqlite3 as _stdlib_sqlite3  # noqa: F401 — used by utils.is_db_exception()
import sys

try:
    __version__ = importlib.metadata.version("fw-context-mcp")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0-dev"

# Replace stdlib sqlite3 with pysqlite3 before any submodule imports it.
# The stdlib sqlite3 on macOS (pyenv without --enable-loadable-sqlite-extensions)
# lacks enable_load_extension(), which is required by sqlite-vec.
# pysqlite3 provides a build of SQLite with extension support and pre-built
# wheels for macOS, Linux, and Windows.
# Placed here in __init__.py so it activates before EVERY submodule's
# ``import sqlite3`` — ensures consistent exception classes across all
# files (stdlib sqlite3.Error and pysqlite3.Error are different C extension
# types; catching the wrong one would silently miss DB exceptions).
try:
    import pysqlite3

    sys.modules["sqlite3"] = pysqlite3

    # pysqlite3.dbapi2.Row (C extension) lacks .get() method that stdlib
    # sqlite3.Row has in Python 3.13+.  Replace with a Python subclass
    # that adds .get() compatibility.
    _OrigRow = pysqlite3.dbapi2.Row
    class _CompatRow(_OrigRow):
        if not hasattr(_OrigRow, "get"):
            def get(self, key, default=None):
                try:
                    return self[key]
                except (KeyError, IndexError):
                    return default
    pysqlite3.dbapi2.Row = _CompatRow
    pysqlite3.Row = _CompatRow
except ImportError:
    pass
