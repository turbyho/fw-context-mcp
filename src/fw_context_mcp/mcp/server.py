"""fw-context MCP server — build-aware code intelligence for embedded projects.

Search tools (search_code, smart_search) delegate to the search pipeline
(fw_context_mcp.search).  Everything else stays in this file as thin
handlers with shared helpers for DB access, staleness detection, etc.
"""

import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from mcp.server.fastmcp import FastMCP

from ..config import Config, derive_project_id
from ..config import load as load_config
from ..indexer.compile_commands import parse as parse_cc
from ..indexer import db as index_db
from ..indexer.db import (
    CURRENT_SCHEMA_VERSION,
    DatabaseCorruptionError,
    count_refs,
    find_refs,
    get_active_config,
    get_all_projects,
    get_db_schema_version,
    get_file_mtime_indexed,
    open_db,
    search_symbols,
    transaction,
)
from ..llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async, check_setup
from ..utils import MTIME_TOLERANCE_S, abs_path, resolve_project_root

log = logging.getLogger(__name__)

# ── MCP server instance ─────────────────────────────────────────────────────

mcp = FastMCP(
    "fw-context",
    instructions=(
        "Primary interface for reading and navigating indexed C/C++ embedded code. "
        "Prefer get_source, get_file_map, and get_symbol_context for reading symbols "
        "and functions — they use libclang extents and understand build flags. Use "
        "normal file reads for broader file context outside indexed symbols.\n\n"
        "Start every session with get_active_build() to check index health.\n\n"
        "Code reading: get_file_map (symbol table of contents) → get_source (function "
        "body, fast) or get_symbol_context (body + callers + callees in one call).\n\n"
        "Search: lookup_symbol (by exact/prefix name), search_code (by FTS5 keywords), "
        "smart_search (natural language via Ollama, slow).\n\n"
        "Call graph (refs must be indexed): find_callers, find_call_path, "
        "find_all_callers_recursive, find_callees_recursive, find_hotspots, "
        "find_dead_code, find_wrapper_callers, trace_data_flow.\n\n"
        "Maintenance: reindex_file (after editing a file), reset_index (destructive! "
        "re-index from scratch), check_ollama (before smart_search/explain_symbol), "
        "list_projects (discover indexed projects)."
    ),
)

# ── Shared helpers ──────────────────────────────────────────────────────────


def _db_path(project_root: Path) -> Path:
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _resolve_context(project_root: str | None) -> tuple[Path, Config, str, Path]:
    root = resolve_project_root(project_root)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    return cfg.index.db_dir / project_id / "index.db", cfg, project_id, root


def _open_db_safe(db_path: Path) -> tuple[sqlite3.Connection | None, dict | None]:
    try:
        return open_db(db_path), None
    except DatabaseCorruptionError as e:
        return None, {
            "error": f"Database corruption detected: {e.details}",
            "action": "reset_index",
            "hint": "Run reset_index() then fw-context index to rebuild.",
        }


