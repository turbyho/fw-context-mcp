"""SDK path filtering — LIKE-based exclude patterns for vendor/OS code."""

from __future__ import annotations

from pathlib import Path

from ...config import load as load_config
from .context import _detect_build_system


def _path_matches(file_path: str, pattern: str) -> bool:
    """Check if *file_path* matches a SQL LIKE pattern (supports % wildcard)."""
    import fnmatch

    # Convert SQL LIKE pattern to fnmatch pattern: % → *
    fn_pattern = pattern.replace("%", "*")
    # Match against the path itself, and also against any containing directory
    return fnmatch.fnmatch(file_path, fn_pattern) or fnmatch.fnmatch(
        "/" + file_path, "*/" + fn_pattern
    )


def _build_sdk_excludes(root: Path) -> list[str]:
    """Build default SDK exclude patterns from build system type."""
    build_system = _detect_build_system(root)
    excludes: list[str] = []
    if build_system == "mbed-os":
        excludes.append("mbed-os/%")
    elif build_system == "platformio":
        excludes.extend([".pio/%", "%.platformio/%"])
    elif build_system == "zephyr":
        excludes.extend(["zephyr/%", "build/%", "modules/%"])
    return excludes


def _merge_excludes(exclude_paths: list[str] | None, project_only: bool, root: Path) -> list[str] | None:
    """Merge auto-SDK + config + user exclude patterns. Returns None if empty."""
    if not project_only:
        return exclude_paths  # pass through as-is

    effective: list[str] = list(_build_sdk_excludes(root))
    try:
        cfg = load_config(project_root=root)
        effective.extend(cfg.index.exclude_paths)
    except Exception:
        pass
    if exclude_paths:
        effective.extend(exclude_paths)

    # Deduplicate while preserving order
    seen: set[str] = set()
    final: list[str] = []
    for p in effective:
        if p not in seen:
            seen.add(p)
            final.append(p)
    return final if final else None


def detect_sdk_exclude_like(project_root: Path, extra_exclude: list[str] | None = None) -> list[str]:
    """Return LIKE patterns for SDK/vendor directories.

    SDK directories are auto-detected from the build system type via
    :func:`_build_sdk_excludes`. User-configured paths come from
    *extra_exclude* (config ``exclude_paths``).

    Returns patterns with a ``%`` prefix so they match both relative
    (``mbed-os/...``) and absolute (``/home/.../mbed-os/...``) paths
    in the files table.
    """
    # Auto-detect from build system (zero hardcoded marker names outside _build_sdk_excludes).
    # Some patterns already include a % prefix for nested-path matching (e.g. %.platformio/%),
    # so we strip any leading % before adding our own.
    patterns: list[str] = [f"%{p.lstrip('%')}" for p in _build_sdk_excludes(project_root)]

    if extra_exclude:
        for p in extra_exclude:
            p = p.strip("/")
            if p and f"%{p}/%" not in patterns:
                patterns.append(f"%{p}/%")

    return patterns


def compute_exclude_like(
    project_root: Path,
    *,
    analyze_vendor: bool = False,
    exclude_paths: list[str] | list[Path] | None = None,
) -> list[str]:
    """Compute SQL LIKE patterns for SDK/vendor path exclusion.

    Centralized entry point for SDK/vendor path exclusion.  When
    *analyze_vendor* is True, returns an empty list (no exclusion).
    Otherwise auto-detects SDK directories from the build system via
    :func:`_build_sdk_excludes` and merges user-configured
    *exclude_paths*.

    Args:
        project_root: Root directory of the project.
        analyze_vendor: When True, skip all vendor/SDK exclusion.
        exclude_paths: Additional paths to exclude.  Accepts relative
            strings (from config ``IndexConfig.exclude_paths``) or
            absolute ``Path`` objects (from
            ``Config.exclude_root_paths()``).  Paths outside
            *project_root* are silently skipped.

    Returns:
        List of SQL LIKE patterns (e.g. ``%mbed-os/%``), or empty
        list when *analyze_vendor* is True.
    """
    if analyze_vendor:
        return []

    extra: list[str] | None = None
    if exclude_paths is not None:
        extra = []
        for p in exclude_paths:
            if isinstance(p, Path):
                try:
                    extra.append(str(p.relative_to(project_root)))
                except ValueError:
                    continue
            else:
                extra.append(p)

    return detect_sdk_exclude_like(project_root, extra)
