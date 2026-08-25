"""SDK/vendor path detection — low-level helpers used by both indexer and MCP layers.

WHY shared between indexer and MCP: the ``is_project`` column is computed
during indexing but MUST produce the same result when the MCP layer later
queries it for filtering.  Using the same detection logic in both layers
ensures consistency — a symbol classified as vendor during indexing will
also be treated as vendor by the MCP query filters.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _path_matches(file_path: str, pattern: str) -> bool:
    """Check if *file_path* matches a SQL LIKE pattern (supports % wildcard).

    WHY LIKE patterns not glob: vendor paths can appear at any depth.
    ``mbed-os/%`` must match ``mbed-os/targets/TARGET_NORDIC/foo.cpp``,
    which glob ``*`` cannot do (it doesn't cross directory boundaries).
    SQL LIKE ``%`` matches any sequence including ``/``.

    ``%`` matches any sequence of characters **including ``/``** (unlike
    fnmatch ``*``).  Uses regex for correct matching of nested directories.

    For absolute paths (``project_paths`` outside project root) the first
    branch is used — the second branch (prefix match ``*/pattern`` against
    ``/file_path``) preserves compatibility with ``detect_sdk_exclude_like``
    prefixed patterns (``%mbed-os/%``).
    """
    import re

    # Escape regex special chars.  Use a placeholder for % before escaping
    # since re.escape() does NOT escape % (it's not a regex metachar).
    # Without a placeholder, a literal % in a file name would be treated as
    # a wildcard — acceptable risk for embedded firmware projects where %
    # in file names is essentially nonexistent.
    placeholder = "\x00"
    escaped = re.escape(pattern.replace("%", placeholder))
    regex = escaped.replace(placeholder, ".*")
    regex = "^" + regex + "$"
    if re.match(regex, file_path):
        return True
    # Also match "*/pattern" against "/file_path" — preserves original
    # prefix behavior for absolute paths and detect_sdk_exclude_like
    # prefixed patterns (e.g. %mbed-os/%)
    return bool(re.match("^.*/" + regex.lstrip("^"), "/" + file_path))


def _normalize_path_pattern(pattern: str) -> str:
    """Append '/%' to patterns without a wildcard so _path_matches (regex) matches subdirectories.

    ``"third_party"`` → ``"third_party/%"`` (matches all files in the directory)
    ``"mbed-os/%"`` → ``"mbed-os/%"`` (already has wildcard, unchanged)
    ``"/home/user/esp/components/muj_fork"`` → ``"/home/user/esp/components/muj_fork/%"``
    """
    if "%" not in pattern:
        pattern = pattern.rstrip("/") + "/%"
    return pattern


def _normalize_patterns(patterns: list[str]) -> list[str]:
    # NOTE: vendor_patterns from _build_sdk_excludes() already contain % wildcards
    # and do NOT go through this function. Only user-supplied paths need normalization.
    """Normalize patterns for _path_matches — each wildcard-less pattern gets '/%' appended.

    Centralized wrapper calling _normalize_path_pattern for each pattern.
    Used by both runner.py and maintenance.py.

    ``"third_party"`` → ``"third_party/%"``
    ``"/home/user/esp/components/muj_fork"`` → ``"/home/user/esp/components/muj_fork/%"``
    ``"mbed-os/%"`` → ``"mbed-os/%"`` (already has wildcard, unchanged)
    """
    return [_normalize_path_pattern(p) for p in patterns]


def _build_sdk_excludes(
    root: Path,
    build_system: str | None = None,
    *,
    units: list | None = None,
) -> list[str]:
    """Return the in-tree vendor and SDK patterns for this project.

    A thin wrapper over the builder registry.  Each builder answers for its
    own build system through ``get_vendor_patterns()``, so the knowledge of
    where a build system puts third-party code lives with that build system
    and not in a chain of ``if`` branches here.  A build system that has no
    canonical in-tree vendor directory answers with an empty list, and each
    one says why in its own docstring.

    *build_system* is the ``[build] system`` key from the config.  It wins
    over marker detection, because the markers and the config can disagree:
    a freestanding NCS application has CMakeLists.txt and no west.yml, so a
    marker scan calls it a CMake project.  Pass None on a path that has no
    config, and the markers decide.

    *units* are this build's translation units, or None when the caller has
    not parsed them.  A builder that reads the compiler flags needs them.

    Returns LIKE patterns (with the ``%`` wildcard) for the vendor and SDK
    trees INSIDE the project.  Build output is NOT in the result: it has
    ``get_build_dir_patterns()``, and generated code counts as project code.
    """
    from .build import detect_build_system
    from .builders import registry

    key = build_system or detect_build_system(root)
    if key is None:
        return []
    builder_cls = registry.get(key)
    if builder_cls is None:
        log.warning(
            "Unknown build system %r — no vendor patterns.  Known: %s",
            key, ", ".join(sorted(registry.keys())),
        )
        return []
    try:
        return builder_cls().get_vendor_patterns(root, units=units)
    except (OSError, ValueError, TypeError, RuntimeError):
        # A builder that cannot answer must not stop the index run or the
        # query.  An empty list means "no in-tree vendor tree", which is the
        # answer for most projects anyway, and a wrong pattern would hide
        # the team's own code.
        log.warning("Builder %s failed to give vendor patterns", key, exc_info=True)
        return []


def vendor_patterns_for_build(
    manifest: dict | None,
    project_root: Path,
    *,
    build_system: str | None = None,
) -> list[str]:
    """Return the vendor set THIS build was indexed with.

    Reads ``manifest["vendor_patterns"]``, which the index run wrote after it
    applied that set.  Deriving one instead gives a different answer: the
    strongest source for Zephyr is ``-fmacro-prefix-map`` in the compiler
    flags, and only the indexer has those.  A query layer with a wider set
    then trusts headers the indexer re-hashed, and a query layer with a
    narrower set re-hashes headers the indexer trusted — the second case
    reports the index as permanently stale, and a reindex does not clear it.

    Falls back to detection when the key is absent, which is every index
    written before the key existed.  An empty list in the manifest is an
    ANSWER, not a missing value: most projects keep their SDK outside the
    project, and the correct set for them is empty.
    """
    if manifest is not None and "vendor_patterns" in manifest:
        return list(manifest["vendor_patterns"])
    return list(_build_sdk_excludes(project_root, build_system))
