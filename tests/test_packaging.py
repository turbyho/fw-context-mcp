"""Guards for the file selection of the sdist.

Hatchling reads ``.gitignore`` to select the files for the sdist.  Before it
applies the patterns, it tests the project root against them
(``hatchling/builders/config.py``, ``load_vcs_exclusion_patterns``).  On a
match it drops every pattern, thus the sdist receives each ignored local
file: the caches of the AI tools, the local debug scripts, and the private
notes.

An unanchored directory pattern causes this match when the absolute path of
the checkout holds a directory with the same name.  The pattern ``work/``
matched ``/home/<user>/dev/sw/work/tools/fw-context-mcp``, and it also
matches a GitHub Actions checkout below ``/home/runner/work``.  The result
was an sdist of 11.7 MB against a wheel of 0.7 MB.  Release 0.27.6 anchored
the pattern to ``/work/``.

These tests hold the fix in place.  The defect is silent: the build gives no
warning, and only the size of the artifact shows it.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = PROJECT_ROOT / ".gitignore"


def _unanchored_patterns() -> list[str]:
    """Return the patterns that git matches at any depth.

    A pattern with a slash inside it, or with a leading slash, is relative to
    the directory of the ``.gitignore`` file.  Such a pattern cannot match a
    parent directory of the checkout.  Every other pattern matches at any
    depth, thus only this class can hit the project root.
    """
    patterns = []
    for raw_line in GITIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # A negation makes fewer paths match, thus it cannot cause the defect.
        if line.startswith("!"):
            continue
        body = line.rstrip("/")
        if "/" in body:
            continue
        patterns.append(body)
    return patterns


def test_gitignore_exists():
    assert GITIGNORE.is_file(), f"no .gitignore at {GITIGNORE}"


def test_project_root_does_not_match_gitignore():
    """No ignore pattern is allowed to match a component of the root path.

    This is the condition that makes hatchling discard the complete
    ``.gitignore``.  A failure here means that the sdist of this checkout
    holds every local file, thus fix the pattern or move the checkout.
    """
    # Skip the filesystem root, because it is "/" and matches no pattern.
    components = PROJECT_ROOT.parts[1:]
    offenders = sorted(
        {
            f"pattern {pattern!r} matches the path component {component!r}"
            for pattern in _unanchored_patterns()
            for component in components
            if fnmatch.fnmatch(component, pattern)
        }
    )
    assert not offenders, (
        f"hatchling drops the complete .gitignore for the root {PROJECT_ROOT}, "
        f"thus the sdist gets every ignored local file:\n  "
        + "\n  ".join(offenders)
        + "\nFix: give the pattern a leading slash to anchor it to the project root."
    )


def test_gitignore_still_covers_the_local_work_directory():
    """The anchor must keep the ignore for the ``work`` directory of the root.

    The fix for the sdist changed ``work/`` to ``/work/``.  The directory
    holds local scripts, thus git must still ignore it.
    """
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("no .git directory — the test runs from an unpacked sdist")

    result = subprocess.run(
        ["git", "check-ignore", "-v", "work/"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "git does not ignore the work/ directory of the project root"
    assert "/work/" in result.stdout, f"an unexpected pattern covers work/: {result.stdout.strip()}"
