"""Git context helpers — the branch, the tag and the HEAD of the tree of a build.

WHY git context: each indexed build stores a ``description`` column in
``build_configs``.  When a developer looks at ``get_project_status`` output
and sees multiple config_hashes, the git context tells them WHICH branch
each index was built from — essential for debugging "why is symbol X
missing?" across feature branches.

Each build also records the text of the file HEAD (``git_head``).  A
checkout of another branch, tag or commit changes it, and
``head_moved`` compares it with the tree: compile_commands.json belongs to
the checkout it was generated on.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
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


# The file HEAD of each project root, with the time of the lookup.  WHY a
# cache for the process: the place of the file does not change while the
# tree exists, and each MCP query reads HEAD.  One `git` call for each root,
# then one read of a small file for each query.  A root without git is asked
# again after _NO_GIT_RECHECK_S: a `git init` while a server runs is then
# seen, and a tree without git costs one `git` call a minute, not one for
# each query.
_HEAD_PATHS: dict[str, tuple[Path | None, float]] = {}
_NO_GIT_RECHECK_S = 60.0

_REF_PREFIX = "ref: "
_BRANCH_REF_PREFIX = "ref: refs/heads/"
# The directories in which git keeps the state of a rebase that runs.  Each
# holds `head-name`, the branch that the rebase updates when it ends.
_REBASE_DIRS = ("rebase-merge", "rebase-apply")
_SHA_RX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def head_path(project_root: Path) -> Path | None:
    """Return the file HEAD of the repository at *project_root*, or None without git.

    ``git rev-parse --git-path HEAD`` gives it, thus a worktree, where
    ``.git`` is a file, gets the HEAD of its own checkout, and a project in
    a subdirectory of the repository gets the HEAD of that repository.  The
    path is absolute, thus a later change of the working directory does
    not change the answer.
    """
    root = project_root.resolve()
    key = str(root)
    now = time.monotonic()
    cached = _HEAD_PATHS.get(key)
    if cached is not None and (cached[0] is not None or now - cached[1] < _NO_GIT_RECHECK_S):
        return cached[0]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--git-path", "HEAD"],
            cwd=str(root),
            text=True,
            encoding=_GIT_ENCODING,
            timeout=_GIT_TIMEOUT,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeDecodeError):
        out = ""
    # The path is relative to the directory of the call, or absolute.
    path = (root / out) if out else None
    _HEAD_PATHS[key] = (path, now)
    return path


def read_head(project_root: Path) -> str:
    """Return the checkout of the tree at *project_root*: the text of the file HEAD, or "" without git.

    ``ref: refs/heads/<branch>`` on a branch, the SHA of the commit on a
    detached HEAD (a checkout of a tag or of a commit).  WHY the text of
    the file and not the commit: a commit on a branch changes the branch
    and not HEAD, and it changes no file of the tree that the watcher does
    not see.  A checkout changes HEAD, and a checkout makes
    compile_commands.json of the old tree wrong.

    Two cases where HEAD moves without a checkout:

    * A rebase detaches HEAD and moves it to each commit that it makes.
      While it runs, the answer is the branch that git names in
      ``head-name`` of its state directory: the branch that the rebase
      updates when it ends.  The changed files reach the index as the files
      of a commit do.
    * A commit on a detached HEAD moves it to the new commit.
      :func:`head_moved` reads the reflog of HEAD for that case.
    """
    path = head_path(project_root)
    if path is None:
        return ""
    for name in _REBASE_DIRS:
        try:
            rebased = (path.parent / name / "head-name").read_text(encoding=_GIT_ENCODING).strip()
        except (OSError, UnicodeDecodeError):
            continue
        # A rebase of a detached HEAD writes "detached HEAD": no branch.
        if rebased.startswith("refs/"):
            return _REF_PREFIX + rebased
    try:
        return path.read_text(encoding=_GIT_ENCODING).strip()
    except (OSError, UnicodeDecodeError):
        return ""


def head_label(head: str) -> str:
    """Return a short name of a HEAD text: the branch, or ``commit <sha>`` for a detached HEAD."""
    if head.startswith(_BRANCH_REF_PREFIX):
        return head[len(_BRANCH_REF_PREFIX):]
    if head.startswith(_REF_PREFIX):
        return head[len(_REF_PREFIX):]
    return f"commit {head[:12]}" if head else ""


def _committed_from(project_root: Path, indexed: str, live: str) -> bool:
    """Say whether HEAD went from the commit *indexed* to the commit *live* by commits alone.

    The reflog of HEAD (``logs/HEAD`` next to the file HEAD) has one line
    for each move: ``<old> <new> <who> <when>\\t<message>``, and the message
    of a commit starts with ``commit``.  The newest lines are read back to
    the line that left *indexed*.  Any other move on the way (a checkout, a
    reset, a rebase) is a change of the checkout.  Without a reflog the
    answer is no, thus the move counts as a checkout.
    """
    if not (_SHA_RX.fullmatch(indexed) and _SHA_RX.fullmatch(live)):
        return False
    path = head_path(project_root)
    if path is None:
        return False
    try:
        lines = (path.parent / "logs" / "HEAD").read_text(encoding=_GIT_ENCODING, errors="replace").splitlines()
    except OSError:
        return False
    current = live
    for line in reversed(lines):
        meta, _, message = line.partition("\t")
        fields = meta.split(" ", 2)
        if len(fields) < 3 or fields[1] != current or not message.startswith("commit"):
            return False
        if fields[0] == indexed:
            return True
        current = fields[0]
    return False


def head_moved(
    indexed_head: str, description: str, live_head: str, project_root: Path | None = None,
) -> tuple[str, str]:
    """Say whether the tree has a different checkout than a build of the index.

    Returns the labels ``(indexed, live)`` (:func:`head_label`) when they
    differ, and ``("", "")`` when they agree or when either side is unknown.

    *indexed_head* is the HEAD that the build recorded.  An index of an
    older version recorded none: then the branch of its *description* is
    compared with the branch of *live_head*, as before, and a detached HEAD
    on either side reads as unknown.  With *project_root*, a detached HEAD
    that only commits moved is no change of the checkout
    (:func:`_committed_from`).
    """
    if indexed_head:
        if not live_head or live_head == indexed_head:
            return "", ""
        if project_root is not None and _committed_from(project_root, indexed_head, live_head):
            return "", ""
        return head_label(indexed_head), head_label(live_head)
    indexed_branch = branch_of_description(description)
    live_branch = head_label(live_head) if live_head.startswith(_BRANCH_REF_PREFIX) else ""
    if not indexed_branch or not live_branch or indexed_branch == live_branch:
        return "", ""
    return indexed_branch, live_branch


def stored_head(row) -> str:
    """Return the HEAD that a ``build_configs`` row recorded, or "".

    The column is not in the fingerprint of the schema
    (``upsert_build_config``), thus an index of an older version has no
    such column, and a row of a run of that version holds NULL.
    """
    if row is None or "git_head" not in row.keys():
        return ""
    return str(row["git_head"] or "")
