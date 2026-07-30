"""SDK path filtering — LIKE-based exclude patterns for vendor/OS code."""

from __future__ import annotations

from pathlib import Path

# NOTE: These are re-exported from indexer layer for MCP-layer convenience.
from ...indexer.sdk_detect import (  # noqa: F401 — re-exported
    _build_sdk_excludes,
    _normalize_path_pattern,
    _path_matches,
)


def detect_sdk_exclude_like(project_root: Path, extra_vendor_paths: list[str] | None = None) -> list[str]:
    """Return LIKE patterns for SDK/vendor directories.

    SDK directories are auto-detected from the build system type via
    :func:`_build_sdk_excludes`. User-configured paths come from
    *extra_vendor_paths* (config ``vendor_paths``).

    Returns patterns with a ``%`` prefix so they match both relative
    (``mbed-os/...``) and absolute (``/home/.../mbed-os/...``) paths
    in the files table.
    """
    # Auto-detect from build system (zero hardcoded marker names outside _build_sdk_excludes).
    # Some patterns already include a % prefix for nested-path matching (e.g. %.platformio/%),
    # so we strip any leading % before adding our own.
    patterns: list[str] = [f"%{p.lstrip('%')}" for p in _build_sdk_excludes(project_root)]

    if extra_vendor_paths:
        for p in extra_vendor_paths:
            p = p.strip("/")
            if p:  # dedup not critical for small vendor path lists
                patterns.append(f"%{p}/%")

    return patterns


def compute_exclude_like(
    project_root: Path,
    *,
    analyze_vendor: bool = False,
    vendor_paths: list[str] | None = None,
) -> list[str]:
    """Compute SQL LIKE patterns for SDK/vendor path exclusion.

    Centralized entry point for SDK/vendor path exclusion.  When
    *analyze_vendor* is True, returns an empty list (no exclusion).
    Otherwise auto-detects SDK directories from the build system via
    :func:`_build_sdk_excludes` and merges user-configured
    *vendor_paths*.

    Args:
        project_root: Root directory of the project.
        analyze_vendor: When True, skip all vendor/SDK exclusion.
        vendor_paths: Additional vendor/SDK directory patterns (relative
            strings).  Additive to auto-detection.

    Returns:
        List of SQL LIKE patterns (e.g. ``%mbed-os/%``), or empty
        list when *analyze_vendor* is True.
    """
    if analyze_vendor:
        return []

    extra: list[str] | None = None
    if vendor_paths is not None:
        extra = list(vendor_paths)

    return detect_sdk_exclude_like(project_root, extra)
