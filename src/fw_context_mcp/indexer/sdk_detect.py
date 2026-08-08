"""SDK/vendor path detection — low-level helpers used by both indexer and MCP layers.

WHY shared between indexer and MCP: the ``is_project`` column is computed
during indexing but MUST produce the same result when the MCP layer later
queries it for filtering.  Using the same detection logic in both layers
ensures consistency — a symbol classified as vendor during indexing will
also be treated as vendor by the MCP query filters.
"""

from __future__ import annotations

from pathlib import Path


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


def _build_sdk_excludes(root: Path) -> list[str]:
    """Build default SDK exclude patterns from build system type.

    WHY auto-detect from build system: each build system has known vendor
    directories — ``mbed-os/`` for Mbed OS, ``.pio/`` for PlatformIO,
    ``zephyr/`` for Zephyr.  Auto-detection removes the need for users to
    manually configure vendor paths in 95% of projects.

    Returns LIKE patterns (with ``%`` wildcard) for vendor/SDK directories.
    Only true vendor/SDK source code — NOT build output (build/ is not SDK).
    """
    from .build import detect_build_system

    build_system = detect_build_system(root)
    if build_system is None:
        return []
    excludes: list[str] = []
    if build_system == "mbed-os":
        excludes.append("mbed-os/%")
        excludes.append("BUILD/%")
    elif build_system == "platformio":
        excludes.extend([".pio/%", "%.platformio/%"])
    elif build_system == "zephyr":
        excludes.extend(["zephyr/%", "modules/%"])
    return excludes
