"""Maintenance MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...config import derive_project_id
from ...config import load as load_config
from ...config.settings import Config
from ...indexer.compile_commands import parse as parse_cc
from ...indexer.db import (
    CURRENT_SCHEMA_VERSION,
    DatabaseCorruptionError,
    count_refs,
    get_active_config,
    get_all_projects,
    get_db_schema_version,
    open_db,
    transaction,
)
from ...llm._diag import check_setup
from ...utils import resolve_project_root
from ..background import _is_bg_reindex_running
from ..shared.context import (
    _db_path,
    _detect_build_system,
    _invalidate_conn_cache,
    _is_stale,
    _open_db_or_return,
    _resolve_context,
)
from ..shared.stale import _check_header_staleness, _count_modified_files

log = logging.getLogger(__name__)


# ── moved from server.py ──
def get_active_build(
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")
    ] = None,
    fast: Annotated[
        bool, Field(description="When True (default), skip per-file stat scan — faster but "
        "modified_files_count and header_affected_tus may be 0.")
    ] = True,
) -> dict:
    """MANDATORY FIRST CALL for C/C++ projects. Return metadata about the
    most recently indexed build configuration — check index health before
    using any other fw-context tools.

    Read-only: yes. Call at session start to check if the index exists,
    how many symbols it contains, and whether a reindex is needed.

    Use the ``status`` field for decision-making:

    * ``"ready"`` — fully up to date, no issues. Continue normally.
    * ``"reindexing"`` — background reindex in progress. Index is still
      usable — all queries return accurate results. Continue normally.
    * ``"reindex_needed"`` — compile_commands.json changed or schema
      mismatch. Run ``fw-context index``, but queries still work on
      existing data.
    * ``"no_index"`` — no build config indexed. Use other tools.
    * ``"error"`` — DB corruption or access error. Use other tools.

    ``reindex_needed`` is True when a structural mismatch exists: schema
    version outdated or compile_commands.json changed since indexing.
    Modified source files are auto-handled per-query and do NOT cause
    ``reindex_needed=True``.

    ``index_message`` is a human-readable summary of the index state.

    Background reindex is managed by the startup daemon thread and the
    file watcher — ``get_active_build()`` is a read-only tool that does
    not spawn subprocesses.  ``reindex_progress`` contains the last log
    line from the reindex subprocess when ``bg_reindex_running`` is True.

    Set ``fast=False`` to include per-file stat scanning for accurate
    ``modified_files_count`` and ``header_affected_tus``.  The default
    ``fast=True`` is faster and sufficient for most session-start checks.

    Args:
        project_root: Project root directory. Auto-detected from CWD if
            omitted.

    Returns:
        dict: {config_hash, project_id, project_root, build_system,
        compile_commands, indexed_at (ISO timestamp), symbol_count, file_count,
        reference_count, modified_files_count (int), header_affected_tus (int —
        number of TUs with stale header dependencies), manifest_verification (str —
        "full" when manifest.json exists, "none" otherwise), analyzed_symbols (int),
        unanalyzed_symbols (int — definition symbols still needing LLM analysis),
        analysis_model (str or None), vendor_paths (list[str] —
        config index.vendor_paths), project_paths (list[str] —
        config index.project_paths), bg_reindex_running (bool),
        reindex_progress (str or None — last log line when reindex is running),
        schema_version (int — DB schema version),
        current_schema (int — code expects), status (str — "ready"|"reindexing"|
        "reindex_needed"|"no_index"|"error"), reindex_needed (bool —
        structural mismatch requiring a full reindex),
        reindex_reasons (list[str] — why reindex is needed, empty when False),
        index_message (str — human-readable summary of index state)}
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    # Check bg reindex status BEFORE querying the DB — when a bg reindex
    # is running, the _modified_cache may contain stale counts from before
    # the reindex started.  Skipping the cache ensures accurate counts.
    bg_running = _is_bg_reindex_running(root)

    conn, err_result = _open_db_or_return(db_path)
    if err_result is not None:
        return err_result[0]
    assert conn is not None
    with conn:
        project_id = derive_project_id(root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return {"error": f"No build config indexed for project at {root}."}
        config_hash = cfg["config_hash"]
        sym_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE config_hash=?", (config_hash,)).fetchone()[0]
        file_count = conn.execute("SELECT COUNT(*) FROM files WHERE config_hash=?", (config_hash,)).fetchone()[0]
        ref_count = count_refs(conn, config_hash)
        manifest_verification = cfg["manifest_verification"] if "manifest_verification" in cfg else "none"
        if fast:
            modified_count = 0
            header_affected_tus = 0
        else:
            modified_count = _count_modified_files(conn, config_hash, root, use_cache=False)
            # Check header dependencies separately (different metric: TUs, not files)
            if manifest_verification == "full":
                header_affected_tus, _ = _check_header_staleness(
                    conn,
                    config_hash,
                    root,
                    use_cache=False,
                )
            else:
                header_affected_tus = 0
        db_schema_ver = get_db_schema_version(conn)

        # LLM analysis statistics
        analyzed_count = conn.execute(
            """SELECT COUNT(*) FROM llm_analysis a
               JOIN symbols s ON s.id = a.symbol_id
               WHERE s.config_hash = ?""",
            (config_hash,),
        ).fetchone()[0]
        analysis_model_row = conn.execute(
            """SELECT a.model FROM llm_analysis a
               JOIN symbols s ON s.id = a.symbol_id
               WHERE s.config_hash = ? LIMIT 1""",
            (config_hash,),
        ).fetchone()

        # Read stored analyze_vendor — what was actually indexed.
        # Fall back to False for DBs that predate the column.
        try:
            stored_analyze_vendor = bool(
                conn.execute(
                    "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
                    (config_hash,),
                ).fetchone()["analyze_vendor"]
            )
        except sqlite3.OperationalError:
            stored_analyze_vendor = False

        # Load project config for vendor_paths/project_paths in the response
        # and for unanalyzed_symbol count filtering.
        proj_cfg = load_config(project_root=root)

        # Count definition symbols that still need LLM analysis
        unanalyzed_count = _count_unanalyzed_symbols(
            conn, config_hash, stored_analyze_vendor, root,
            proj_cfg.index.vendor_paths,
        )

        cc_changed, stale_reason = _is_stale(cfg, cfg["compile_commands_path"])
        schema_old = db_schema_ver < CURRENT_SCHEMA_VERSION
        needs_reindex = cc_changed or schema_old

        # Build reindex_reasons — only when reindex is actually needed
        reindex_reasons: list[str] = []
        if schema_old:
            reindex_reasons.append(f"schema_mismatch: {db_schema_ver} < {CURRENT_SCHEMA_VERSION}")
        if cc_changed:
            reindex_reasons.append(stale_reason or "compile_commands_changed")

        # Determine status
        if needs_reindex:
            status = "reindex_needed"
        elif bg_running:
            status = "reindexing"
        else:
            status = "ready"

        # Build human-readable index message
        if status == "ready":
            index_message = f"Index is fully up to date ({sym_count} symbols)"
        elif status == "reindexing":
            if modified_count:
                index_message = (
                    f"Index is usable — {modified_count} file(s) being reindexed "
                    f"in background. All queries return accurate results."
                )
            else:
                index_message = (
                    "Index is usable — background reindex in progress. All queries return accurate results."
                )
        elif schema_old and cc_changed:
            index_message = (
                f"Schema version mismatch ({db_schema_ver} < {CURRENT_SCHEMA_VERSION}) "
                f"and compile_commands.json changed. Run fw-context index. "
                f"Queries still work on existing data."
            )
        elif schema_old:
            index_message = (
                f"Schema version mismatch ({db_schema_ver} < {CURRENT_SCHEMA_VERSION}). "
                f"Run fw-context index. Queries still work on existing data."
            )
        else:
            index_message = "Compile commands changed — run fw-context index. Queries still work on existing data."

        # When manifest verification is not full, warn the LLM.
        _warning = None
        if manifest_verification != "full":
            if bg_running:
                _warning = (
                    "Index was built without full header dependency "
                    "tracking (manifest verification: "
                    f"{manifest_verification}). A reindex is currently "
                    "running — the index will be updated once it "
                    "completes. You may continue analysis, but note "
                    "that header changes since the last completed "
                    "index may not be reflected."
                )
                index_message += (
                    f" — manifest verification: {manifest_verification} (reindex in progress — wait for completion)"
                )
            else:
                _warning = (
                    "⚠️ INDEX DEGRADED — STOP ALL C/C++ ANALYSIS "
                    "IMMEDIATELY.\n\n"
                    "manifest.json is NOT available "
                    "for this index (manifest verification: "
                    f"{manifest_verification}). The index may contain "
                    "STALE data — header changes cannot be detected "
                    "without manifest.json. Continuing analysis with a "
                    "stale index WILL produce incorrect results.\n\n"
                    "REQUIRED: Tell the user to run 'fw-context index' "
                    "to rebuild with full dependency tracking. Do NOT "
                    "continue any C/C++ analysis until the user "
                    "confirms the index has been rebuilt."
                )
                index_message += (
                    f" — manifest verification: {manifest_verification} (run 'fw-context index' for full tracking)"
                )

        if header_affected_tus:
            index_message += (
                f" | {header_affected_tus} TU(s) have stale header dependencies — header changes since last index"
            )

        result: dict = {
            "config_hash": config_hash,
            "project_id": project_id,
            "project_root": str(root),
            "build_system": _detect_build_system(root),
            "compile_commands": cfg["compile_commands_path"],
            "indexed_at": cfg["created_at"],
            "symbol_count": sym_count,
            "file_count": file_count,
            "reference_count": ref_count,
            "modified_files_count": modified_count,
            "header_affected_tus": header_affected_tus,
            "schema_version": db_schema_ver,
            "current_schema": CURRENT_SCHEMA_VERSION,
            "analyzed_symbols": analyzed_count,
            "unanalyzed_symbols": unanalyzed_count,
            "analysis_model": analysis_model_row["model"] if analysis_model_row else None,
            "manifest_verification": manifest_verification,
            "description": cfg["description"] if "description" in cfg.keys() else "",
            "first_indexed_at": cfg["first_indexed_at"] if "first_indexed_at" in cfg.keys() else "",
            "vendor_paths": proj_cfg.index.vendor_paths,
            "project_paths": proj_cfg.index.project_paths,
            "status": status,
            "reindex_needed": needs_reindex,
            "reindex_reasons": reindex_reasons,
            "index_message": index_message,
        }
        if _warning is not None:
            result["_warning"] = _warning
    # Background reindex is managed by the startup daemon thread and the
    # file watcher — get_active_build() is a read-only tool and should
    # not spawn subprocesses.
    result["bg_reindex_running"] = bg_running
    if bg_running:
        result["reindex_progress"] = _read_reindex_progress(db_path)
    return result
def _read_reindex_progress(db_path: Path) -> str | None:
    """Read the last line of reindex.log, or None if unavailable."""
    log_file = db_path.parent / "reindex.log"
    try:
        with open(log_file, encoding="utf-8") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            if file_size == 0:
                return None
            fh.seek(max(0, file_size - 4096))
            last_chunk = fh.read()
            lines = last_chunk.splitlines()
            return lines[-1].strip() if lines else None
    except (OSError, IndexError):
        return None


# ── moved from server.py ──
def _list_status(db_schema_ver: int, cc_stale: bool) -> str:
    """Return a status string for list_projects."""
    needs = (db_schema_ver < CURRENT_SCHEMA_VERSION) or cc_stale
    return "reindex_needed" if needs else "ready"

def _count_unanalyzed_symbols(
    conn: sqlite3.Connection,
    config_hash: str,
    stored_analyze_vendor: bool,
    root: Path,
    vendor_paths: list[str],
) -> int:
    """Count definition symbols that still need LLM analysis."""
    if stored_analyze_vendor:
        return conn.execute(
            """SELECT COUNT(*)
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.kind IN ('function', 'method', 'constructor',
                                'destructor', 'class', 'struct')
                 AND s.name NOT LIKE '%(anonymous%'
                 AND s.name NOT LIKE '%(unnamed%'
                 AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)""",
            (config_hash,),
        ).fetchone()[0]

    from ..shared.filtering import compute_exclude_like

    exclude_like = compute_exclude_like(
        root,
        analyze_vendor=False,
        vendor_paths=vendor_paths,
    )
    exclude_clauses = " AND ".join(
        ["s.file_path NOT LIKE ?"] * len(exclude_like)
    )
    exclude_clause = (" AND " + exclude_clauses) if exclude_clauses else ""
    query = f"""SELECT COUNT(*)
           FROM symbols s
           WHERE s.config_hash = ?
             AND s.is_definition = 1
             AND s.kind IN ('function', 'method', 'constructor',
                            'destructor', 'class', 'struct')
             {exclude_clause}
             AND s.name NOT LIKE '%(anonymous%'
             AND s.name NOT LIKE '%(unnamed%'
             AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)"""
    return conn.execute(query, (config_hash, *exclude_like)).fetchone()[0]


def list_projects(
    project_root: Annotated[
        str | None,
        Field(description="Project root. Auto-detected if omitted. Pass to distinguish multiple indexed projects."),
    ] = None,
) -> list[dict]:
    """List all indexed firmware projects with their statistics.

    Read-only. No side effects. Use at session start to discover available
    projects; use ``get_active_build`` for details on the currently active project.

    Args:
        project_root: Project root. Auto-detected if omitted. Pass to
            distinguish multiple indexed projects.

    Returns:
        list of dicts, each with: project_id, name, root_path, build_system,
        symbol_count, file_count, indexed_at, schema_version, current_schema,
        reindex_needed (bool), status (str), db (path to SQLite database file).
    """
    cfg = load_config(project_root=Path(project_root).resolve() if project_root else None)
    index_dir = cfg.index.db_dir
    db_files = list(index_dir.glob("*/index.db")) if index_dir.exists() else []
    if not db_files:
        return [{"info": f"No indexed projects found under {index_dir}."}]
    results: list[dict] = []
    for db_path in sorted(db_files):
        try:
            conn, err_result = _open_db_or_return(db_path)
            if err_result is not None:
                results.append(err_result[0])
                continue
            assert conn is not None
            with conn:
                rows = get_all_projects(conn)
                db_schema_ver = get_db_schema_version(conn)
            for r in rows:
                cc_stale = (
                    _is_stale(
                        {"created_at": r["created_at"]},
                        r["compile_commands_path"],
                    )[0]
                    if r["compile_commands_path"]
                    else False
                )
                root = Path(r["root_path"]) if r["root_path"] else None
                results.append(
                    {
                        "project_id": r["project_id"],
                        "name": r["name"],
                        "root_path": r["root_path"],
                        "build_system": _detect_build_system(root) if root else "unknown",
                        "symbol_count": r["symbol_count"],
                        "file_count": r["file_count"],
                        "indexed_at": r["created_at"],
                        "description": r["description"] if "description" in r.keys() else "",
                        "first_indexed_at": r["first_indexed_at"] if "first_indexed_at" in r.keys() else "",
                        "schema_version": db_schema_ver,
                        "current_schema": CURRENT_SCHEMA_VERSION,
                        "reindex_needed": cc_stale or db_schema_ver < CURRENT_SCHEMA_VERSION,
                        "status": _list_status(db_schema_ver, cc_stale),
                        "db": str(db_path),
                    }
                )
        except (sqlite3.Error, OSError) as e:
            results.append({"db": str(db_path), "error": str(e)})
    return results


# ── moved from server.py ──
def reset_index(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    confirm: Annotated[
        bool, Field(description="Must be True to execute. Call without confirm first as dry-run.")
    ] = False,
) -> dict:
    """Delete the entire symbol index for a project.

    Not read-only — permanently deletes the SQLite database and WAL files.
    Call with ``confirm=False`` first (dry-run) to see what would be deleted.
    Re-index with ``fw-context index`` afterwards.

    Handles corrupt databases gracefully — you can delete a corrupt index
    without needing to open it first.

    Args:
        project_root: Project root directory. Auto-detected if omitted.
        confirm: Must be True to execute. Call without first as dry-run.

    Returns:
        dict: {project_root, db, project_id, action: "dry_run"|"deleted",
        message, symbol_count, indexed_at (dry-run)}.
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Nothing to reset."}
    project_id = derive_project_id(root)
    cfg_data = None
    sym_count = 0
    corrupt = False
    try:
        conn = open_db(db_path)
    except DatabaseCorruptionError:
        corrupt = True
    else:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if cfg_data:
                sym_count = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE config_hash=?",
                    (cfg_data["config_hash"],),
                ).fetchone()[0]
    info: dict[str, object] = {
        "project_root": str(root),
        "db": str(db_path),
        "project_id": project_id,
    }
    if cfg_data:
        info["symbol_count"] = sym_count
        info["indexed_at"] = cfg_data["created_at"]
    elif corrupt:
        info["warning"] = "Database is corrupt — integrity check failed."
    if not confirm:
        info["action"] = "dry_run"
        if corrupt:
            info["message"] = (
                f"Database at {db_path} is corrupt. "
                "Call reset_index(confirm=True) to delete it anyway, "
                "then run 'fw-context index' to rebuild."
            )
        else:
            info["message"] = f"Would delete {db_path}. Call reset_index(confirm=True) to proceed."
        return info
    from ..background import bg_reindex_pause

    with bg_reindex_pause(root):
        db_path.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            p = db_path.with_name(db_path.name + suffix)
            p.unlink(missing_ok=True)
        _invalidate_conn_cache(str(db_path.resolve()))
        info["action"] = "deleted"
        info["message"] = f"Index deleted. Run 'fw-context index' in {root} to rebuild."
    return info


# ── moved from server.py ──
def _reindex_cleanup_deleted_file(
    conn: sqlite3.Connection,
    cfg_data: sqlite3.Row,
    target: Path,
    root: Path,
    db_path: Path,
) -> dict:
    """Clean up index records for a file that no longer exists on disk."""
    config_hash = cfg_data["config_hash"]
    from ...indexer.db import (
        delete_fp_assignments_for_file as _del_fpa,
    )
    from ...indexer.db import (
        delete_indirect_call_sites_for_file as _del_ics,
    )
    from ...indexer.db import (
        delete_inheritance_for_file as _del_inh,
    )
    from ...indexer.db import (
        delete_refs_for_file as _del_refs,
    )
    from ...indexer.db import (
        delete_symbols_for_file as _del_syms,
    )
    from ...indexer.db import (
        get_file_mtimes,
    )
    from ...indexer.db import (
        write_lock as _db_write_lock,
    )
    from ...indexer.ops import _normalize_file_path
    from ..background import bg_reindex_pause

    known = get_file_mtimes(conn, config_hash)
    file_path_str = _normalize_file_path(str(target), root)
    if file_path_str not in known:
        return {"error": f"File not found on disk or in index: {target}"}

    file_id_old, _ = known[file_path_str]
    symbol_count = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE file_id = ?", (file_id_old,)
    ).fetchone()[0]

    try:
        tu_rel = str(target.relative_to(root))
    except ValueError:
        tu_rel = file_path_str

    with bg_reindex_pause(root):
        with _db_write_lock(db_path.parent, timeout=60.0):
            with transaction(conn):
                try:
                    conn.execute(
                        "DELETE FROM vec_symbols WHERE symbol_id IN "
                        "(SELECT id FROM symbols WHERE file_id = ?)",
                        (file_id_old,),
                    )
                except sqlite3.OperationalError:
                    pass
                _del_inh(conn, config_hash, file_id_old)
                _del_syms(conn, file_id_old)
                _del_refs(conn, config_hash, tu_rel)
                _del_ics(conn, config_hash, tu_rel)
                _del_fpa(conn, config_hash, tu_rel)
                conn.execute("DELETE FROM files WHERE id = ?", (file_id_old,))
        return {
            "file": str(target),
            "symbols_removed": symbol_count,
            "action": "deleted",
        }
    from ...mcp.shared.stale import _invalidate_modified_cache

    _invalidate_modified_cache(config_hash)


def _reindex_parse_and_store(
    conn: sqlite3.Connection,
    matching: list,
    cfg_data: sqlite3.Row,
    cfg_refs: bool,
    root: Path,
    db_path: Path,
    target: Path,
    vendor_patterns: list[str],
    project_patterns_list: list[str],
) -> tuple[int, dict]:
    """Parse matching TUs with libclang, store symbols, update manifest.

    Returns (total_symbols, partial_result_dict with elapsed time info).
    """
    from ...indexer.compile_commands import CompilationUnit as TU
    from ...indexer.db import write_lock as db_write_lock
    from ...indexer.ops import store_symbols_for_unit
    from ...indexer.symbols import (
        ExtractionResult,
        extract_all,
    )
    from ..background import bg_reindex_pause

    config_hash = cfg_data["config_hash"]

    parsed_units: list[tuple[TU, ExtractionResult]] = []
    skipped_tus: list[str] = []
    for unit in matching:
        try:
            parsed = extract_all(unit, with_refs=cfg_refs)
            parsed_units.append((unit, parsed))
        except sqlite3.Error as exc:
            return 0, {"error": f"DB error during parse of {unit.file.name}: {exc}"}
        except RuntimeError as exc:
            log.warning("skip TU %s during reindex: %s", unit.file.name, exc)
            skipped_tus.append(str(unit.file.name))

    with bg_reindex_pause(root):
        t0 = time.monotonic()
        total_symbols = 0

        try:
            with db_write_lock(db_path.parent, timeout=60.0):
                for unit, parsed in parsed_units:
                    with transaction(conn):
                        syms_added, _, _headers = store_symbols_for_unit(
                            conn, unit, config_hash, root,
                            vendor_patterns=vendor_patterns,
                            project_patterns=project_patterns_list,
                            index_refs=cfg_refs,
                            pre_parsed=parsed,
                        )
                        total_symbols += syms_added

                if parsed_units:
                    try:
                        from ...indexer.macros import resolve_and_update
                        first_unit = parsed_units[0][0]
                        resolve_and_update(conn, config_hash, first_unit.clang_args, first_unit.file.resolve())
                    except (RuntimeError, sqlite3.Error):
                        pass

                from ...indexer.db import delete_orphan_files
                deleted_orphans = delete_orphan_files(conn, config_hash)
                if deleted_orphans:
                    log.debug("Orphan files cleaned up: %d", deleted_orphans)

                _update_manifest_after_reindex(parsed_units, root, db_path.parent, config_hash)

                elapsed = round(time.monotonic() - t0, 2)
                result = {
                    "file": str(target),
                    "translation_units": len(matching),
                    "symbols_updated": total_symbols,
                    "elapsed_s": elapsed,
                }
                if skipped_tus:
                    result["skipped_tus"] = skipped_tus
                    result["skipped_count"] = len(skipped_tus)
                if not parsed_units and skipped_tus:
                    return 0, {"error": f"All {len(matching)} translation unit(s) failed to parse", "skipped_tus": skipped_tus}
            return total_symbols, result
        except sqlite3.Error as exc:
            return 0, {"error": f"DB error during reindex: {exc}"}
    from ...mcp.shared.stale import _invalidate_modified_cache
    _invalidate_modified_cache(config_hash)

def _update_manifest_after_reindex(
    parsed_units: list,
    root: Path,
    db_dir: Path,
    config_hash: str,
) -> None:
    """Update manifest.json entries for reindexed translation units."""
    try:
        from fw_context_mcp.utils import compute_source_hash

        from ...indexer.manifest import (
            _collect_headers_from_tokens,
            update_entry,
        )
        from ...indexer.manifest import (
            load as load_manifest,
        )
        from ...indexer.manifest import (
            save as save_manifest,
        )
        manifest_data = load_manifest(db_dir)
        if manifest_data is not None:
            for unit, _parsed in parsed_units:
                headers = _collect_headers_from_tokens(unit, root, build_dir_patterns=None)
                source_hash = compute_source_hash(unit.file.resolve())
                try:
                    tu_rel = str(unit.file.resolve().relative_to(root))
                except ValueError:
                    tu_rel = str(unit.file.resolve())
                for idx, entry in enumerate(manifest_data.get("entries", [])):
                    if entry.get("file") == tu_rel:
                        update_entry(manifest_data, idx, source_hash, headers)
                        break
                else:
                    manifest_data.setdefault("entries", []).append({
                        "file": tu_rel,
                        "directory": str(unit.directory) if unit.directory else str(root),
                        "arguments": unit.clang_args,
                        "source_hash": source_hash,
                        "headers": headers,
                    })
            save_manifest(manifest_data, db_dir, config_hash)
    except OSError:
        log.debug("manifest.json update skipped during reindex_file", exc_info=True)


def _reindex_post_write_phases(
    conn: sqlite3.Connection,
    config_hash: str,
    cfg: Config,
    total_symbols: int,
    db_dir: Path,
    target: Path,
    matching: list,
    result: dict,
    root: Path,
) -> dict:
    """Run post-write enrichment phases: LLM analysis, overrides, pagerank, embeddings."""
    if cfg.llm.enabled and cfg.llm.analyze_symbols and total_symbols > 0:
        try:
            from ...cache_client import CacheClient
            from ...indexer._llm_analysis import _build_llm_analysis
            try:
                stored_av = bool(
                    conn.execute(
                        "SELECT analyze_vendor FROM build_configs WHERE config_hash = ?",
                        (config_hash,),
                    ).fetchone()["analyze_vendor"]
                )
            except sqlite3.OperationalError:
                stored_av = False
            cc = CacheClient.from_config(cfg)
            try:
                _build_llm_analysis(
                    conn, config_hash, cfg.llm, db_dir,
                    write_lock_held=False, retry_unparseable=True,
                    cache_client=cc, project_only=not stored_av,
                )
                conn.commit()
            finally:
                if cc:
                    cc.close()
            analyzed_count = conn.execute(
                "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash = ?",
                (config_hash,),
            ).fetchone()[0]
            result["analysis_updated"] = analyzed_count
        except (sqlite3.Error, RuntimeError, OSError) as exc:
            result["analysis_warning"] = f"LLM analysis skipped: {exc}"

    if total_symbols > 0 and cfg.index.index_refs:
        try:
            from ...indexer.runner import _build_overrides
            _build_overrides(conn, config_hash, db_dir, write_lock_held=False, force=True)
            conn.commit()
        except (sqlite3.Error, RuntimeError) as exc:
            result["overrides_warning"] = f"Override analysis skipped: {exc}"

    if cfg.index.index_refs and total_symbols > 0:
        try:
            from ...indexer.runner import _build_hotspot_cache, _build_pagerank
            _build_pagerank(conn, config_hash, write_lock_held=False, force=True)
            _build_hotspot_cache(conn, config_hash, force=True)
            conn.commit()
        except (sqlite3.Error, RuntimeError) as exc:
            result["pagerank_warning"] = f"PageRank/hotspot recompute skipped: {exc}"

    if len(matching) == 1 and target.suffix.lower() in {".h", ".hpp"}:
        result["warning"] = (
            "Header re-indexed via one TU. Other TUs including this header may still have stale symbols — run 'fw-context index' for full accuracy."
        )

    if cfg.llm.enabled and total_symbols > 0:
        from ...indexer.ops import _normalize_file_path
        try:
            from ...indexer.runner import _build_embeddings as _reembed
            file_symbol_ids = [
                r[0] for r in conn.execute(
                    "SELECT id FROM symbols WHERE config_hash = ? AND file_path = ?",
                    (config_hash, _normalize_file_path(str(target), root)),
                ).fetchall()
            ]
            if file_symbol_ids:
                _reembed(conn, config_hash, cfg.llm, db_dir, symbol_ids=file_symbol_ids)
            else:
                log.warning(
                    "No symbols found for path %s in DB — skipping embedding regeneration "
                    "(this may happen for files outside the project root or after a symlink change)",
                    target,
                )
            conn.commit()
            reembedded = conn.execute(
                "SELECT COUNT(DISTINCT symbol_id) FROM embeddings e "
                "JOIN symbols s ON s.id = e.symbol_id WHERE s.config_hash = ?",
                (config_hash,),
            ).fetchone()[0]
            result["embeddings_updated"] = reembedded
        except (sqlite3.Error, RuntimeError, OSError) as exc:
            result["embedding_warning"] = f"Embedding regeneration skipped: {exc}"

    return result


# ── moved from server.py ──
def reindex_file_impl(
    file_path: Annotated[
        str,
        Field(
            description="Absolute or project-relative path to the source file to re-parse. Must have a matching entry in compile_commands.json."
        ),
    ],
    project_root: Annotated[
        str | None, Field(description="Project root directory. Auto-detected from cwd if omitted.")
    ] = None,
    *,
    with_analysis: Annotated[
        bool,
        Field(
            description="When True (default), also regenerates LLM symbol analysis and method override relationships — slower but produces a fully up-to-date index. Set False for a fast symbol-only update (used by background auto-reindex)."
        ),
    ] = True,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index."""
    db_path, cfg, project_id, root = _resolve_context(project_root, skip_ready_check=True)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err_result = _open_db_or_return(db_path)
    if err_result is not None:
        return err_result[0]
    assert conn is not None
    try:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}

        target, error = _reindex_resolve_target(file_path, root)
        if error:
            return error
        if not target.exists():
            return _reindex_cleanup_deleted_file(conn, cfg_data, target, root, db_path)

        matching, error = _reindex_match_tus(target, cfg_data)
        if error:
            return error

        vendor_patterns, project_patterns_list = _reindex_build_patterns(cfg, root)
        total_symbols, result = _reindex_parse_and_store(
            conn, matching, cfg_data, cfg.index.index_refs,
            root, db_path, target, vendor_patterns, project_patterns_list,
        )
        if "error" in result:
            return result
        if not with_analysis:
            return result

        config_hash = cfg_data["config_hash"]
        return _reindex_post_write_phases(
            conn, config_hash, cfg, total_symbols, db_path.parent,
            target, matching, result, root,
        )
    finally:
        try:
            conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        _invalidate_conn_cache(str(db_path.resolve()))


