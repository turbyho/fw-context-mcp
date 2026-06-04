"""Shared utilities used across fw-context-mcp.

Avoids duplication of project-root resolution, path normalisation, and
other small helpers that were copied into multiple modules.
"""

from __future__ import annotations

import os
from pathlib import Path

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
    *root*.
    """
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(root / p)