def _is_stale(cfg, compile_commands_path: str) -> bool:
    try:
        cc_mtime = os.path.getmtime(compile_commands_path)
        indexed_at = datetime.fromisoformat(cfg["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + MTIME_TOLERANCE_S
    except Exception:
        return False


def _lookup_definition(conn, config_hash: str, name: str):
    BASE_QUERY = """SELECT s.* FROM symbols s
       WHERE s.config_hash=? AND %s
       ORDER BY %s s.line
       LIMIT 1"""
    for column in ("s.name", "s.qualified_name"):
        row = conn.execute(
            BASE_QUERY % (f"{column}=? AND s.is_definition=1", ""),
            (config_hash, name),
        ).fetchone()
        if row:
            return row
        row = conn.execute(
            BASE_QUERY % (f"{column}=?", "s.is_definition DESC,"),
            (config_hash, name),
        ).fetchone()
        if row:
            return row

    # Fallback: "Foo::bar" without namespace — extract short name, suffix-filter
    if "::" in name:
        short_name = name.rsplit("::", 1)[-1]
        FALLBACK_QUERY = """SELECT s.* FROM symbols s
           WHERE s.config_hash=? AND %s
           ORDER BY %s s.line"""
        for column in ("s.name", "s.qualified_name"):
            rows = conn.execute(
                FALLBACK_QUERY % (f"{column}=? AND s.is_definition=1", ""),
                (config_hash, short_name),
            ).fetchall()
            for row in rows:
                if row["qualified_name"].endswith(name):
                    return row
            rows = conn.execute(
                FALLBACK_QUERY % (f"{column}=?", "s.is_definition DESC,"),
                (config_hash, short_name),
            ).fetchall()
            for row in rows:
                if row["qualified_name"].endswith(name):
                    return row
    return None


def _read_symbol_body(file_path: str, line_no: int, end_line: int = 0, max_lines: int = 400) -> str:
    """Read up to *max_lines* lines around *line_no* without loading the entire file.

    When *end_line* is provided (from libclang) it is used as the exact body
    boundary.  Otherwise brace-matching finds the closing ``}``.
    """
    try:
        p = Path(file_path)
        if not p.exists():
            return ""
        # Read only the needed window — avoid loading huge generated files
        start_idx = max(0, line_no - 1)
        read_start = max(0, start_idx - 5)  # small margin for brace matching
        read_end = end_line + 5 if end_line else start_idx + max_lines + 5
        window: list[str] = []
        with p.open(errors="replace") as fh:
            for i, line in enumerate(fh):
                if i < read_start:
                    continue
                if i >= read_end:
                    break
                window.append(line.rstrip("\n\r"))
    except Exception:
        return ""

    if start_idx < read_start or not window:
        return ""
    local_start = start_idx - read_start
    if local_start < 0 or local_start >= len(window):
        return ""

    if end_line and end_line >= line_no:
        local_end = min(len(window) - 1, end_line - 1 - read_start)
        return "\n".join(f"{read_start + i + 1:4d}  {window[i]}" for i in range(local_start, local_end + 1))

    # Brace matching
    depth = 0
    seen_open = False
    local_end = local_start
    for i in range(local_start, len(window)):
        for ch in window[i]:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
        local_end = i
        if seen_open and depth <= 0:
            break
    if not seen_open:
        local_end = min(len(window) - 1, local_start + 2)
    return "\n".join(f"{read_start + i + 1:4d}  {window[i]}" for i in range(local_start, local_end + 1))


def _stale_files(conn, config_hash: str, file_paths: list[str]) -> list[str]:
    stale = []
    for path in dict.fromkeys(file_paths):
        try:
            stored = get_file_mtime_indexed(conn, config_hash, path)
            if stored is None:
                continue
            if os.path.getmtime(path) > stored + MTIME_TOLERANCE_S:
                stale.append(path)
        except OSError:
            pass
    return stale


def _count_modified_files(conn, config_hash: str, root: Path) -> int:
    modified = 0
    rows = conn.execute(
        "SELECT path, mtime FROM files WHERE config_hash=?", (config_hash,)
    ).fetchall()
    for r in rows:
        path = r["path"]
        stored = r["mtime"]
        if not stored:
            # mtime=0 from a pre-migration database — unknown, treat as stale
            modified += 1
            continue
        p = Path(path)
        if not p.is_absolute():
            p = (root / path).resolve()
        try:
            if p.stat().st_mtime > stored + MTIME_TOLERANCE_S:
                modified += 1
        except OSError:
            pass
    return modified


def _detect_build_system(root: Path) -> str:
    if (root / "mbed-os").is_dir() or (root / "mbed_app.json").exists():
        return "mbed-os"
    if (root / "west.yml").exists() or (root / "prj.conf").exists():
        return "zephyr"
    if (root / "platformio.ini").exists():
        return "platformio"
    return "unknown"


def _auto_reindex_stale(
    stale_files: list[str],
    project_root: Path,
    max_files: int = 5,
    timeout_s: float = 30.0,
) -> tuple[list[str], list[str]]:
    succeeded: list[str] = []
    failed: list[str] = []
    t0 = time.monotonic()
    for fp in stale_files[:max_files]:
        if time.monotonic() - t0 > timeout_s:
            break
        try:
            result = reindex_file(fp, str(project_root))
            if "error" in result:
                failed.append(fp)
            else:
                succeeded.append(fp)
        except Exception:
            failed.append(fp)
    return succeeded, failed


# ── Tools (non-search) ──────────────────────────────────────────────────────


@mcp.tool()
def get_active_build(
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")] = None,
) -> dict:
    """Return metadata about the most recently indexed build configuration.

    Read-only: yes. Call at session start to check if the index exists,
    how many symbols it contains, and whether it is stale (needs re-index).

    Returns:
        dict: {index_exists, project_id, total_symbols, total_files,
        total_refs, config_hash, last_indexed (ISO timestamp),
        stale (bool — True if compile_commands.json is newer than index),
        schema_version (int — DB schema version), current_schema (int — code expects)}
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
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
            modified_count = _count_modified_files(conn, config_hash, root)
            db_schema_ver = get_db_schema_version(conn)
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
                "schema_version": db_schema_ver,
                "current_schema": CURRENT_SCHEMA_VERSION,
                "stale": _is_stale(cfg, cfg["compile_commands_path"]) or modified_count > 0
                or db_schema_ver < CURRENT_SCHEMA_VERSION,
            }
        return result
    finally:
        conn.close()


def _with_stale_recovery(
    root: Path,
    db_path: Path,
    query_fn,
    *,
    stale_msg: str = "",
) -> list[dict]:
    """Execute *query_fn(conn, config_hash)* with automatic stale-recovery.

    When the index or result files are stale:
    1. Auto-reindex up to 5 stale files (30 s timeout).
    2. Re-run *query_fn* against a fresh connection.
    3. Aggregate warnings.

    Connections are always closed before returning.
    """
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    try:
        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]
            config_hash = cfg["config_hash"]
            result_rows = query_fn(conn, config_hash)
            stale_f = _stale_files(
                conn, config_hash,
                [abs_path(root, r["file"]) for r in result_rows if "file" in r],
            )
    finally:
        conn.close()

    results: list[dict] = []
    if _is_stale(cfg, cfg["compile_commands_path"]):
        results.append({"warning": stale_msg or "Index may be stale — compile_commands.json changed since last index. Run 'fw-context index' to update."})
    if stale_f:
        succeeded, failed = _auto_reindex_stale(stale_f, root)
        if succeeded:
            conn2, err2 = _open_db_safe(db_path)
            if err2:
                results.append({"warning": f"Auto-reindex partially succeeded ({len(succeeded)} files), but DB is now corrupt."})
                results += result_rows
                return results
            try:
                with conn2:
                    result_rows = query_fn(conn2, config_hash)
            finally:
                conn2.close()
        if failed:
            results.append({"warning": f"Auto-reindex failed for {len(failed)} file(s): {', '.join(failed[:3])}. Run 'fw-context index' manually."})
    results += result_rows
    return results


@mcp.tool()
def lookup_symbol(
    name: Annotated[str, Field(description="Symbol name. Exact match if exact=True, prefix LIKE match otherwise. E.g. 'uart_init' or 'uart_'.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected from CWD if omitted.")] = None,
    exact: Annotated[bool, Field(description="True = exact name match, False = prefix LIKE match (default).")] = False,
    limit: Annotated[int, Field(description="Maximum results returned (default 50).")] = 50,
) -> list[dict]:
    """Look up a symbol by name — exact or prefix matching.

    Returns all declarations and definitions matching the name across the
    entire indexed codebase. Prefer this over search_code when you know the
    exact symbol name or a prefix of it (``uart_`` finds all UART symbols).
    Use search_code for keyword/concept search when you don't know the name.

    Read-only: yes. May auto-reindex stale files (non-blocking).

    Args:
        name: Symbol name (exact match) or prefix (set exact=False).
            E.g. 'uart_init' finds the exact function; 'uart_' finds
            all symbols starting with 'uart_'.
        project_root: Project directory. Auto-detected if omitted.
        exact: True = exact name match, False = prefix LIKE match (default).
        limit: Maximum results (default 50).

    Returns:
        list[dict]: Symbols with name, qualified_name, kind, file, line,
        signature, docstring, is_definition fields. Enum constants include
        ``enum_value`` with the integer value. Empty if not found.
    """
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]

        limit = min(limit, 100)

        def _do_lookup(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            if exact:
                rows = c.execute(
                    """SELECT s.* FROM symbols s
                       WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
                       ORDER BY s.is_definition DESC, s.line
                       LIMIT ?""",
                    (config_hash, name, name, limit),
                ).fetchall()
            else:
                esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = c.execute(
                    r"""SELECT s.* FROM symbols s
                       WHERE s.config_hash=? AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
                       ORDER BY s.is_definition DESC, s.line
                       LIMIT ?""",
                    (config_hash, f"{esc}%", f"{esc}%", limit),
                ).fetchall()

            # Fallback: "Foo::bar" without namespace — extract short name, suffix-filter
            if not rows and "::" in name:
                short_name = name.rsplit("::", 1)[-1]
                if exact:
                    rows = c.execute(
                        """SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, short_name, short_name, limit * 2),
                    ).fetchall()
                else:
                    esc2 = short_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    rows = c.execute(
                        r"""SELECT s.* FROM symbols s
                           WHERE s.config_hash=? AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        (config_hash, f"{esc2}%", f"{esc2}%", limit * 2),
                    ).fetchall()
                rows = [r for r in rows if r["qualified_name"].endswith(name)][:limit]

            # Did-you-mean? suggestions when nothing matched
            _suggestions: list[str] = []
            if not rows:
                try:
                    from ..search.did_you_mean import suggest as suggest_names
                    _suggestions = suggest_names(c, config_hash, name, limit=5)
                except Exception:
                    pass  # suggestions are best-effort

            result = [
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": abs_path(root, r["file_path"]),
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                    **({"enum_value": r["enum_value"]} if r["enum_value"] is not None else {}),
                }
                for r in rows
            ]
            if _suggestions:
                result.append({"_did_you_mean": _suggestions})
            return result

        return _with_stale_recovery(root, db_path, _do_lookup)
    except Exception as e:
        log.exception("lookup_symbol failed: %s", e)
        return [{"error": f"lookup_symbol failed: {e}"}]


@mcp.tool()
def list_projects(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted. Pass to distinguish multiple indexed projects.")] = None,
) -> list[dict]:
    """Read-only. No side effects. Lists all firmware projects that have been indexed with fw-context, showing each project's database path, symbol count, and last re-index time. Use at session start to discover available projects; use get_active_build for details on the current project."""
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
                    "stale": cc_stale or db_schema_ver < CURRENT_SCHEMA_VERSION,
                    "db": str(db_path),
                })
        except Exception as e:
            results.append({"db": str(db_path), "error": str(e)})
    return results


