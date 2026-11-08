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

The work material of the project is in git on a private remote, thus
``.gitignore`` does not exclude it now.  The ``include`` and ``exclude``
lists of ``[tool.hatch.build.targets.sdist]`` select the public files.  The
tests at the end ask the sdist builder of hatchling, not a copy of its
rules, which paths it selects.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
from hatchling.builders.config import BuilderConfig
from hatchling.builders.sdist import SdistBuilder

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


@pytest.fixture(scope="module")
def sdist_config() -> BuilderConfig:
    """Give the file selection of the hatchling sdist builder for this checkout.

    The builder reads ``pyproject.toml`` and ``.gitignore`` of the checkout,
    thus the tests examine the configuration that a release uses.  A path
    string is sufficient: the selection does not read the file.
    """
    return SdistBuilder(str(PROJECT_ROOT)).config


# Paths of the work material.  The names are examples of each kind: the
# selection works on the path, and the file does not have to exist.
_WORK_MATERIAL = [
    "work/notes.py",
    "plans/notes.md",
    "experiments/probe.py",
    "CLAUDE.md",
    "AGENTS.md",
    "LEAN-CTX.md",
    ".claude/skills/example/SKILL.md",
    "_debug_probe.py",
    "docs/examples/raw/notes.md",
    "tests/test_feature_comparison.py",
    "scripts/local-tool.sh",
]


@pytest.mark.parametrize("relative_path", _WORK_MATERIAL)
def test_sdist_leaves_out_the_work_material(sdist_config: BuilderConfig, relative_path: str):
    """The sdist must not hold a file of the work material.

    ``.gitignore`` does not exclude these paths now, thus only the include
    list keeps a top-level path out, and only the exclude list keeps out a
    path in a public folder.
    """
    assert not sdist_config.include_path(relative_path), f"the sdist selects {relative_path}"


# Files that the tests of tests/builds/ make: untracked, and also out of
# the sdist.  The config.toml is the regression: "!**/.fw-context/config.toml"
# in .gitignore matches the file itself, and in pathspec.GitIgnoreSpec it
# had priority over the folder exclude "tests/builds/".
_TEST_BUILD_OUTPUTS = [
    "tests/builds/arduino/.fw-context/config.toml",
    "tests/builds/.fw-context/config.toml",
    "tests/builds/zephyr/build/zephyr/zephyr.elf",
]


@pytest.mark.parametrize("relative_path", _TEST_BUILD_OUTPUTS)
def test_sdist_leaves_out_the_outputs_of_test_builds(sdist_config: BuilderConfig, relative_path: str):
    """A folder exclude must also exclude a file that a negation of .gitignore matches."""
    assert not sdist_config.include_path(relative_path), f"the sdist selects {relative_path}"


_PUBLIC_FILES = [
    "src/fw_context_mcp/__init__.py",
    ".fw-context/config.toml",
    "README.md",
    "pyproject.toml",
    "docs/README.md",
    "scripts/build_clang_headers.py",
    "tests/test_packaging.py",
]


@pytest.mark.parametrize("relative_path", _PUBLIC_FILES)
def test_sdist_holds_the_public_files(sdist_config: BuilderConfig, relative_path: str):
    """The include list must not leave out a public file that the sdist held before."""
    assert sdist_config.include_path(relative_path), f"the sdist leaves out {relative_path}"
