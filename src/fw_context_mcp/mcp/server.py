"""fw-context MCP server — build-aware code intelligence for embedded projects.

Search tools (search_code, smart_search) delegate to the search pipeline
(fw_context_mcp.search).  Everything else stays in this file as thin
handlers with shared helpers for DB access, staleness detection, etc.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import Config, derive_project_id
from ..config import load as load_config
from ..indexer.compile_commands import parse as parse_cc
from ..indexer.db import (
    DatabaseCorruptionError,
    count_refs,
    find_refs,
    get_active_config,
    get_all_projects,
    get_file_mtime_indexed,
    open_db,
    search_symbols,
    transaction,
)
from ..llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async, check_setup

log = logging.getLogger(__name__)

# ── MCP server instance ─────────────────────────────────────────────────────

mcp = FastMCP(
    "fw-context",
    instructions="Build-aware code intelligence index for embedded firmware (Mbed OS, Zephyr).",
)

# ── Shared helpers ──────────────────────────────────────────────────────────


def _db_path(project_root: Path) -> Path:
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _resolve_context(project_root: str | None) -> tuple[Path, Config, str, Path]:
    root = _resolve_project_root(project_root)
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


def _resolve_project_root(project_root: str | None) -> Path:
    if project_root:
        return Path(project_root).resolve()
    cwd = Path(os.getcwd())
    p = cwd
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return cwd


def _abs_path(root: Path, path: str) -> str:
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(root / p)


def _is_stale(cfg, compile_commands_path: str) -> bool:
    try:
        cc_mtime = os.path.getmtime(compile_commands_path)
        indexed_at = datetime.fromisoformat(cfg["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + 1
    except Exception:
        return False


def _lookup_definition(conn, config_hash: str, name: str):
    BASE_QUERY = """SELECT s.* FROM symbols s
       WHERE s.config_hash=? AND %s
       ORDER BY %s (CASE WHEN s.file_path LIKE '%%mbed-os%%' THEN 1 ELSE 0 END), s.line
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
    return None


def _read_symbol_body(file_path: str, line_no: int, end_line: int = 0, max_lines: int = 400) -> str:
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    start = line_no - 1
    if start < 0 or start >= len(lines):
        return ""
    if end_line and end_line >= line_no:
        end = min(len(lines) - 1, end_line - 1)
        return "\n".join(f"{i + 1:4d}  {lines[i]}" for i in range(start, end + 1))
    depth = 0
    seen_open = False
    end = start
    for i in range(start, min(len(lines), start + max_lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                seen_open = True
            elif ch == "}":
                depth -= 1
        end = i
        if seen_open and depth <= 0:
            break
    if not seen_open:
        end = min(len(lines) - 1, start + 2)
    return "\n".join(f"{i + 1:4d}  {lines[i]}" for i in range(start, end + 1))


def _stale_files(conn, config_hash: str, file_paths: list[str]) -> list[str]:
    stale = []
    for path in dict.fromkeys(file_paths):
        try:
            stored = get_file_mtime_indexed(conn, config_hash, path)
            if stored is None:
                continue
            if os.path.getmtime(path) > stored + 1:
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
            continue
        p = Path(path)
        if not p.is_absolute():
            p = (root / path).resolve()
        try:
            if p.stat().st_mtime > stored + 1:
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
def get_active_build(project_root: str | None = None) -> dict:
    """Return metadata about the most recently indexed build configuration."""
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
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
        return {
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
            "stale": _is_stale(cfg, cfg["compile_commands_path"]) or modified_count > 0,
        }


@mcp.tool()
def lookup_symbol(
    name: str,
    project_root: str | None = None,
    exact: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Look up a symbol by name. Returns all matches — declarations and definitions."""
    try:
        root = _resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]
        conn, err = _open_db_safe(db_path)
        if err:
            return [err]

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
            return [
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": _abs_path(root, r["file_path"]),
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                }
                for r in rows
            ]

        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]
            config_hash = cfg["config_hash"]
            limit = min(limit, 100)
            result_rows = _do_lookup(conn, config_hash)
            stale_f = _stale_files(conn, config_hash, [_abs_path(root, r["file"]) for r in result_rows])

        results: list[dict] = []
        if _is_stale(cfg, cfg["compile_commands_path"]):
            results.append({"warning": "Index may be stale — compile_commands.json changed since last index. Run 'fw-context index' to update."})
        if stale_f:
            succeeded, failed = _auto_reindex_stale(stale_f, root)
            if succeeded:
                conn2, err2 = _open_db_safe(db_path)
                if err2:
                    results.append({"warning": f"Auto-reindex partially succeeded ({len(succeeded)} files), but DB is now corrupt."})
                    results += result_rows
                    return results
                with conn2:
                    result_rows = _do_lookup(conn2, config_hash)
            if failed:
                results.append({"warning": f"Auto-reindex failed for {len(failed)} file(s): {', '.join(failed[:3])}. Run 'fw-context index' manually."})
        results += result_rows
        return results
    except Exception as e:
        log.exception("lookup_symbol failed: %s", e)
        return [{"error": f"lookup_symbol failed: {e}"}]


@mcp.tool()
def list_projects(project_root: str | None = None) -> list[dict]:
    """List all indexed firmware projects with their status."""
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
            with conn:
                rows = get_all_projects(conn)
            for r in rows:
                stale = _is_stale(
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
                    "stale": stale,
                    "db": str(db_path),
                })
        except Exception as e:
            results.append({"db": str(db_path), "error": str(e)})
    return results


@mcp.tool()
def reset_index(project_root: str | None = None, confirm: bool = False) -> dict:
    """Delete the symbol index for a project so it can be re-indexed from scratch."""
    root = _resolve_project_root(project_root)
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
    file_path: str,
    project_root: str | None = None,
) -> dict:
    """Re-parse a single source file and update its symbols in the index."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
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
    result = {
        "file": str(target),
        "translation_units": len(matching),
        "symbols_updated": total_symbols,
        "elapsed_s": elapsed,
    }
    if len(matching) == 1 and target.suffix.lower() in {".h", ".hpp"}:
        result["warning"] = "Header re-indexed via one TU. Other TUs including this header may still have stale symbols — run 'fw-context index' for full accuracy."
    return result


@mcp.tool()
def check_ollama(project_root: str | None = None) -> dict:
    """Check whether Ollama is running and the configured model is installed."""
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
    name: str,
    project_root: str | None = None,
    context_lines: int = 40,
) -> dict:
    """Look up a symbol and explain what it does."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    with conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}
        config_hash = cfg_data["config_hash"]
        row = _lookup_definition(conn, config_hash, name)
        if not row:
            return {"error": f"Symbol not found: {name}"}
        file_path = _abs_path(root, row["file_path"])
        line_no = row["line"]
        signature = row["signature"] or ""
        kind = row["kind"]
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
def get_source(name: str, project_root: str | None = None) -> dict:
    """Return the source code of a symbol's definition — no LLM, fast."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}
    conn, err = _open_db_safe(db_path)
    if err:
        return err
    with conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}
        row = _lookup_definition(conn, cfg_data["config_hash"], name)
        if not row:
            return {"error": f"Symbol not found: {name}"}
        file_path = _abs_path(root, row["file_path"])
        result = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "file": file_path,
            "line": row["line"],
            "signature": row["signature"] or "",
            "is_definition": bool(row["is_definition"]),
        }
    source = _read_symbol_body(file_path, row["line"], end_line=row["end_line"] or 0)
    if not source:
        result["warning"] = f"Could not read source from {file_path}"
    else:
        result["source"] = source[:8000] if len(source) > 8000 else source
    return result


