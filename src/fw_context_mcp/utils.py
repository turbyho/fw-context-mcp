"""Shared utilities used across fw-context-mcp.

Avoids duplication of project-root resolution, path normalisation, and
other small helpers that were copied into multiple modules.
"""

from __future__ import annotations


import logging

log = logging.getLogger(__name__)
from collections import OrderedDict
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "MTIME_TOLERANCE_S",
    "abs_path",
    "compute_content_hash",
    "compute_source_hash",
    "fmt_count",
    "is_compile_commands_stale",
    "read_file_lines",
    "resolve_project_root",
    "truncate_path_middle",
]

# Seconds of tolerance when comparing file mtimes to account for
# clock skew between the indexer and the filesystem.
MTIME_TOLERANCE_S: float = 1.0


def resolve_project_root(explicit: str | None = None) -> Path:
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
    if len(_read_cache) >= _read_cache_max:
        _read_cache.popitem(last=False)
    _read_cache[cache_key] = result
    return result


def compute_content_hash(body: str, qualified_name: str, signature: str, docstring: str) -> str:
    """Stable SHA256 hex fingerprint of a symbol's content.

    Used for content-addressable LLM analysis caching — survives
    re-indexes, config changes, and branch switches.
    """
    raw = f"{body.strip()}\x1f{qualified_name}\x1f{signature or ''}\x1f{(docstring or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def compute_source_hash(file_path: Path) -> str:
    """Return SHA-256 hex digest of file content, or empty string on error."""
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def is_compile_commands_stale(
    created_at: str,
    compile_commands_path: str | Path,
    tolerance_s: float = MTIME_TOLERANCE_S,
) -> bool:
    """Check if compile_commands.json is newer than *created_at* timestamp.

    Returns False on any error (missing file, bad timestamp, OSError)
    so staleness checks don't block queries or CLI output.
    """
    try:
        cc_path = Path(compile_commands_path)
        if not cc_path.exists():
            return False
        cc_mtime = os.path.getmtime(cc_path)
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        indexed_at = dt
        return cc_mtime > indexed_at.timestamp() + tolerance_s
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        log.warning("is_compile_commands_stale failed for %s: %s", compile_commands_path, e)
        return False


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
    return f"{n:_d}".replace("_", " ")
