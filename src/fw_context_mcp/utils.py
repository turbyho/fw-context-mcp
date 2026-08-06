"""Shared utilities used across fw-context-mcp.

Avoids duplication of project-root resolution, path normalisation, and
other small helpers that were copied into multiple modules.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import sqlite3
import subprocess
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fw_context_mcp.indexer.build import BuildConfig

from fw_context_mcp import _stdlib_sqlite3

log = logging.getLogger(__name__)

__all__ = [
    "MTIME_TOLERANCE_S",
    "abs_path",
    "compute_content_hash",
    "compute_source_hash",
    "fmt_count",
    "is_compile_commands_stale",
    "is_db_exception",
    "is_fatal",
    "read_file_lines",
    "resolve_project_root",
    "SAFE_EXCEPT",
    "truncate_path_middle",
]

# Seconds of tolerance when comparing file mtimes to account for
# clock skew between the indexer and the filesystem.
MTIME_TOLERANCE_S: float = 1.0

# Standard exception tuple for non-fatal recoverable errors.
# Use in all broad-except blocks where the operation can safely
# continue or log+skip.  Always pair with is_fatal() check first.
#
# Includes stdlib sqlite3.Error (not just pysqlite3) because
# external libraries that import sqlite3 before the pysqlite3
# redirect may raise the stdlib type.
_STDLIB_DB_ERROR = (_stdlib_sqlite3.Error,) if _stdlib_sqlite3 is not None else ()

SAFE_EXCEPT = (
    ValueError, TypeError, RuntimeError, AttributeError,
    sqlite3.Error, OSError,
) + _STDLIB_DB_ERROR


def is_fatal(exc: BaseException) -> bool:
    """Return True for exceptions that must never be swallowed."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError, SystemError))


def is_db_exception(exc: BaseException) -> bool:
    """Return True for sqlite3 database exceptions from either stdlib or pysqlite3.

    After :mod:`fw_context_mcp` redirects ``sqlite3`` → ``pysqlite3``, internal
    ``sqlite3.Error`` resolves to ``pysqlite3.dbapi2.Error``, but
    externally‑created connections raise stdlib ``sqlite3.Error`` — a
    different C‑extension type.  This helper matches both.

    Always call **before** :func:`is_fatal` — fatal exceptions
    (:class:`KeyboardInterrupt`, :class:`SystemExit`, :class:`MemoryError`)
    are not DB errors and must propagate.

    Usage::

        try:
            rows = conn.execute(...).fetchall()
        except Exception as exc:
            if not is_db_exception(exc):
                raise
            return None
    """
    if is_fatal(exc):
        return False
    if isinstance(exc, sqlite3.Error):
        return True
    if _stdlib_sqlite3 is not None and isinstance(exc, _stdlib_sqlite3.Error):
        return True
    return False

def run_build_command(
    cmd: list[str],
    cwd: Path,
    timeout: float = 600,
    description: str = "",
    env: dict[str, str] | None = None,
    build_cfg: BuildConfig | None = None,
) -> subprocess.CompletedProcess:
    """Run a build command with consistent timeout and output capture.

    All build-system invocations should use this helper instead of raw
    ``subprocess.run()`` to ensure consistent timeout and output capture
    behaviour across the nine builders.

    Args:
        cmd: Command and arguments as a list (shell=False is enforced).
        cwd: Working directory.
        timeout: Maximum time in seconds.
        description: Human-readable description for error messages.
        env: Optional environment variables dict (merged with os.environ).
        build_cfg: Optional BuildConfig — when set, ``activate`` wraps the
            command in ``bash -c "source <activate> && <cmd>"``, and
            ``extra_path`` / ``extra_env`` are merged into the environment.

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        RuntimeError: On non-zero exit or timeout.
    """
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    if build_cfg:
        if build_cfg.extra_path:
            merged_env["PATH"] = os.pathsep.join(build_cfg.extra_path) + os.pathsep + merged_env.get("PATH", "")
        if build_cfg.extra_env:
            merged_env.update(build_cfg.extra_env)
        if build_cfg.activate:
            expanded = str(Path(build_cfg.activate).expanduser())
            cmd_str = " ".join(shlex.quote(c) for c in cmd)
            cmd = ["bash", "-c", f"source {shlex.quote(expanded)} && {cmd_str}"]

    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=merged_env
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Build command timed out after {timeout}s: "
            f"{description or ' '.join(cmd)}"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(
            f"Build command failed (exit {result.returncode}): "
            f"{description or ' '.join(cmd)}\n"
            f"stderr: {result.stderr[:500]}"
        )
    return result

def resolve_project_root(explicit: str | Path | None = None) -> Path:
    """Return the project root directory.

    Resolution order:
    1. *explicit* path (resolved absolutely)
    2. Nearest git-root from ``$PWD``
    3. ``$PWD`` when no git repository is found
    """
    if explicit:
        return Path(explicit).resolve()
    cwd = Path(os.getcwd())
    p = cwd
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return cwd


