"""Pre-index validation and auto-fix for build artifacts.

``validate_and_fix()`` is called between build and ``runner.run()`` in
``cmd_index()``.  It parses ``compile_commands.json``, runs per-builder
validation, auto-fixes detected problems, and returns any remaining
unfixable issues.

``is_compile_commands_stale()`` detects structural staleness —
``compile_commands.json`` older than build markers, or source files
missing from the compilation database.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .build import BuildConfig
from .builders.protocol import BuildIssue, BuildSystem

log = logging.getLogger(__name__)

# Directories scanned for source files during staleness / completeness checks.
_DEFAULT_SOURCE_DIRS = ["src", "lib", "app"]


def is_compile_commands_stale(
    compile_commands: Path,
    project_root: Path,
    source_dirs: list[Path] | None = None,
    build_markers: list[Path] | None = None,
) -> tuple[bool, list[str]]:
    """Return ``(stale, reasons)`` — whether *compile_commands* needs a rebuild.

    Checks:
    1. **mtime**: build markers newer than compile_commands.json.
    2. **coverage**: ``.c``/``.cpp`` files in *source_dirs* that don't
       appear as entries in compile_commands.json.

    *source_dirs* defaults to ``["src", "lib", "app"]`` when None.
    *build_markers* are files whose mtime triggers staleness
    (``platformio.ini``, ``CMakeLists.txt``, etc.). Defaults to common
    build markers when None.

    Returns ``(False, [])`` when compile_commands.json is up to date.
    """
    if not compile_commands.exists():
        return True, ["compile_commands.json missing"]

    reasons: list[str] = []
    root = project_root.resolve()

    try:
        cc_mtime = compile_commands.stat().st_mtime
    except OSError as exc:
        return True, [f"cannot stat compile_commands.json: {exc}"]

    # 1. Build marker mtime check
    if build_markers is None:
        build_markers = [
            root / m for m in ["platformio.ini", "CMakeLists.txt", ".mbed", "west.yml"]
        ]
    for marker in build_markers:
        try:
            if marker.exists() and marker.stat().st_mtime > cc_mtime:
                reasons.append(f"{marker.name} newer than compile_commands.json")
        except OSError:
            pass

    # 2. Coverage check — are all source files in compile_commands?
    dirs = source_dirs or [root / d for d in _DEFAULT_SOURCE_DIRS]
    source_files: set[str] = set()
    for sd in dirs:
        if sd.is_dir():
            for ext in (".c", ".cpp", ".cc", ".cxx"):
                for sf in sd.rglob(f"*{ext}"):
                    source_files.add(str(sf.resolve()))

    if source_files:
        try:
            cc_data = json.loads(compile_commands.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return True, [f"cannot parse compile_commands.json: {exc}"]

        cc_files: set[str] = set()
        for entry in cc_data:
            f = entry.get("file", "")
            if f:
                p = Path(f)
                if not p.is_absolute():
                    p = (Path(entry.get("directory", "")) / f).resolve()
                cc_files.add(str(p.resolve()))

        missing = source_files - cc_files
        if missing:
            example = sorted(missing)[:3]
            suffix = f" ({len(missing) - 3} more...)" if len(missing) > 3 else ""
            reasons.append(
                f"{len(missing)} source file(s) not in compile_commands.json: "
                f"{', '.join(Path(m).name for m in example)}{suffix}"
            )

    return (len(reasons) > 0, reasons)


def validate_and_fix(
    compile_commands: Path,
    project_root: Path,
    build_system: BuildSystem,
    build_cfg: BuildConfig | None = None,
    *,
    fix: bool = False,
) -> list[BuildIssue]:
    """Validate build artifacts and auto-fix detected problems.

    1. Parse ``compile_commands.json``.
    2. Run ``build_system.validate_artifacts()`` for per-builder checks.
    3. For each auto-fixable issue, call ``build_system.auto_fix()``.
    4. Return remaining unfixed issues (empty list = all good).

    When *fix* is True and issues remain after auto-fix, a rebuild+re-validate
    cycle is attempted once.

    Returns the list of issues that could NOT be fixed.
    """
    root = project_root.resolve()
    issues: list[BuildIssue] = []

    # ── Parse compile_commands.json ──
    if not compile_commands.exists():
        issues.append(BuildIssue(
            severity="error",
            category="missing_compile_commands",
            message="compile_commands.json not found",
            auto_fixable=False,
            fix_hint="Run 'fw-context index --build' to regenerate.",
        ))
        return issues

    try:
        cc_data = json.loads(compile_commands.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        issues.append(BuildIssue(
            severity="error",
            category="invalid_compile_commands",
            message=f"Cannot parse compile_commands.json: {exc}",
            auto_fixable=False,
        ))
        return issues

    if not cc_data:
        issues.append(BuildIssue(
            severity="error",
            category="empty_compile_commands",
            message="compile_commands.json is empty — build may have produced nothing",
            auto_fixable=False,
            fix_hint="Run 'fw-context index --build' to regenerate.",
        ))
        return issues

    # ── Per-TU check: -include / -imacros files exist ──
    from .compile_commands import normalize_args, validate_include_files

    missing_includes: set[str] = set()
    for entry in cc_data:
        try:
            cwd = Path(entry.get("directory", str(root)))
            args = normalize_args(entry, cwd)
            validate_include_files(args)
        except RuntimeError as exc:
            missing_includes.add(str(exc))

    for msg in missing_includes:
        issues.append(BuildIssue(
            severity="error",
            category="missing_include_files",
            message=msg,
            auto_fixable=False,
            fix_hint="Run 'fw-context index --build' to regenerate build output.",
        ))

    # ── Per-builder validation ──
    try:
        builder_issues = build_system.validate_artifacts(compile_commands, root)
        issues.extend(builder_issues)
    except Exception as exc:
        log.warning("Builder validation raised: %s", exc)

    if not issues:
        return []

    # ── Auto-fix loop (max 1 cycle) ──
    if not fix:
        return issues

    fixed_count = 0
    unfixed: list[BuildIssue] = []
    for issue in issues:
        if issue.auto_fixable:
            try:
                ok = build_system.auto_fix(issue, root)
                if ok:
                    fixed_count += 1
                    continue
            except Exception as exc:
                log.warning("Auto-fix for %s raised: %s", issue.category, exc)
        unfixed.append(issue)

    if fixed_count > 0:
        log.info("Auto-fixed %d issue(s), %d remain", fixed_count, len(unfixed))

    return unfixed
