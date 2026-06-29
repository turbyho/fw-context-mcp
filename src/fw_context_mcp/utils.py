"""Shared utilities used across fw-context-mcp.

Avoids duplication of project-root resolution, path normalisation, and
other small helpers that were copied into multiple modules.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["MTIME_TOLERANCE_S", "abs_path", "compute_content_hash", "resolve_project_root"]

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


def compute_content_hash(body: str, qualified_name: str, signature: str, docstring: str) -> str:
    """Stable SHA256 hex fingerprint of a symbol's content.

    Used for content-addressable LLM analysis caching — survives
    re-indexes, config changes, and branch switches.
    """
    raw = f"{body.strip()}|{qualified_name}|{signature or ''}|{(docstring or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()