@mcp.tool()
def reset_index(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    confirm: Annotated[bool, Field(description="Must be True to execute. Call without confirm first as dry-run.")] = False,
) -> dict:
    """Not read-only — deletes the entire symbol index. Call without confirm=True first as dry-run. Re-index with 'fw-context index' afterwards."""
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


@mcp.tool()
def reindex_file(
    file_path: Annotated[str, Field(description="Path to source file to re-parse. Must be in compile_commands.json.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Not read-only — re-parses a single source file with libclang using the exact compiler flags from compile_commands.json and updates its symbols in the SQLite+FTS5 index. The file must be in compile_commands.json. Use after editing a file to keep the index current without rebuilding; use reset_index to rebuild from scratch."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            target = Path(file_path).resolve()
            if not target.exists():
                return {"error": f"File not found: {target}"}
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
            t0 = time.monotonic()
            total_symbols = 0
            from ..indexer.ops import store_symbols_for_unit
            for unit in matching:
                with transaction(conn):
                    syms_added, _ = store_symbols_for_unit(
                        conn, unit, config_hash, root,
                        source_roots=source_roots,
                        exclude_paths=exclude_paths,
                        index_refs=cfg.index.index_refs,
                    )
                    total_symbols += syms_added
        elapsed = round(time.monotonic() - t0, 2)
        result: dict = {
            "file": str(target),
            "translation_units": len(matching),
            "symbols_updated": total_symbols,
            "elapsed_s": elapsed,
        }
    finally:
        conn.close()
    if len(matching) == 1 and target.suffix.lower() in {".h", ".hpp"}:
        result["warning"] = "Header re-indexed via one TU. Other TUs including this header may still have stale symbols — run 'fw-context index' for full accuracy."
    return result


@mcp.tool()
def check_ollama(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted. Ignored by this tool.")] = None,
) -> dict:
    """Check whether Ollama is running and the configured embedding/chat model is installed.

    Read-only: yes. No side effects. Call before smart_search or
    explain_symbol if unsure about Ollama availability.

    Returns:
        dict: {ollama_running: bool, model_installed: bool, model: str,
        error: str or None}
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
                "Ollama is disabled in config ([llm] enabled = false). "
                "explain_symbol will return source + explain_prompt for the agent to answer. "
                "smart_search will use raw text queries."
            ),
        }
    result = check_setup(cfg.llm)
    result["ollama_enabled"] = True
    return result


@mcp.tool()
async def explain_symbol(
    name: Annotated[str, Field(description="Symbol name to explain. E.g. 'uart_init', 'ModemMsg::send'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    context_lines: Annotated[int, Field(description="Lines of source context around the symbol definition.")] = 40,
) -> dict:
    """Read-only. No side effects — may call Ollama (optional external LLM) if configured. Returns a plain-English explanation of what a C/C++ symbol does, based on its name, signature, docstring, and call context. Requires Ollama to be running. For raw source code use get_source; for symbol metadata without explanation use lookup_symbol; for body+callers+callees use get_symbol_context."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]
            row = _lookup_definition(conn, config_hash, name)
            if not row:
                return {"error": f"Symbol not found: {name}"}
            file_path = abs_path(root, row["file_path"])
            line_no = row["line"]
            signature = row["signature"] or ""
            kind = row["kind"]
    finally:
        conn.close()
    context_lines = min(context_lines, 200)
    source_snippet = ""
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
        start = max(0, line_no - context_lines - 1)
        end = min(len(lines), line_no + context_lines)
        numbered = "\n".join(
            f"{i + start + 1:4d}  {lines[i + start]}" for i in range(end - start)
        )
        source_snippet = numbered
    except Exception:
        pass
    prompt = (
        f"You are a C/C++ embedded firmware expert.\n"
        f"Explain what the following {kind} does. "
        f"Cover: (1) what it does and why, (2) key mechanism or logic, (3) when/how it fits in the system.\n\n"
        f"Symbol: {name}\n"
        f"File: {file_path}:{line_no}\n"
        f"Signature: {signature}\n"
    )
    if source_snippet:
        prompt += f"\nSource context:\n```cpp\n{source_snippet}\n```\n"
    result: dict = {
        "name": name,
        "kind": kind,
        "file": file_path,
        "line": line_no,
        "signature": signature,
    }
    if cfg.llm.enabled:
        try:
            result["explanation"] = await call_ollama_async(prompt, cfg.llm)
        except OllamaModelNotFoundError as e:
            result["warning"] = (
                f"{e}. No Ollama model available — interpret the 'source' and "
                f"'explain_prompt' fields below with your own LLM to provide the explanation."
            )
        except OllamaError as e:
            result["warning"] = (
                f"Ollama unavailable: {e}. No local LLM — interpret the 'source' and "
                f"'explain_prompt' fields below with your own LLM to provide the explanation."
            )
        else:
            return result
    result["source"] = source_snippet[:4000] if len(source_snippet) > 4000 else source_snippet
    result["explain_prompt"] = prompt
    return result


@mcp.tool()
def get_source(
    name: Annotated[str, Field(description="Fully qualified symbol name. Returns exact function body via libclang extent.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Read-only. Preferred way to read a function/method/enum body — uses libclang
    for exact extents instead of guessing from line numbers. No LLM, fast.

    For enums, includes a ``constants`` array listing all member constants
    with their names and integer values.

    For rich context (who calls this, what does it call) use
    get_symbol_context instead. For the full file, use a normal file read.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            row = _lookup_definition(conn, cfg_data["config_hash"], name)
            if not row:
                return {"error": f"Symbol not found: {name}"}
            file_path = abs_path(root, row["file_path"])
            result: dict = {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "file": file_path,
                "line": row["line"],
                "signature": row["signature"] or "",
                "is_definition": bool(row["is_definition"]),
            }
            if row["enum_value"] is not None:
                result["enum_value"] = row["enum_value"]
            # For enums, collect all constants with their values
            if row["kind"] == "enum":
                qn = row["qualified_name"]
                const_rows = conn.execute(
                    """SELECT name, qualified_name, line, enum_value
                       FROM symbols
                       WHERE config_hash = ? AND kind = 'enum_constant'
                         AND (qualified_name LIKE ? OR qualified_name LIKE ?)
                       ORDER BY line""",
                    (cfg_data["config_hash"], f"{qn}::%", f"%{qn}::%"),
                ).fetchall()
                if const_rows:
                    result["constants"] = [
                        {
                            "name": c["name"],
                            **({"enum_value": c["enum_value"]} if c["enum_value"] is not None else {}),
                        }
                        for c in const_rows
                    ]
            end_line = row["end_line"] or 0
            line_no = row["line"]
    finally:
        conn.close()
    source = _read_symbol_body(file_path, line_no, end_line=end_line)
    if not source:
        result["warning"] = f"Could not read source from {file_path}"
    else:
        result["source"] = source[:8000] if len(source) > 8000 else source
    return result


@mcp.tool()
def get_file_map(
    file_path: Annotated[str, Field(description="Path to source file — relative to project root or just filename.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    signatures: Annotated[bool, Field(description="Include full function signatures in output.")] = False,
    max_per_kind: Annotated[int, Field(description="Max items per symbol kind group (default 30, 0 = unlimited).")] = 30,
) -> dict:
    """Return all symbols in a file grouped by kind — fast structural overview.

    Like a table of contents before reading a chapter. Pass a path relative
    to the project root (``src/main.cpp``) or just the filename (``main.cpp``).
    Returns symbols keyed by kind (function, method, class, struct, enum, ...).
    Each kind has count (total) and items (first N, default 30).
    Set max_per_kind=0 for unlimited, signatures=true for full sigs.

    Enum constants (``enum_constant``) are grouped into ``subgroups`` by
    parent enum. Each subgroup has ``name``, ``count``, and ``constants``
    (list of ``{name, qualified_name, line, enum_value}``). The subgroup
    count reflects the real total even when ``max_per_kind`` limits the
    constants list.

    Read-only: yes. No side effects. Use before reading a large file to
    orient yourself — see what functions, classes, and enums it defines.

    Args:
        file_path: Path relative to project root, or just the filename.
        project_root: Project directory. Auto-detected if omitted.
        signatures: Include full function signatures. Default: False.
        max_per_kind: Max items per kind group (default 30, 0 = unlimited).

    Returns:
        dict: {file, total_symbols, symbols: {kind: {count, items[],
        subgroups?[]}}}
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]
            # Resolve file_path: try exact match first, then suffix
            exact = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND file_path=?",
                (config_hash, file_path),
            ).fetchone()[0]
            if not exact:
                # Try to find the canonical path from the files table
                candidates = conn.execute(
                    "SELECT path FROM files WHERE config_hash=? AND path LIKE ? LIMIT 3",
                    (config_hash, f"%{file_path}"),
                ).fetchall()
                if not candidates:
                    return {"error": f"File not found in index: {file_path}. Check the path — use relative paths like 'src/main.cpp'."}
                # Pick the best match (shortest path that ends with file_path)
                resolved = min((c["path"] for c in candidates), key=len)
            else:
                resolved = file_path
            result = index_db.get_file_map(
                conn, config_hash, resolved,
                signatures=signatures, max_per_kind=max_per_kind,
            )
    finally:
        conn.close()
    return result


@mcp.tool()
def get_symbol_context(
    name: Annotated[str, Field(description="Symbol name. Returns body, signature, all direct callers and callees.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Read-only. Rich one-shot context for a symbol: body, signature, all direct
    callers and callees. Answers "what does this do and how does it fit in the
    system?" in a single response.

    For body-only use get_source (faster). For transitive call-graph exploration
    use find_all_callers_recursive or find_callees_recursive.

    Returns dict with: name, qualified_name, kind, file, line, signature,
    is_definition, callers (list), callees (list), source (body text).
    For enums also returns constants and enum_value.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return {"error": "No build config indexed."}
            config_hash = cfg_data["config_hash"]
            row = _lookup_definition(conn, config_hash, name)
            if not row:
                return {"error": f"Symbol not found: {name}"}
            file_path = abs_path(root, row["file_path"])
            symbol_usr = row["usr"]

            # Immediate callers (who calls this symbol, direct + indirect)
            callers = find_refs(conn, config_hash, name, ref_kind=["call", "indirect"])
            callers_list = [
                {"name": c["caller_name"] or "?", "qualified_name": c["caller_qname"] or "",
                 "file": abs_path(root, c["caller_file"] or c["from_file"]),
                 "line": c["from_line"], "kind": c["caller_kind"] or "",
                 "ref_kind": c["ref_kind"]}
                for c in callers
            ]

            # Immediate callees (what this symbol calls, direct + indirect)
            callees_rows = conn.execute(
                """SELECT s.name, s.qualified_name, s.kind, s.file_path, r.ref_kind
                   FROM refs r
                   JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
                   WHERE r.from_usr = ? AND r.config_hash = ?
                     AND r.ref_kind IN ('call', 'indirect')""",
                (symbol_usr, config_hash),
            ).fetchall()
            callees_list = [
                {"name": c["name"], "qualified_name": c["qualified_name"] or "",
                 "kind": c["kind"], "file": abs_path(root, c["file_path"]),
                 "ref_kind": c["ref_kind"]}
                for c in callees_rows
            ]

            # For enums, collect all constants with their values
            enum_constants: list[dict] = []
            if row["kind"] == "enum":
                qn = row["qualified_name"]
                const_rows = conn.execute(
                    """SELECT name, qualified_name, line, enum_value
                       FROM symbols
                       WHERE config_hash = ? AND kind = 'enum_constant'
                         AND (qualified_name LIKE ? OR qualified_name LIKE ?)
                       ORDER BY line""",
                    (config_hash, f"{qn}::%", f"%{qn}::%"),
                ).fetchall()
                enum_constants = [
                    {
                        "name": c["name"],
                        **({"enum_value": c["enum_value"]} if c["enum_value"] is not None else {}),
                    }
                    for c in const_rows
                ]
    finally:
        conn.close()

    source = _read_symbol_body(file_path, row["line"], end_line=row["end_line"] or 0)
    result: dict = {
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "file": file_path,
        "line": row["line"],
        "signature": row["signature"] or "",
        "is_definition": bool(row["is_definition"]),
        "callers": callers_list,
        "callees": callees_list,
    }
    if row["enum_value"] is not None:
        result["enum_value"] = row["enum_value"]
    if enum_constants:
        result["constants"] = enum_constants
    if source:
        result["source"] = source[:6000] if len(source) > 6000 else source
    return result


def _references_result(name: str, project_root: str | None, ref_kind: str | list[str] | None, limit: int, *, caller_mode: bool = False) -> list[dict]:
    """Shared logic for find_callers / find_references."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return [{"error": "No build config indexed."}]
            config_hash = cfg_data["config_hash"]
            if count_refs(conn, config_hash) == 0:
                return [{"info": (
                    "No references indexed. Refs are on by default — "
                    "they may have been disabled with [index] index_refs = false. "
                    "Re-run 'fw-context index' to rebuild with refs enabled."
                )}]
            limit = min(limit, 200)
            rows = find_refs(conn, config_hash, name, ref_kind=ref_kind, limit=limit)
            if not rows:
                label = "callers" if caller_mode else "references"
                return [{"info": f"No {label} found for '{name}'. Check the name (exact match) and that the index is current."}]
            result: list[dict] = [
                {
                    "file": abs_path(root, r["from_file"]),
                    "line": r["from_line"],
                    "ref_kind": r["ref_kind"],
                    "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
                    "caller_kind": r["caller_kind"],
                }
                for r in rows
            ]
        return result
    finally:
        conn.close()


@mcp.tool()
def find_callers(
    name: Annotated[str, Field(description="Symbol name to find callers of. Returns direct call sites and indirect calls via function pointers.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results.")] = 50,
) -> list[dict]:
    """Read-only. Find call sites of a function/method — who calls ``name`` (direct + indirect via function pointers).

    Use when you need a quick, flat list of immediate callers. For the full
    transitive call tree (who calls this indirectly through other functions),
    use ``find_all_callers_recursive``.  For all references including reads
    and member accesses, use ``find_references``.  For a path between two
    specific symbols, use ``find_call_path``.

    Requires the reference index (``fw-context index`` — refs are on by
    default).  Only direct call sites are returned; callers more than one
    hop away are not included.
    """
    return _references_result(name, project_root, ref_kind=["call", "indirect"], limit=limit, caller_mode=True)


@mcp.tool()
def find_references(
    name: Annotated[str, Field(description="Symbol name to find all references of — calls, reads, member accesses.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results.")] = 50,
) -> list[dict]:
    """Read-only. No side effects. Returns all references to a symbol (call sites, reads, member accesses) across the indexed codebase. Requires refs to be indexed (fw-context index --refs). For direct callers only use find_callers; for transitive callers use find_all_callers_recursive; for call paths between two symbols use find_call_path."""
    return _references_result(name, project_root, ref_kind=None, limit=limit)


# ── Graph analytics tools ─────────────────────────────────────────────────────


def _refs_guard(project_root: str | None) -> tuple[Path, str, str] | tuple[None, None, list[dict]]:
    """Shared guard for graph tools: resolve project, open DB, check refs exist.

    Returns ``(root, config_hash, None)`` on success or ``(None, None, error_list)``
    on failure (caller propagates the error list directly).
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return None, None, [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return None, None, [err]
    try:
        project_id = derive_project_id(root)
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return None, None, [{"error": "No build config indexed."}]
            config_hash = cfg_data["config_hash"]
            if count_refs(conn, config_hash) == 0:
                return None, None, [{"info": (
                    "No references indexed. Refs are on by default — "
                    "they may have been disabled with [index] index_refs = false. "
                    "Re-run 'fw-context index' to rebuild."
                )}]
            return root, config_hash, None
    except Exception:
        conn.close()
        raise
    finally:
        conn.close()


@mcp.tool()
def find_call_path(
    from_name: Annotated[str, Field(description="Starting symbol for path search.")],
    to_name: Annotated[str, Field(description="Target symbol to find path to.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for path search (default 10).")] = 10,
) -> list[dict]:
    """Read-only. Find call paths between two functions via BFS in the call graph.

    Use to answer "how does A reach B?" — e.g. tracing how a high-level
    event handler eventually calls a low-level driver.  Returns up to 5
    shortest paths, each with ``depth`` (edge count) and ``chain``
    (e.g. ``"main → app_run → modem_init"``).

    For one-sided exploration use ``find_all_callers_recursive`` (who reaches
    this?) or ``find_callees_recursive`` (what does this reach?).
    Requires both symbols to be in the index and refs enabled
    (``fw-context index`` — refs on by default).
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        rows = index_db.find_call_path(conn, config_hash, from_name, to_name, max_depth=max_depth)
        if not rows:
            return [{"info": f"No path found from '{from_name}' to '{to_name}' within depth {max_depth}."}]
        return rows
    finally:
        conn.close()


@mcp.tool()
def find_all_callers_recursive(
    name: Annotated[str, Field(description="Symbol name to find transitive callers of.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive search (default 5).")] = 5,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
) -> list[dict]:
    """Read-only. Find all transitive callers — who calls *name*, directly or indirectly.

    Use for impact analysis: "if I change this function, how far does the
    ripple go?"  Returns callers at depth 1 (direct), depth 2 (callers of
    callers), up to ``max_depth`` (default 5).  Results are deduplicated —
    each caller appears once at its shortest distance to the target.

    For a flat, single-level caller list use ``find_callers`` (faster).
    Requires the reference index (``fw-context index`` — refs on by default).
    BFS from the target outward; performance scales with call-graph fan-out.
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        rows = index_db.find_all_callers_recursive(conn, config_hash, name, max_depth=max_depth, limit=limit)
        if not rows:
            return [{"info": f"No callers found for '{name}'."}]
        return rows
    finally:
        conn.close()


@mcp.tool()
def find_callees_recursive(
    name: Annotated[str, Field(description="Symbol name to find transitive callees of.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive search (default 5).")] = 5,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
) -> list[dict]:
    """Read-only. Find all transitive callees — what *name* calls, directly or indirectly.

    Use for dependency analysis: "what does this function depend on to do
    its job?"  Returns callees at depth 1 (direct), depth 2 (callees of
    callees), up to ``max_depth`` (default 5).  Results are deduplicated
    by shortest distance.

    For direct callees only, ``get_symbol_context`` gives a faster flat
    list along with the function body and callers.
    Requires the reference index (``fw-context index`` — refs on by default).
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        rows = index_db.find_callees_recursive(conn, config_hash, name, max_depth=max_depth, limit=limit)
        if not rows:
            return [{"info": f"No callees found for '{name}'."}]
        return rows
    finally:
        conn.close()


@mcp.tool()
def find_dead_code(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 100).")] = 100,
    exclude_paths: Annotated[list[str] | None, Field(description="File path LIKE patterns to exclude. E.g. ['mbed-os/%', 'zephyr/%'] for SDK paths. No default — pass explicitly to filter vendor noise.")] = None,
) -> list[dict]:
    """Read-only. Find functions/methods that are defined but never called.

    Use to spot unused code candidates.  Expect false positives:
    constructors called via factories or template instantiation, interrupt
    handlers (ISRs), virtual method overrides, and weak-aliased symbols
    often have no direct calls in the reference index.  Verify each hit
    with ``find_callers`` before deleting.

    Use ``exclude_paths`` to filter vendor SDK noise (e.g.
    ``['mbed-os/%', 'zephyr/%']``). Requires the reference index.
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        rows = index_db.find_dead_code(conn, config_hash, limit=limit, exclude_paths=exclude_paths)
        if not rows:
            return [{"info": "No dead code found — every defined function has at least one caller."}]
        return rows
    finally:
        conn.close()


@mcp.tool()
def find_wrapper_callers(
    class_name: Annotated[str, Field(description="Driver class name to find wrappers for. E.g. 'UART_DRIVER' or 'hal::UART_DRIVER'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum wrapper method results (default 50).")] = 50,
) -> list[dict]:
    """Read-only. Find wrapper classes that call methods of a driver class.

    Returns wrapper methods grouped by wrapper class, showing which driver
    methods each wrapper calls.  Useful for understanding the adapter/wrapper
    architecture (e.g. ``UART`` wraps ``UART_DRIVER``).
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        # Resolve driver class USR — find all methods of the class
        driver_methods = conn.execute(
            """SELECT s.usr, s.name, s.qualified_name
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.kind = 'method'
                 AND (s.qualified_name LIKE ? OR s.qualified_name LIKE ?)
               ORDER BY s.name""",
            (config_hash, f"{class_name}::%", f"%{class_name}::%"),
        ).fetchall()

        if not driver_methods:
            return [{"info": f"No methods found for class '{class_name}'."}]

        driver_usr_map = {r["usr"]: r for r in driver_methods}

        # Find all callers of those methods
        placeholders = ",".join("?" * len(driver_usr_map))
        rows = conn.execute(
            f"""SELECT r.from_usr, r.to_usr, r.from_file, r.from_line, r.ref_kind,
                       caller.name AS caller_name,
                       caller.qualified_name AS caller_qname,
                       caller.kind AS caller_kind
                FROM refs r
                LEFT JOIN symbols caller
                  ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                WHERE r.config_hash = ?
                  AND r.to_usr IN ({placeholders})
                  AND r.ref_kind IN ('call', 'indirect')
                ORDER BY caller.qualified_name, r.from_line
                LIMIT ?""",
            (config_hash, *driver_usr_map.keys(), limit),
        ).fetchall()

        if not rows:
            return [{"info": f"No callers found for methods of '{class_name}'."}]

        # Group by wrapper class
        wrapped: dict[str, dict] = {}
        for r in rows:
            caller_qn = r["caller_qname"] or r["caller_name"] or "?"
            # Extract class from qualified name: "zbox::ZMODEM::start" → "zbox::ZMODEM"
            if "::" in caller_qn:
                wrapper_class = caller_qn.rsplit("::", 1)[0]
            else:
                wrapper_class = "(global)"
            if wrapper_class not in wrapped:
                wrapped[wrapper_class] = {"class": wrapper_class, "methods": {}, "_file": r["from_file"]}
            cm = wrapped[wrapper_class]["methods"]
            if caller_qn not in cm:
                cm[caller_qn] = {
                    "method": r["caller_name"],
                    "qualified_name": caller_qn,
                    "kind": r["caller_kind"],
                    "calls": [],
                }
            target = driver_usr_map.get(r["to_usr"])
            if target:
                cm[caller_qn]["calls"].append({
                    "driver_method": target["name"],
                    "line": r["from_line"],
                })

        # Flatten for output
        result = []
        for wc in sorted(wrapped.keys()):
            entry = wrapped[wc]
            result.append({
                "wrapper_class": wc,
                "method_count": len(entry["methods"]),
                "methods": sorted(entry["methods"].values(), key=lambda m: m["qualified_name"]),
            })
        return result
    finally:
        conn.close()


@mcp.tool()
def trace_data_flow(
    type_name: Annotated[str, Field(description="Type name to trace. E.g. 'SensorData' or 'Config::SensorData'.")],
    to_symbol: Annotated[str, Field(description="Target symbol name. E.g. 'uart_send' or 'UART_DRIVER::send'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum call path depth (default 8).")] = 8,
    limit: Annotated[int, Field(description="Maximum source functions to trace (default 15).")] = 15,
) -> list[dict]:
    """Read-only. Experimental. Trace how data of a given type flows to a target function.

    Finds functions whose signature mentions *type_name*, then looks for call
    paths from those functions to *to_symbol*.  Returns an approximate data
    flow map — useful for understanding how a data structure travels through
    the system before reaching its destination.

    **Experimental.**  Does not resolve type transformations (e.g. CBOR
    encoding, serialization, void-pointer casts).  Best used as a starting
    point, then verify specific paths with ``find_call_path``.  For exact
    call-graph queries without type tracking, use the ``find_*`` family.
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        # Resolve target USR
        target = conn.execute(
            """SELECT usr, name FROM symbols
               WHERE config_hash = ? AND (name = ? OR qualified_name = ?)
               ORDER BY is_definition DESC LIMIT 1""",
            (config_hash, to_symbol, to_symbol),
        ).fetchone()
        if not target:
            return [{"info": f"Target symbol '{to_symbol}' not found."}]

        # Find functions mentioning type_name in their signature (ranked by
        # caller count so the most "active" data handlers are shown first)
        sources = conn.execute(
            """SELECT s.name, s.qualified_name, s.kind, s.file_path, s.line,
                      s.signature, s.usr,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.to_usr = s.usr AND r.config_hash = s.config_hash
                         AND r.ref_kind IN ('call', 'indirect')) AS caller_count
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.signature LIKE ?
               ORDER BY caller_count DESC
               LIMIT ?""",
            (config_hash, f"%{type_name}%", limit),
        ).fetchall()

        if not sources:
            return [{"info": f"No functions found with '{type_name}' in their signature."}]

        # Try call paths from each source to target
        results = []
        for src in sources:
            paths = index_db.find_call_path(
                conn, config_hash, src["qualified_name"], to_symbol, max_depth=max_depth,
            )
            entry = {
                "source_name": src["name"],
                "source_qualified_name": src["qualified_name"],
                "source_kind": src["kind"],
                "source_file": abs_path(root, src["file_path"]),
                "source_line": src["line"],
                "caller_count": src["caller_count"],
            }
            if paths:
                entry["reachable"] = True
                entry["paths"] = paths[:3]
            else:
                entry["reachable"] = False
            results.append(entry)

        num_reachable = sum(1 for r in results if r["reachable"])
        return [
            {
                "_summary": f"{num_reachable}/{len(results)} source functions reach '{to_symbol}' within depth {max_depth}",
                "_type": type_name,
                "_target": to_symbol,
                "_experimental": True,
            },
            *results,
        ]
    finally:
        conn.close()


@mcp.tool()
def find_hotspots(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Number of top-called functions to return (default 20).")] = 20,
) -> list[dict]:
    """Read-only. Find the most-called functions ranked by caller count.

    Use for high-level impact assessment: changing a hotspot affects many
    call sites.  The result tells you which functions carry the most
    "architectural weight" — good targets for refactoring, optimization,
    or extra testing.

    Requires the reference index (``fw-context index`` — refs on by default).
    For the callers of a specific hotspot, follow up with ``find_callers``
    or ``find_all_callers_recursive``.
    """
    root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    try:
        rows = index_db.find_hotspots(conn, config_hash, limit=limit)
        if not rows:
            return [{"info": "No references indexed — enable index_refs and re-index."}]
        return rows
    finally:
        conn.close()


# ── Pipeline-based search tools ─────────────────────────────────────────────


@mcp.tool()
def search_code(
    query: Annotated[str, Field(description="FTS5 search terms. 1-3 words, omit underscores. E.g. 'modem init' not 'modem_init'. Supports trailing wildcard 'modem*'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    kind: Annotated[str | None, Field(description="Optional kind filter: function, method, class, struct, enum, typedef, variable, field, namespace.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
) -> list[dict]:
    """Read-only. Full-text search over indexed symbols (functions, classes, methods, enums, etc.).

    Use when looking for symbols by topic or keyword rather than exact name.
    Prefer ``lookup_symbol`` when you already know the symbol name.

    **FTS5 syntax:**
    - ``init*`` matches init, init_uart, initialize (trailing wildcard)
    - ``"spi init"`` matches the exact phrase "spi init"
    - Do NOT use underscore in queries — ``modem_init`` is split into
      ``modem AND init``. Use ``modem init`` instead.

    **Kind filter values:** ``function``, ``method``, ``constructor``,
    ``destructor``, ``class``, ``struct``, ``enum``, ``enum_constant``,
    ``typedef``, ``variable``, ``field``, ``namespace``.

    Args:
        query: Search term(s) with FTS5 syntax. Keep queries short — 1–3 words.
        project_root: Absolute path to the project. Defaults to nearest git root.
        kind: Optional filter to return only symbols of this kind.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        is_definition, signature, docstring. Enum constants include
        ``enum_value`` with the integer value.
    """
    try:
        root = resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        limit = min(limit, 100)

        def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            rows = search_symbols(
                c, query, config_hash, limit=limit, kind=kind,
                exclude_variables=(kind is None),
            )
            # Fallback: when FTS5 returns nothing, try prefix lookup on each
            # query term.  Handles cases like ``"socket state"`` where FTS5
            # tokenisation misses ``socket_state_t``.
            fallback_used = False
            if not rows:
                fallback_used = True
                terms = [t for t in query.split() if len(t) > 1]
                seen_usr: set[str] = set()
                fallback_rows: list = []
                for term in terms:
                    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    kind_filter = "AND s.kind = ?" if kind else ""
                    params = [config_hash, f"{esc}%", f"{esc}%"]
                    if kind:
                        params.append(kind)
                    params.append(limit * 2)
                    term_rows = c.execute(
                        rf"""SELECT s.* FROM symbols s
                           WHERE s.config_hash=?
                             AND (s.name LIKE ? ESCAPE '\' OR s.qualified_name LIKE ? ESCAPE '\')
                             {kind_filter}
                           ORDER BY s.is_definition DESC, s.line
                           LIMIT ?""",
                        params,
                    ).fetchall()
                    for r in term_rows:
                        if r["usr"] not in seen_usr:
                            seen_usr.add(r["usr"])
                            fallback_rows.append(r)
                rows = fallback_rows[:limit]

            def _fmt(r) -> dict:
                d = {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": abs_path(root, r["file_path"]),
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                }
                if r["enum_value"] is not None:
                    d["enum_value"] = r["enum_value"]
                if fallback_used:
                    d["_fallback"] = "lookup_symbol"
                return d

            return [_fmt(r) for r in rows]

        return _with_stale_recovery(root, db_path, _do_search)
    except Exception as e:
        log.exception("search_code failed: %s", e)
        return [{"error": f"search_code failed: {e}"}]


@mcp.tool()
async def smart_search(
    query: Annotated[str, Field(description="Natural language description, 5-15 words. E.g. 'how does the modem connect?' or 'handle BLE pairing failure'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 20, max 100).")] = 20,
) -> list[dict]:
    """Read-only. Natural-language search: Ollama generates FTS5 keywords, then searches the index. Slow (10-30 s).

    Multi-phase approach:
    1) Translate non-English queries
    2) Rough FTS5 search to gather sample symbols
    3) Ollama sees those samples + query and generates FTS5 terms
    4) Refine: Ollama checks results and course-corrects
    5) FTS5 search with generated terms
    6) Semantic embedding search (cosine similarity)
    7) Score, deduplicate, format

    **When to prefer over search_code:** When you don't know the exact keywords
    and want to describe what you're looking for ("how does the modem connect?",
    "handle BLE pairing failure").

    **Fallback:** When Ollama is unavailable, falls back to direct FTS5 search
    with word-split terms from the query.

    Args:
        query: Natural language description of what you're looking for.
               Be specific — 5–15 words works best.
        project_root: Absolute path to the project. Defaults to nearest git root.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts with metadata entries (_generated_queries, _rough_queries,
        _translated_from) followed by symbol results.
    """
    from fw_context_mcp.search import SMART_SEARCH
    from fw_context_mcp.search.context import PipelineContext
    from fw_context_mcp.search.pipeline import PipelineRunner

    try:
        ctx = PipelineContext.create(query=query, project_root=project_root, limit=limit)
    except ValueError as e:
        return [{"error": str(e)}]

    runner = PipelineRunner(SMART_SEARCH)
    ctx = await runner.run(ctx)

    # Add staleness warning if applicable
    results = list(ctx.formatted_results)
    if ctx.ollama_warning is None:
        conn, err = _open_db_safe(ctx.db_path)
        if err:
            return [err]
        try:
            with conn:
                cfg_data = get_active_config(conn, derive_project_id(ctx.project_root))
                if cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"]):
                    results.append({
                        "warning": "Index may be stale — compile_commands.json changed since last index.",
                        "hint": "Call reindex_file() on modified files or run 'fw-context index' to update.",
                    })
        finally:
            conn.close()
    return results


# ── MCP Resources ──────────────────────────────────────────────────────────


@mcp.resource("fw-context://stats")
def resource_stats() -> str:
    """Return a human-readable summary of all indexed projects."""
    import json

    projects = list_projects()
    if not projects:
        return "No indexed projects found."
    lines = [f"# fw-context — {len(projects)} project(s)", ""]
    for p in projects:
        if "error" in p:
            lines.append(f"- **{p.get('db', '?')}**: ERROR — {p['error']}")
            continue
        stale = "⚠ stale" if p.get("stale") else "✓ fresh"
        lines.append(
            f"- **{p['name']}** ({p['project_id']}) — "
            f"{p['symbol_count']} symbols, {p['file_count']} files, "
            f"indexed {p['indexed_at']}, {stale}"
        )
    return "\n".join(lines)


@mcp.resource("fw-context://projects")
def resource_projects() -> str:
    """Return project list as JSON."""
    import json
    return json.dumps(list_projects(), indent=2, ensure_ascii=False, default=str)


@mcp.resource("fw-context://symbols/{name}")
def resource_symbol(name: str) -> str:
    """Return the definition source of a symbol as a resource."""
    import json

    result = get_source(name)
    if "error" in result:
        return json.dumps(result)
    source = result.pop("source", "")
    # Render as a small markdown document
    lines = [
        f"# {result['name']}",
        "",
        f"- **qualified:** `{result['qualified_name']}`",
        f"- **kind:** {result['kind']}",
        f"- **file:** `{result['file']}:{result['line']}`",
        f"- **signature:** `{result.get('signature', '')}`",
        "",
        "```cpp",
        source,
        "```",
    ]
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────────────


def main() -> None:
    mcp.run()