def _references_result(name: str, project_root: str | None, ref_kind: str | None, limit: int) -> list[dict]:
    """Shared logic for find_callers / find_references."""
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    with conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return [{"error": "No build config indexed."}]
        config_hash = cfg_data["config_hash"]
        if count_refs(conn, config_hash) == 0:
            return [{"info": (
                "No references indexed. The cross-reference graph is opt-in — "
                "enable it with [index] index_refs = true (or run "
                "'fw-context index --refs') and re-index the project."
            )}]
        limit = min(limit, 200)
        rows = find_refs(conn, config_hash, name, ref_kind=ref_kind, limit=limit)
        if not rows:
            label = "callers" if ref_kind == "call" else "references"
            return [{"info": f"No {label} found for '{name}'. Check the name (exact match) and that the index is current."}]
        return [
            {
                "file": _abs_path(root, r["from_file"]),
                "line": r["from_line"],
                "ref_kind": r["ref_kind"],
                "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
                "caller_kind": r["caller_kind"],
            }
            for r in rows
        ]


@mcp.tool()
def find_callers(name: str, project_root: str | None = None, limit: int = 50) -> list[dict]:
    """Find call sites of a function/method — who calls ``name``."""
    return _references_result(name, project_root, ref_kind="call", limit=limit)


@mcp.tool()
def find_references(name: str, project_root: str | None = None, limit: int = 50) -> list[dict]:
    """Find all references to a symbol — calls, reads, and member accesses."""
    return _references_result(name, project_root, ref_kind=None, limit=limit)


# ── Pipeline-based search tools ─────────────────────────────────────────────


@mcp.tool()
def search_code(
    query: str,
    project_root: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Full-text search over indexed symbols (functions, classes, methods, enums, etc.).

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
        is_definition, signature, docstring.
    """
    try:
        root = _resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        conn, err = _open_db_safe(db_path)
        if err:
            return [err]

        def _do_search(c: sqlite3.Connection, config_hash: str) -> list[dict]:
            rows = search_symbols(c, query, config_hash, limit=limit, kind=kind)
            return [
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": _abs_path(root, r["file_path"]),
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                }
                for r in rows
            ]

        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]
            config_hash = cfg["config_hash"]
            limit = min(limit, 100)
            result_rows = _do_search(conn, config_hash)
            stale_f = _stale_files(conn, config_hash, [_abs_path(root, r["file"]) for r in result_rows])

        results: list[dict] = []
        if _is_stale(cfg, cfg["compile_commands_path"]):
            results.append({"warning": "Index may be stale — compile_commands.json changed since last index. Run 'fw-context index' to update."})
        if stale_f:
            succeeded, failed = _auto_reindex_stale(stale_f, root)
            if succeeded:
                conn2, err2 = _open_db_safe(db_path)
                if err2:
                    results.append({"warning": f"Auto-reindex partially succeeded ({len(succeeded)} files), but DB is now corrupt. Run reset_index() + 'fw-context index'."})
                    results += result_rows
                    return results
                with conn2:
                    result_rows = _do_search(conn2, config_hash)
            if failed:
                results.append({"warning": f"Auto-reindex failed for {len(failed)} file(s): {', '.join(failed[:3])}. Run 'fw-context index' manually."})
        results += result_rows
        return results
    except Exception as e:
        log.exception("search_code failed: %s", e)
        return [{"error": f"search_code failed: {e}"}]


@mcp.tool()
async def smart_search(
    query: str,
    project_root: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Natural-language search: Ollama generates FTS5 keywords, then searches the index.

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
        with conn:
            cfg_data = get_active_config(conn, derive_project_id(ctx.project_root))
            if cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"]):
                results.append({
                    "warning": "Index may be stale — compile_commands.json changed since last index.",
                    "hint": "Call reindex_file() on modified files or run 'fw-context index' to update.",
                })
    return results


def main() -> None:
    mcp.run()