def abs_path(root: Path, path: str) -> str:
    """Resolve a stored (potentially relative) file path to an absolute path.

    Absolute paths are returned unchanged; relative paths are joined with
    *root*.  An empty string is returned as-is (callers should guard
    against empty paths before calling).
    """
    if not path:
        return ""  # Empty path — caller should guard before calling
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(root / p)


# Cache with mtime-based invalidation — avoids stale data in daemon/server processes.
# Keyed by (path, mtime) so file changes are automatically detected.
_read_cache: OrderedDict[tuple[str, float], list[str]] = OrderedDict()
_read_cache_max = 500
_read_cache_lock = threading.Lock()


def read_file_lines(abs_path: str) -> list[str] | None:
    """Read all lines from a source file, detecting the encoding.

    Tries a sequence of common encodings for embedded C/C++ projects
    (UTF-8 first, then regional code pages, then repair mode as last
    resort).  Returns ``None`` when the file cannot be read at all.

    Results are cached by (path, mtime) — file changes invalidate the cache.
    """
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return None
    cache_key = (abs_path, mtime)
    with _read_cache_lock:
        if cache_key in _read_cache:
            _read_cache.move_to_end(cache_key)
            return _read_cache[cache_key]

    # NOTE: UTF-16 files will produce garbled content; extremely low risk
    # in embedded C/C++ projects.
    encodings = ["utf-8", "cp1252", "latin1"]
    for encoding in encodings:
        try:
            with open(abs_path, encoding=encoding) as f:
                result = f.readlines()
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except (FileNotFoundError, OSError):
            return None
    else:
        # Last resort — replace invalid bytes
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                result = f.readlines()
        except (FileNotFoundError, OSError):
            return None

    # Evict oldest entry if at capacity (LRU via OrderedDict — move_to_end on hit)
    with _read_cache_lock:
        if len(_read_cache) >= _read_cache_max:
            _read_cache.popitem(last=False)
        _read_cache[cache_key] = result
    return result


def compute_content_hash(body: str, qualified_name: str, signature: str, docstring: str) -> str:
    """Stable SHA256 hex fingerprint of a symbol's content.

    Used for content-addressable LLM analysis caching — survives
    re-indexes, config changes, and branch switches.
    """
    # \x1f (Unit Separator) — safe delimiter not occurring in C/C++ identifiers,
    # signatures, or docstrings.  Changed from a plain printable delimiter to
    # avoid collisions with symbols whose names contain the old separator.
    raw = f"{body.strip()}\x1f{qualified_name}\x1f{signature or ''}\x1f{(docstring or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def compute_source_hash(file_path: Path) -> str:
    """Return SHA-256 hex digest of file content, or empty string on error."""
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def escape_like(value: str) -> str:
    """Escape LIKE wildcards ``%``, ``_``, and the escape char ``\\``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_compile_commands_stale(
    created_at: str,
    compile_commands_path: str | Path,
    tolerance_s: float = MTIME_TOLERANCE_S,
) -> tuple[bool, str | None]:
    """Check if compile_commands.json is newer than *created_at* timestamp.

    Returns ``(is_stale, reason)`` where *reason* is ``None`` when the
    compile_commands file is present and up to date, or a string like
    ``"compile_commands_missing"`` or ``"compile_commands_newer"`` when
    the index should be considered stale.

    A missing ``compile_commands.json`` returns ``(True,
    "compile_commands_missing")`` — the index IS stale when its source
    of truth has disappeared.
    """
    try:
        cc_path = Path(compile_commands_path)
        if not cc_path.exists():
            return True, "compile_commands_missing"
        cc_mtime = os.path.getmtime(cc_path)
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        indexed_at = dt
        if cc_mtime > indexed_at.timestamp() + tolerance_s:
            return True, "compile_commands_newer"
        return False, None
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        log.warning("is_compile_commands_stale failed for %s: %s", compile_commands_path, e)
        return True, f"stale_check_error: {e}"


def truncate_path_middle(path: str, max_len: int) -> str:
    """Truncate a path by removing the middle, keeping start and end visible.

    Example: ``/home/turbyho/dev/sw/…/privat/HA_Boiler``
    """
    if len(path) <= max_len:
        return path
    keep_start = max(max_len // 3, 12)
    keep_end = max_len - keep_start - 1
    return path[:keep_start] + "…" + path[-keep_end:]


def fmt_count(n: int) -> str:
    """Format a count with thousand separators (non-breaking thin spaces)."""
    if n < 0:
        return "-" + f"{abs(n):_d}".replace("_", " ")
    return f"{n:_d}".replace("_", " ")
