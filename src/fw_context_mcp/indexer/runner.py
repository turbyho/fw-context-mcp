"""Index runner: parse compile_commands.json, extract symbols, store to SQLite."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

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


def _project_id(root: Path) -> str:
    """Derive a stable project_id from git remote URL or directory path."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return hashlib.sha256(out.encode()).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def run(
    compile_commands: Path,
    db_path: Path,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    project_name: str | None = None,
) -> str:
    """Index a project. Returns config_hash of the indexed build."""
    project_root = compile_commands.parent.resolve()
    if source_roots is None:
        source_roots = [project_root / "src", project_root / "lib"]
    # Only keep roots that actually exist
    source_roots = [r.resolve() for r in source_roots if r.exists()]
    if exclude_paths is None:
        exclude_paths = []
    exclude_paths = [p.resolve() for p in exclude_paths]

    project_id = _project_id(project_root)
    name = project_name or project_root.name
    config_hash = compute_config_hash(compile_commands)

    log.info("project=%s config_hash=%s", name, config_hash[:12])

    conn = open_db(db_path)
    with transaction(conn):
        upsert_project(conn, project_id, name, str(project_root))
        upsert_build_config(conn, config_hash, project_id, str(compile_commands))

    from .compile_commands import _SOURCE_EXTS  # reuse C/C++ extension set

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
            file_id = upsert_file(conn, config_hash, file_path, unit.language, mtime=current_mtime)
            if syms:
                rows = [
                    (
                        config_hash,
                        file_id,
                        s.usr,
                        s.name,
                        s.qualified_name,
                        s.kind,
                        s.line,
                        s.column,
                        int(s.is_definition),
                        s.signature,
                        s.docstring,
                    )
                    for s in syms
                ]
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
