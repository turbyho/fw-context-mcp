"""Index runner: parse compile_commands.json, extract symbols, store to SQLite."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config.settings import derive_project_id
from .compile_commands import _SOURCE_EXTS
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute as compute_config_hash
from .db import (
    delete_symbols_for_file,
    get_file_mtimes,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from .symbols import extract as extract_symbols

log = logging.getLogger(__name__)

# Directories to look for when auto-detecting source roots
_COMMON_SOURCE_DIRS = ["src", "lib", "app", "include", "drivers", "modules"]
_COMMON_OS_DIRS = ["zephyr", "mbed-os"]


def _detect_source_roots(project_root: Path, compile_commands: Path) -> list[Path]:
    """Auto-detect source directories from project structure and compile_commands.json.

    Scans project root for common source/OS directories, then supplements with
    top-level directories discovered from compile_commands.json entries.
    Falls back to the project root itself if nothing is found.
    """
    roots: list[Path] = []
    seen: set[Path] = set()

    # 1. Scan for common source directories
    for name in _COMMON_SOURCE_DIRS:
        p = project_root / name
        if p.is_dir() and p not in seen:
            roots.append(p)
            seen.add(p)

    # 2. Scan for common OS/framework directories
    for name in _COMMON_OS_DIRS:
        p = project_root / name
        if p.is_dir() and p not in seen:
            roots.append(p)
            seen.add(p)

    # 3. Discover additional top-level dirs from compile_commands.json
    try:
        units = list(parse_compile_commands(compile_commands))
        for unit in units:
            try:
                rel = unit.file.resolve().relative_to(project_root)
                top = project_root / rel.parts[0]
                if top.is_dir() and top not in seen:
                    roots.append(top)
                    seen.add(top)
            except ValueError:
                pass  # outside project root, skip
    except Exception:
        pass

    # 4. Fallback: index everything under project root
    if not roots:
        roots = [project_root]
        log.info("No source directories detected, falling back to project root")

    log.info("Auto-detected source roots: %s", [str(r) for r in roots])
    return roots


def run(
    compile_commands: Path,
    db_path: Path,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    project_name: str | None = None,
) -> str:
    """Index a project. Returns config_hash of the indexed build."""
    project_root = compile_commands.parent.resolve()
    if not source_roots:
        source_roots = _detect_source_roots(project_root, compile_commands)
    # Only keep roots that actually exist
    source_roots = [r.resolve() for r in source_roots if r.exists()]
    if exclude_paths is None:
        exclude_paths = []
    exclude_paths = [p.resolve() for p in exclude_paths]

    project_id = derive_project_id(project_root)
    name = project_name or project_root.name
    config_hash = compute_config_hash(compile_commands)

    log.info("project=%s config_hash=%s", name, config_hash[:12])

    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, project_id, name, str(project_root))
        upsert_build_config(conn, config_hash, project_id, str(compile_commands))

    units = list(parse_compile_commands(compile_commands))
    units = [u for u in units if u.file.suffix.lower() in _SOURCE_EXTS]
    log.info("TUs to index: %d", len(units))

    # Load existing file records for incremental update
    existing_files = get_file_mtimes(conn, config_hash)

    total_syms = 0
    skipped = 0
    unchanged = 0
    updated = 0
    t0 = time.monotonic()

    for i, unit in enumerate(units):
        file_path = str(unit.file)

        # Skip TUs that live under an excluded path entirely
        resolved_tu = unit.file.resolve()
        if any(resolved_tu == ep or resolved_tu.is_relative_to(ep) for ep in exclude_paths):
            unchanged += 1
            continue

        try:
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
        except OSError:
            current_mtime = 0.0

        # Skip if file hasn't changed since last index
        if file_path in existing_files:
            _, stored_mtime = existing_files[file_path]
            if abs(current_mtime - stored_mtime) < 0.001:
                unchanged += 1
                continue

        try:
            syms = list(extract_symbols(unit, source_roots=source_roots, exclude_paths=exclude_paths))
        except Exception as exc:
            log.warning("skip TU %s: %s", unit.file.name, exc)
            skipped += 1
            continue

        with transaction(conn):
            if file_path in existing_files:
                file_id, _ = existing_files[file_path]
                delete_symbols_for_file(conn, file_id)
            # Register the TU file (for mtime tracking)
            upsert_file(conn, config_hash, file_path, unit.language, mtime=current_mtime)

            if syms:
                # Each symbol may come from a different file (e.g. included header).
                # Build a per-file cache so file_id reflects the symbol's actual location.
                file_id_cache: dict[str, int] = {}
                rows = []
                for s in syms:
                    sym_file = s.file
                    if sym_file not in file_id_cache:
                        lang = "cpp" if Path(sym_file).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
                        try:
                            sym_mtime = Path(sym_file).stat().st_mtime
                        except OSError:
                            sym_mtime = 0.0
                        file_id_cache[sym_file] = upsert_file(conn, config_hash, sym_file, lang, mtime=sym_mtime)
                    rows.append((
                        config_hash,
                        file_id_cache[sym_file],
                        s.usr,
                        s.name,
                        s.qualified_name,
                        s.kind,
                        s.line,
                        s.column,
                        int(s.is_definition),
                        s.signature,
                        s.docstring,
                    ))
                total_syms += insert_symbols_batch(conn, rows)

        updated += 1
        if updated % 50 == 0:
            elapsed = time.monotonic() - t0
            log.info(
                "  %d/%d TUs processed, %d symbols, %.1fs elapsed",
                i + 1, len(units), total_syms, elapsed,
            )

    elapsed = time.monotonic() - t0
    log.info(
        "Done: %d updated, %d unchanged, %d skipped — %d symbols in %.1fs (config_hash=%s)",
        updated, unchanged, skipped, total_syms, elapsed, config_hash[:12],
    )
    return config_hash
