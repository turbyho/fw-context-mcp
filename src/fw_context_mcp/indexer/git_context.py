"""Git context helpers — extract branch and tag info for build description.

WHY git context: each indexed build stores a ``description`` column in
``build_configs``.  When a developer looks at ``get_active_build`` output
and sees multiple config_hashes, the git context tells them WHICH branch
each index was built from — essential for debugging "why is symbol X
missing?" across feature branches.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Hard-coded UTF-8 — git outputs UTF-8 on all modern platforms regardless of
# locale, and branch/tag names can contain Unicode characters.
_GIT_ENCODING = "utf-8"
_GIT_TIMEOUT = 5  # seconds; may fail on repos with 20K+ tags or NFS — degrades gracefully — prevents hangs on NFS or corrupt .git directories


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
            stderr=subprocess.PIPE,
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
            stderr=subprocess.PIPE,
        ).strip()
        if tag:
            parts.append(f"tag: {tag}")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass

    return ", ".join(parts)


_BRANCH_PREFIX = "branch: "


def branch_of_description(description: str) -> str:
    """Return the branch a stored ``description`` names, or an empty string.

    The format is the one `get_git_description` writes:
    ``"branch: main, tag: v2.1.0"``.  A description written when HEAD was
    detached carries no branch part and gives an empty string.
    """
    for part in description.split(","):
        stripped = part.strip()
        if stripped.startswith(_BRANCH_PREFIX):
            return stripped[len(_BRANCH_PREFIX):].strip()
    return ""


def current_branch(project_root: Path) -> str:
    """Return the branch checked out at *project_root*, or an empty string.

    Empty for a detached HEAD, for a directory that is not a repository, and
    for any git failure — the same rule `get_git_description` follows, so the
    two agree on what "no branch" means.
    """
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_root),
            text=True,
            encoding=_GIT_ENCODING,
            timeout=_GIT_TIMEOUT,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            UnicodeDecodeError):
        return ""
    return "" if branch == "HEAD" else branch


def branch_moved_since(description: str, project_root: Path) -> tuple[str, str]:
    """Say whether the tree is on a different branch than the index.

    Returns ``(indexed_branch, current_branch)`` when they differ, and
    ``("", "")`` when they agree or when either is unknown.

    WHY this matters more than a plain reindex: a branch switch changes the
    source tree AND the compiler flags, and `compile_commands.json` is a
    build artifact of the branch it was generated on.  Measured on
    zbox-ecb-fw, switching from 4.15.3 to 4.15.1 left two generated zcbor
    sources listed in that file and absent from the tree.  A reindex reads
    the same stale file; only a build regenerates it.

    WHY the branch and not the whole description: the description also
    carries the last tag, and a new tag on the same branch moves nothing.
    Comparing whole strings would ask for a build after every release tag.

    WHAT THIS DOES NOT SEE: a move that leaves no branch name — checking out
    a tag or a commit detaches HEAD, and both sides then read as unknown.
    The index reports nothing rather than guessing, and the ordinary
    staleness checks still see the changed files.
    """
    indexed = branch_of_description(description)
    if not indexed:
        return "", ""
    current = current_branch(project_root)
    if not current or current == indexed:
        return "", ""
    return indexed, current
