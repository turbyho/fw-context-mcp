"""Git context helpers — extract branch and tag info for build description."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Hard-coded UTF-8 — git outputs UTF-8 on all modern platforms regardless of
# locale, and branch/tag names can contain Unicode characters.
_GIT_ENCODING = "utf-8"
_GIT_TIMEOUT = 5  # seconds — prevents hangs on NFS or corrupt .git directories


def get_git_description(project_root: Path) -> str:
    """Return a description of the git context at *project_root*.

    Format: ``"branch: main, tag: v2.1.0"`` — or just ``"branch: main"``
    when no tag exists.  Best-effort: returns an empty string when git is
    not available, *project_root* is not a git repository, or any git
    command fails.

    Used to populate the ``description`` column in ``build_configs`` so
    each indexed build carries a human-readable record of which branch
    and release tag it was built from.
    """
    parts: list[str] = []

    # ── Branch ──
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_root),
            text=True,
            encoding=_GIT_ENCODING,
            timeout=_GIT_TIMEOUT,
            stderr=subprocess.DEVNULL,
        ).strip()
        # Detached HEAD reports "HEAD" — semantically misleading in a
        # description, so treat it as no branch.
        if branch and branch != "HEAD":
            parts.append(f"branch: {branch}")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass

    # ── Last tag on HEAD ──
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(project_root),
            text=True,
            encoding=_GIT_ENCODING,
            timeout=_GIT_TIMEOUT,
            stderr=subprocess.DEVNULL,
        ).strip()
        if tag:
            parts.append(f"tag: {tag}")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass

    return ", ".join(parts)
