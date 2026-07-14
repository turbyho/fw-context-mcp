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
from ...indexer.symbols import (
    FnPointerAssignment,
    IndirectCallSite,
    InheritanceRecord,
    Macro,
    Symbol,
)
from ...llm.ollama import check_setup
from ...utils import resolve_project_root
from ..background import _is_bg_reindex_running
from ..shared.context import _db_path, _detect_build_system, _is_stale, _open_db_safe, _resolve_context
from ..shared.stale import _check_header_staleness, _count_modified_files

log = logging.getLogger(__name__)


# ── moved from server.py ──
def get_active_build(
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")] = None,
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

    When ``modified_files_count > 0``, a background ``fw-context index``
    subprocess is spawned automatically (non-blocking, at most one at a time).
    Queries continue to be served from the existing index while the new one
    is being built.  ``reindex_progress`` contains the last log line from
    the reindex subprocess when ``bg_reindex_running`` is True.

    Args:
        project_root: Project root directory. Auto-detected from CWD if
            omitted.

    Returns:
        dict: {config_hash, project_id, project_root, build_system,
        compile_commands, indexed_at (ISO timestamp), symbol_count, file_count,
        reference_count, modified_files_count (int), analyzed_symbols (int),
        unanalyzed_symbols (int — definition symbols still needing LLM analysis),
        analysis_model (str or None), bg_reindex_running (bool),
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
        return {
            "status": "no_index",
            "project_root": str(root),
            "index_message": (
                f"No symbol index found for {root}. "
                "Run `fw-context index <path/to/compile_commands.json>` via bash "
                "to build the index, then call get_active_build() again."
            ),
        }

    # Check bg reindex status BEFORE querying the DB — when a bg reindex
    # is running, the _modified_cache may contain stale counts from before
    # the reindex started.  Skipping the cache ensures accurate counts.
    bg_running = _is_bg_reindex_running(root)

    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return {"error": f"No build config indexed for project at {root}."}
            config_hash = cfg["config_hash"]
            sym_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (config_hash,)
            ).fetchone()[0]
            file_count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE config_hash=?", (config_hash,)
            ).fetchone()[0]
            ref_count = count_refs(conn, config_hash)
            deps_verification = cfg["deps_verification"] if "deps_verification" in cfg else "none"
            modified_count = _count_modified_files(conn, config_hash, root, use_cache=False)
            # Check header dependencies separately (different metric: TUs, not files)
            if deps_verification == "full":
                header_affected_tus, _ = _check_header_staleness(
                    conn, config_hash, root, use_cache=False,
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

            # Count definition symbols that still need LLM analysis
            unanalyzed_count = conn.execute(
                """SELECT COUNT(*)
                   FROM symbols s
                   WHERE s.config_hash = ?
                     AND s.is_definition = 1
                     AND s.kind IN ('function', 'method', 'constructor',
                                    'destructor', 'class', 'struct')
                     AND s.file_path NOT LIKE 'mbed-os/%'
                     AND s.file_path NOT LIKE '.pio/%'
                     AND s.file_path NOT LIKE 'zephyr/%'
                     AND s.file_path NOT LIKE 'build/%'
                     AND s.file_path NOT LIKE 'modules/%'
                     AND s.name NOT LIKE '%(anonymous%'
                     AND s.name NOT LIKE '%(unnamed%'
                     AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)""",
                (config_hash,),
            ).fetchone()[0]

            cc_changed = _is_stale(cfg, cfg["compile_commands_path"])
            schema_old = db_schema_ver < CURRENT_SCHEMA_VERSION
            needs_reindex = cc_changed or schema_old

            # Build reindex_reasons — only when reindex is actually needed
            reindex_reasons: list[str] = []
            if schema_old:
                reindex_reasons.append(f"schema_mismatch: {db_schema_ver} < {CURRENT_SCHEMA_VERSION}")
            if cc_changed:
                reindex_reasons.append("compile_commands_changed")

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
                    index_message = "Index is usable — background reindex in progress. All queries return accurate results."
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

            # When deps are not fully verified, emit a strict warning that the
            # LLM MUST relay to the user — header changes cannot be detected
            # without .d files, so the index may be silently stale.
            _warning = None
            if deps_verification != "full":
                _warning = (
                    "⚠️ INDEX DEGRADED — STOP ALL C/C++ ANALYSIS IMMEDIATELY.\n\n"
                    "Header dependency (.d) files are NOT available for this index "
                    f"(deps verification: {deps_verification}). "
                    "The index may contain STALE data — header changes cannot be "
                    "detected without .d files. Continuing analysis with a stale "
                    "index WILL produce incorrect results.\n\n"
                    "REQUIRED: Tell the user to run 'fw-context index' to rebuild "
                    "with full dependency tracking. Do NOT continue any C/C++ "
                    "analysis until the user confirms the index has been rebuilt."
                )
                index_message += f" — deps verification: {deps_verification} (run 'fw-context index' for full tracking)"

            if header_affected_tus:
                index_message += (
                    f" | {header_affected_tus} TU(s) have stale header "
                    f"dependencies — header changes since last index"
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
                "deps_verification": deps_verification,
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
            log_file = db_path.parent / "reindex.log"
            try:
                with open(log_file) as fh:
                    lines = fh.readlines()
                    result["reindex_progress"] = lines[-1].strip() if lines else None
            except (OSError, IndexError):
                result["reindex_progress"] = None
        return result
    finally:
        conn.close()

# ── moved from server.py ──
def _list_status(db_schema_ver: int, cc_stale: bool) -> str:
    """Return a status string for list_projects."""
    needs = (db_schema_ver < CURRENT_SCHEMA_VERSION) or cc_stale
    return "reindex_needed" if needs else "ready"


def list_projects(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted. Pass to distinguish multiple indexed projects.")] = None,
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
            conn, err = _open_db_safe(db_path)
            if err:
                results.append(err)
                continue
            assert conn is not None
            try:
                with conn:
                    rows = get_all_projects(conn)
                    db_schema_ver = get_db_schema_version(conn)
            finally:
                conn.close()
            for r in rows:
                cc_stale = _is_stale(
                    {"created_at": r["created_at"]},
                    r["compile_commands_path"],
                ) if r["compile_commands_path"] else False
                root = Path(r["root_path"]) if r["root_path"] else None
                results.append({
                    "project_id": r["project_id"],
                    "name": r["name"],
                    "root_path": r["root_path"],
                    "build_system": _detect_build_system(root) if root else "unknown",
                    "symbol_count": r["symbol_count"],
                    "file_count": r["file_count"],
                    "indexed_at": r["created_at"],
                    "schema_version": db_schema_ver,
                    "current_schema": CURRENT_SCHEMA_VERSION,
                    "reindex_needed": cc_stale or db_schema_ver < CURRENT_SCHEMA_VERSION,
                    "status": _list_status(db_schema_ver, cc_stale),
                    "db": str(db_path),
                })
        except Exception as e:
            results.append({"db": str(db_path), "error": str(e)})
    return results

# ── moved from server.py ──
def reset_index(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    confirm: Annotated[bool, Field(description="Must be True to execute. Call without confirm first as dry-run.")] = False,
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
        try:
            with conn:
                cfg_data = get_active_config(conn, project_id)
                if cfg_data:
                    sym_count = conn.execute(
                        "SELECT COUNT(*) FROM symbols WHERE config_hash=?",
                        (cfg_data["config_hash"],),
                    ).fetchone()[0]
        finally:
            conn.close()
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
            info["message"] = (
                f"Would delete {db_path}. "
                "Call reset_index(confirm=True) to proceed."
            )
        return info
    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    info["action"] = "deleted"
    info["message"] = f"Index deleted. Run 'fw-context index' in {root} to rebuild."
    return info

# ── moved from server.py ──
def reindex_file_impl(
    file_path: Annotated[str, Field(description="Absolute or project-relative path to the source file to re-parse. Must have a matching entry in compile_commands.json.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from cwd if omitted.")] = None,
    *,
    with_analysis: Annotated[bool, Field(description="When True (default), also regenerates LLM symbol analysis and method override relationships — slower but produces a fully up-to-date index. Set False for a fast symbol-only update (used by background auto-reindex).")] = True,
) -> dict:
    """Re-parse a single source file with libclang and update its symbols in the index.

    Shared implementation used by ``reindex_file`` (public tool, full analysis) and
    ``_auto_reindex_stale`` (background fast path, no LLM). Prefer ``reindex_file``
    for interactive use; call this directly only when you need to control
    *with_analysis* explicitly.

    Requires an existing index (``fw-context index`` must have been run first).
    The file must appear in ``compile_commands.json`` — header-only files are
    re-indexed via the ``.cpp`` translation unit that includes them.

    Args:
        file_path: Absolute or project-relative path to the source file to
            re-parse. Must have a matching entry in compile_commands.json.
        project_root: Project root directory. Auto-detected from cwd if omitted.
        with_analysis: When True (default), also regenerates LLM symbol analysis
            and method override relationships — slower but
            produces a fully up-to-date index. Set to False for a fast
            symbol-only update (used by background auto-reindex).

    Returns:
        On success — dict with keys:
            file (str): Resolved absolute path to the re-indexed file.
            translation_units (int): Number of TUs that include this file.
            symbols_updated (int): Number of symbols written/updated.
            elapsed_s (float): Parse + store time in seconds.
            analysis_updated (int, optional): Symbol count with fresh LLM
                analysis (only present when LLM analysis is enabled and
                with_analysis=True).
            analysis_warning (str, optional): Reason LLM analysis was skipped.
            overrides_warning (str, optional): Reason override analysis was skipped.
            warning (str, optional): Header re-indexed via a single TU — other
                TUs including this header may still have stale symbols; run
                ``fw-context index`` for full accuracy.
        On error — dict with key:
            error (str): Human-readable reason (no index found, file not found,
                file not in compile_commands.json, no build config indexed).
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    assert conn is not None
    try:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}
        target = Path(file_path)
        if not target.is_absolute():
            target = (root / target).resolve()
        else:
            target = target.resolve()
        if not target.exists():
            # File deleted from disk — clean up its records from the index.
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
            from ..background import _request_bg_reindex_pause, _resume_bg_reindex

            known = get_file_mtimes(conn, config_hash)
            file_path_str = str(target)
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

            _request_bg_reindex_pause(root)
            try:
                with _db_write_lock(db_path.parent, timeout=60.0):
                    with transaction(conn):
                        _del_inh(conn, config_hash, file_id_old)
                        _del_syms(conn, file_id_old)
                        # ON DELETE CASCADE → llm_analysis, embeddings removed
                        _del_refs(conn, config_hash, tu_rel)
                        _del_ics(conn, config_hash, tu_rel)
                        _del_fpa(conn, config_hash, tu_rel)
                        conn.execute("DELETE FROM files WHERE id = ?", (file_id_old,))
                return {
                    "file": str(target),
                    "symbols_removed": symbol_count,
                    "action": "deleted",
                }
            finally:
                _resume_bg_reindex(root)
                from ...mcp.shared.stale import _invalidate_modified_cache
                _invalidate_modified_cache(config_hash)

        cc_path = Path(cfg_data["compile_commands_path"])
        if not cc_path.exists():
            return {"error": f"compile_commands.json not found: {cc_path}"}
        units = parse_cc(cc_path)
        matching = [u for u in units if Path(u.file).resolve() == target]
        if not matching:
            return {"error": f"{target.name} not found in compile_commands.json — it may be a header-only file."}
        config_hash = cfg_data["config_hash"]
        source_roots = cfg.source_root_paths(root)
        exclude_paths = cfg.exclude_root_paths(root)

        from ...indexer.compile_commands import CompilationUnit as TU
        from ...indexer.db import write_lock as db_write_lock
        from ...indexer.ops import store_symbols_for_unit
        from ...indexer.symbols import (
            Reference,
            extract_all,
        )
        from ..background import _request_bg_reindex_pause, _resume_bg_reindex

        # ── Parse outside the write lock ──
        # libclang is CPU-bound; running it inside the lock serialises
        # parsing when multiple TUs match (e.g. reindexing a header
        # included by several .cpp files).
        parsed_units: list[tuple[TU, tuple[list[Symbol], list[Reference], list[InheritanceRecord], list[IndirectCallSite], list[FnPointerAssignment], list[Macro]]]] = []
        for unit in matching:
            try:
                parsed = extract_all(
                    unit,
                    source_roots=source_roots,
                    exclude_paths=exclude_paths,
                    with_refs=cfg.index.index_refs,
                )
                parsed_units.append((unit, parsed))
            except sqlite3.Error as exc:
                return {"error": f"DB error during parse of {unit.file.name}: {exc}"}
            except Exception as exc:
                msg = str(exc)
                if "unable to open database file" in msg:
                    return {"error": f"DB error during parse of {unit.file.name}: {exc}"}
                log.warning("skip TU %s during reindex: %s", unit.file.name, exc)

        # Request bg reindex to pause — manual operations take priority.
        # The bg process checks for the pause marker between TUs and
        # releases the write lock, allowing this operation to proceed
        # without erroring or killing anything.
        _request_bg_reindex_pause(root)

        t0 = time.monotonic()
        total_symbols = 0

        try:
            with db_write_lock(db_path.parent, timeout=60.0):
                # ── Phase 1: main symbol write ──
                for unit, parsed in parsed_units:
                    with transaction(conn):
                        syms_added, _ = store_symbols_for_unit(
                            conn, unit, config_hash, root,
                            source_roots=source_roots,
                            exclude_paths=exclude_paths,
                            index_refs=cfg.index.index_refs,
                            pre_parsed=parsed,
                        )
                        total_symbols += syms_added

                # Resolve expanded macro values for the re-indexed TUs.
                # Single call is sufficient — all TUs share the same flags.
                if parsed_units:
                    try:
                        from ...indexer.macros import resolve_and_update
                        first_unit = parsed_units[0][0]
                        resolve_and_update(conn, config_hash, first_unit.clang_args, first_unit.file.resolve())
                    except Exception:
                        pass

                elapsed = round(time.monotonic() - t0, 2)
                result: dict = {
                    "file": str(target),
                    "translation_units": len(matching),
                    "symbols_updated": total_symbols,
                    "elapsed_s": elapsed,
                }

                if not with_analysis:
                    return result

                # ── Phase 2: LLM analysis ──
                if cfg.llm.enabled and cfg.llm.analyze_symbols and total_symbols > 0:
                    try:
                        from ...cache_client import CacheClient
                        from ...indexer.runner import _build_llm_analysis
                        cc = CacheClient.from_config(cfg)
                        try:
                            _build_llm_analysis(conn, config_hash, cfg.llm, db_path.parent,
                                               write_lock_held=True, retry_unparseable=True,
                                               cache_client=cc)
                            conn.commit()
                        finally:
                            if cc:
                                cc.close()
                        analyzed_count = conn.execute(
                            "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash = ?",
                            (config_hash,),
                        ).fetchone()[0]
                        result["analysis_updated"] = analyzed_count
                    except Exception as exc:
                        result["analysis_warning"] = f"LLM analysis skipped: {exc}"

                # ── Phase 3: method override graph ──
                if total_symbols > 0:
                    try:
                        from ...indexer.runner import _build_overrides
                        _build_overrides(conn, config_hash, db_path.parent, write_lock_held=True)
                        conn.commit()
                    except Exception as exc:
                        result["overrides_warning"] = f"Override analysis skipped: {exc}"

                if len(matching) == 1 and target.suffix.lower() in {".h", ".hpp"}:
                    result["warning"] = "Header re-indexed via one TU. Other TUs including this header may still have stale symbols — run 'fw-context index' for full accuracy."

                return result
        except sqlite3.Error as exc:
            return {"error": f"DB error during reindex: {exc}"}
        finally:
            _resume_bg_reindex(root)
            from ...mcp.shared.stale import _invalidate_modified_cache
            _invalidate_modified_cache(config_hash)
    finally:
        conn.close()

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
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted. Ignored by this tool.")] = None,
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
