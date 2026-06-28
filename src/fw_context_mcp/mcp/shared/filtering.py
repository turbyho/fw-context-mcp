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