def _reindex_resolve_target(
    file_path: str, root: Path
) -> tuple[Path, dict | None]:
    """Resolve *file_path* to an absolute path relative to *root*.

    Returns ``(target, None)`` on success or ``(target, error_dict)``
    when the path cannot be resolved.
    """
    target = Path(file_path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()
    return target, None


def _reindex_match_tus(
    target: Path, cfg_data: sqlite3.Row
) -> tuple[list, dict | None]:
    """Find compilation units in compile_commands.json that build *target*.

    Returns ``(matching_tus, None)`` or ``(None, error_dict)``.
    """
    cc_path = Path(cfg_data["compile_commands_path"])
    if not cc_path.exists():
        return [], {"error": f"compile_commands.json not found: {cc_path}"}
    units = parse_cc(cc_path)
    matching = [u for u in units if Path(u.file).resolve() == target]
    if not matching:
        return [], {"error": f"{target.name} not found in compile_commands.json — it may be a header-only file."}
    return matching, None


def _reindex_build_patterns(
    cfg, root: Path
) -> tuple[list[str], list[str]]:
    """Collect vendor-exclude and project-include file path patterns."""
    from ...indexer.sdk_detect import _build_sdk_excludes, _normalize_patterns

    vendor_patterns = list(_build_sdk_excludes(root))
    if cfg.index.vendor_paths:
        vendor_patterns.extend(_normalize_patterns(cfg.index.vendor_paths))
    project_patterns_list = (
        _normalize_patterns(list(cfg.index.project_paths))
        if cfg.index.project_paths
        else []
    )
    return vendor_patterns, project_patterns_list


# ── moved from server.py ──
def reindex_file(
    file_path: Annotated[str, Field(description="Path to source file to re-parse. Must be in compile_commands.json.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index.

    Not read-only — uses the exact compiler flags from ``compile_commands.json``.
    The file must be listed in ``compile_commands.json`` (headers are re-indexed
    via the translation unit that includes them). Use after editing a file to
    keep the index current without a full rebuild.

    Also regenerates LLM analysis and method override relationships for
    affected symbols when those features are enabled in config.

    Args:
        file_path: Path to source file to re-parse. Must be in compile_commands.json.
        project_root: Project root directory. Auto-detected if omitted.

    Returns:
        dict: {file, translation_units, symbols_updated, elapsed_s,
        analysis_updated (if LLM enabled), or error}.
    """
    return reindex_file_impl(file_path, project_root, with_analysis=True)


# ── moved from server.py ──
def check_ollama(
    project_root: Annotated[
        str | None, Field(description="Project root. Auto-detected if omitted. Ignored by this tool.")
    ] = None,
) -> dict:
    """Check whether the LLM backend is running and the configured embedding/chat model is installed.

    Read-only: yes. No side effects. Call before smart_search,
    semantic_search, or explain_symbol (when on-demand fallback is
    expected — pre-computed analysis returns instantly without the LLM
    backend).

    Args:
        project_root: Project root. Auto-detected if omitted. Ignored
            by this tool.

    Returns:
        dict: {ollama_enabled (bool), status (str — "ok"|"disabled"|"error"|"model_missing"),
        ollama_running (bool), ollama_url (str), configured_model (str),
        num_ctx (int), installed_models (list[str]),
        configured_embed_model (str), embedding_installed (bool),
        message (str, on error/disabled), available_code_models (list[str], when model missing),
        debug_log (str, optional — only when debug logging is enabled)}
    """
    _, cfg, _, _ = _resolve_context(project_root)
    if not cfg.llm.enabled:
        return {
            "status": "disabled",
            "ollama_enabled": False,
            "ollama_running": False,
            "configured_model": cfg.llm.model,
            "num_ctx": cfg.llm.num_ctx,
            "message": (
                "LLM backend is disabled in config ([llm] enabled = false). "
                "explain_symbol with pre-computed analysis (default) returns instantly. "
                "Without analysis, it returns source + explain_prompt for the agent to answer. "
                "smart_search will use raw text queries."
            ),
        }
    result = check_setup(cfg.llm)
    result["ollama_enabled"] = True
    return result


def get_project_info(
    project_id: Annotated[str, Field(description="Project ID (UUID4 hex) to look up.")] = "",
) -> dict:
    """Return project metadata (name, type, root_path) for a project ID.

    Looks up the global project registry at ``~/.fw-context/projects.db``.
    Use this to identify a project from its UUID4 — find out what build
    system it uses, its name, and where it was last indexed.

    Read-only. No side effects.

    Args:
        project_id: Project ID (UUID4 hex) to look up.

    Returns:
        dict: {project_id, name, project_type, root_path, created_at, updated_at}
        or {"error": "..."} when the project_id is not registered.
    """
    from ...config.global_db import get_project_by_id

    if not project_id:
        return {"error": "project_id is required — provide a UUID4 hex string from fw-context init."}
    result = get_project_by_id(project_id)
    if result is None:
        return {"error": f"Project '{project_id}' not found in the global registry."}
    return result
