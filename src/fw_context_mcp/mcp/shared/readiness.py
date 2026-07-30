"""Project readiness checks for MCP handlers.

Extracted from ``context.py`` — verifies project initialization,
index existence, build system detection, and compile_commands staleness.

External users should continue importing from ``.context``
which re-exports everything from this module.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fw_context_mcp import config
from fw_context_mcp.utils import resolve_project_root

log = logging.getLogger(__name__)

# ── Project ready cache ─────────────────────────────────────────────────
_project_ready_cache: dict[str, tuple[float, str | None]] = {}
_project_ready_lock = threading.Lock()
_PROJECT_READY_TTL = 30  # seconds


def _check_server_ready(project_root: Path | None = None) -> Path:
    """Check that *project_root* has been initialized and indexed.

    Returns the resolved project root.  Raises ``RuntimeError`` with
    user-facing instructions when the project is not ready.

    Results are cached per project root with a 30-second TTL so repeated
    MCP tool calls within a single conversation don't pay the filesystem
    overhead.  Thread-safe — guarded by ``_project_ready_lock``.
    """
    import time

    global _project_ready_cache
    now = time.monotonic()

    root = resolve_project_root(project_root)
    if root is None:
        msg = (
            "No project root found.  Run:\n"
            "  fw-context init          # to initialize a new project\n"
            "  fw-context index --build # to build and index\n"
            "Or pass project_root explicitly."
        )
        cache_key = "__none__"
        with _project_ready_lock:
            _project_ready_cache[cache_key] = (now, msg)
        raise RuntimeError(msg)

    cache_key = str(root)
    with _project_ready_lock:
        if cache_key in _project_ready_cache:
            ts, error = _project_ready_cache[cache_key]
            if now - ts < _PROJECT_READY_TTL:
                if error:
                    raise RuntimeError(error)
                return root

    cfg = config.load(root)
    if not cfg.project.id:
        msg = (
            f"Project at {root} has no project ID.  Run:\n"
            "  fw-context init"
        )
        with _project_ready_lock:
            _project_ready_cache[cache_key] = (now, msg)
        raise RuntimeError(msg)

    db_path = cfg.index.db_dir / cfg.project.id / "index.db"
    if not db_path.exists():
        msg = (
            f"No index found at {db_path}.  Run:\n"
            "  fw-context index --build"
        )
        with _project_ready_lock:
            _project_ready_cache[cache_key] = (now, msg)
        raise RuntimeError(msg)

    with _project_ready_lock:
        _project_ready_cache[cache_key] = (now, None)
    return root


def _db_path(project_root: Path | None) -> Path:
    """Resolve the database path for a project root."""
    root = _check_server_ready(project_root)
    cfg = config.load(root)
    return cfg.index.db_dir / cfg.project.id / "index.db"


def _resolve_context(project_root: Path | None, *, skip_ready_check: bool = False):
    """Resolve all context: (db_path, config, project_id, root).

    When *skip_ready_check* is True, skips the server-ready validation
    (caller handles the missing-index case itself, e.g. ``reindex_file_impl``).
    """
    if skip_ready_check:
        root = resolve_project_root(project_root)
        if root is None:
            raise RuntimeError("No project root found")
    else:
        root = _check_server_ready(project_root)
    cfg = config.load(root)
    db_path = cfg.index.db_dir / cfg.project.id / "index.db"
    return db_path, cfg, cfg.project.id, root


def _is_stale(cfg, compile_commands_path: Path) -> bool:
    """Check if compile_commands.json is newer than the index timestamp.

    *cfg* is a ``sqlite3.Row`` from ``get_active_config()`` or a dict
    with ``created_at`` and ``compile_commands_path`` keys.
    """
    try:
        from fw_context_mcp.utils import is_compile_commands_stale
        created_at = cfg.get("created_at") if hasattr(cfg, "get") else cfg["created_at"]
        return is_compile_commands_stale(created_at, compile_commands_path)
    except (KeyError, Exception):
        return False


def _detect_build_system(root: Path) -> str:
    """Detect the build system for a project root."""
    try:
        from fw_context_mcp.indexer.build import detect_build_system
        result = detect_build_system(root)
        return result if result is not None else "unknown"
    except Exception:
        return "unknown"
