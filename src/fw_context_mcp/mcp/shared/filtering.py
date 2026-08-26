"""SDK path filtering — LIKE-based exclude patterns for vendor/OS code.

Why LIKE patterns instead of file-path comparison:

- The files table stores paths that may be relative (``mbed-os/rtos/Thread.cpp``)
  or absolute (``/home/user/project/mbed-os/rtos/Thread.cpp``).  LIKE with a
  ``%`` prefix matches both forms in one query — ``%mbed-os/%`` finds
  every file under a directory named ``mbed-os`` regardless of the path
  representation.
- A Python-side ``Path.match()`` filter would require pulling the entire
  file list into memory (project databases with 30K+ files) and applying
  path joins per row — the database is faster at pattern matching than
  Python, and the WHERE clause pushes the filter into SQLite's index scan.
- Patterns compose additively: auto-detected SDK directories from the build
  system + user-configured ``vendor_paths`` from config.  LIKE patterns
  merge without conflict because SQL's OR semantics naturally compose.

Pattern format: ``%dir_name/%`` — the trailing ``/%`` ensures only files
inside the directory are excluded, not files whose path merely contains
the directory name as a substring (``%lib/%`` excludes ``lib/mylib.c``
but not ``calibration/sensor.c``).
"""

from __future__ import annotations

from pathlib import Path

# NOTE: These are re-exported from indexer layer for MCP-layer convenience.
from ...indexer.sdk_detect import (  # noqa: F401 — re-exported
    _build_sdk_excludes,
    _normalize_path_pattern,
    _path_matches,
    vendor_patterns_for_build,
)


def detect_sdk_exclude_like(
    project_root: Path,
    extra_vendor_paths: list[str] | None = None,
    *,
    manifest: dict | None = None,
) -> list[str]:
    """Return SQL LIKE patterns that match SDK and vendor directory paths.

    Two sources of patterns, merged:

    1. **Auto-detection** — :func:`_build_sdk_excludes` inspects the
       build system type (PlatformIO, Mbed CLI, CMake) and returns
       patterns like ``mbed-os/``, ``.pio/libdeps/``, ``build/``.
       These patterns come from the SDK's own build-system detectives,
       not from hardcoded guesses.
    2. **User configuration** — ``vendor_paths`` from
       ``[index]`` in ``config.toml``.  Users add directories like
       ``external/libfoo/`` that the auto-detector cannot know about.

    All returned patterns are prefixed with ``%`` so they match both
    relative and absolute paths in SQL ``LIKE`` queries.

    Why the leading ``%`` strip-then-add approach:

    - ``_build_sdk_excludes`` may already include a ``%`` prefix for
      patterns that match nested paths (``%.platformio/%``).  Stripping
      any existing leading ``%`` before adding our own avoids double
      prefixes (``%%.platformio/%``) that would break the pattern.
    - User paths never include ``%``, so the strip is a no-op for them.

    Pass *manifest* whenever the caller has it.  The set stored there is the
    one the index run applied, and this layer cannot derive the same one: the
    strongest source for Zephyr is ``-fmacro-prefix-map`` in the compiler
    flags, and only the indexer has those.
    """
    # Strip existing leading % to prevent double-prefix (some auto-detected
    # patterns already include % for nested-path matching).
    detected = vendor_patterns_for_build(manifest, project_root)
    patterns: list[str] = [f"%{p.lstrip('%')}" for p in detected]

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
    manifest: dict | None = None,
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

    return detect_sdk_exclude_like(project_root, extra, manifest=manifest)
