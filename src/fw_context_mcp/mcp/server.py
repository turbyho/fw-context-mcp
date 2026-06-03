"""fw-context MCP server — build-aware code intelligence for embedded projects."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import Config, derive_project_id
from ..config import load as load_config
from ..indexer.compile_commands import parse as parse_cc
from ..indexer.db import (
    delete_symbols_for_file,
    get_active_config,
    get_all_projects,
    get_file_mtime_indexed,
    get_file_mtimes,
    insert_symbols_batch,
    open_db,
    search_symbols,
    split_tokens,
    transaction,
    upsert_file,
)
from ..llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async, check_setup

log = logging.getLogger(__name__)

# Kind weights for search scoring — module-level constant, not per-call allocation.
# Prefer callable/declarative symbols over data storage (variables/fields).
_KIND_WEIGHT: dict[str, int] = {
    "function": 2, "method": 2, "constructor": 2, "destructor": 2,
    "class": 2, "struct": 2, "enum": 2, "typedef": 2,
    "enum_constant": 1, "namespace": 1,
    "variable": 0, "field": 0,
}

mcp = FastMCP(
    "fw-context",
    instructions="Build-aware code intelligence index for embedded firmware (Mbed OS, Zephyr).",
)

def _db_path(project_root: Path) -> Path:
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _resolve_context(project_root: str | None) -> tuple[Path, Config, str, Path]:
    """Return (db_path, config, project_id, root) in one config load.

    Use when a tool needs both the config (LLM settings, source roots) and
    the DB path — avoids loading config twice.
    """
    root = _resolve_project_root(project_root)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    return cfg.index.db_dir / project_id / "index.db", cfg, project_id, root


def _resolve_project_root(project_root: str | None) -> Path:
    if project_root:
        return Path(project_root).resolve()
    cwd = Path(os.getcwd())
    # Walk up to find a git repo root
    p = cwd
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return cwd


def _abs_path(root: Path, path: str) -> str:
    """Resolve a stored file_path to an absolute path for tool output.

    symbols.file_path is stored relative to the project root (for FTS5 module
    tokenisation). Tool consumers need absolute paths to open / click them, so
    we join with root here. Already-absolute paths are returned unchanged.
    """
    if not path:
        return path
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(root / p)


def _is_stale(cfg, compile_commands_path: str) -> bool:
    """Return True if compile_commands.json is newer than the indexed timestamp."""
    try:
        cc_mtime = os.path.getmtime(compile_commands_path)
        indexed_at = datetime.fromisoformat(cfg["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + 1
    except Exception:
        return False


def _lookup_definition(conn, config_hash: str, name: str):
    """Return the best symbol row for a name: definition preferred, project code
    (src/, lib/ outside mbed-os) preferred over framework code. Returns None if
    the name is not indexed.
    """
    # Prefer a definition; tie-break toward project-local files over mbed-os.
    row = conn.execute(
        """SELECT s.* FROM symbols s
           WHERE s.config_hash=? AND s.name=? AND s.is_definition=1
           ORDER BY (CASE WHEN s.file_path LIKE '%mbed-os%' THEN 1 ELSE 0 END), s.line
           LIMIT 1""",
        (config_hash, name),
    ).fetchone()
    if not row:
        row = conn.execute(
            """SELECT s.* FROM symbols s
               WHERE s.config_hash=? AND s.name=?
               ORDER BY s.is_definition DESC,
                        (CASE WHEN s.file_path LIKE '%mbed-os%' THEN 1 ELSE 0 END), s.line
               LIMIT 1""",
            (config_hash, name),
        ).fetchone()
    return row


def _read_symbol_body(file_path: str, line_no: int, max_lines: int = 400) -> str:
    """Read a symbol's source from its definition line, balancing braces.

    Scans forward from the definition line counting { and } until the body is
    balanced, returning numbered source lines for the full definition. Falls
    back to a small window when there is no brace body (declaration, field,
    enum constant). Brace counting is best-effort — braces inside string/char
    literals or comments may skew the end on pathological inputs.
    """
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    start = line_no - 1  # 0-indexed
    if start < 0 or start >= len(lines):
        return ""

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
        # No body braces — declaration/field/enum constant: return a small window.
        end = min(len(lines) - 1, start + 2)

    return "\n".join(f"{i + 1:4d}  {lines[i]}" for i in range(start, end + 1))


def _stale_files(conn, config_hash: str, file_paths: list[str]) -> list[str]:
    """Return subset of file_paths whose on-disk mtime is newer than stored mtime."""
    stale = []
    for path in dict.fromkeys(file_paths):  # deduplicate, preserve order
        try:
            stored = get_file_mtime_indexed(conn, config_hash, path)
            if stored is None:
                continue
            if os.path.getmtime(path) > stored + 1:
                stale.append(path)
        except OSError:
            pass
    return stale


def _detect_build_system(root: Path) -> str:
    if (root / "mbed-os").is_dir() or (root / "mbed_app.json").exists():
        return "mbed-os"
    if (root / "west.yml").exists() or (root / "prj.conf").exists():
        return "zephyr"
    if (root / "platformio.ini").exists():
        return "platformio"
    return "unknown"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_active_build(project_root: str | None = None) -> dict:
    """Return metadata about the most recently indexed build configuration.

    Call this at the start of every session to confirm the index exists and is
    not stale. If ``stale`` is true, remind the user to run ``fw-context index``
    before relying on search results.

    Args:
        project_root: Absolute path to the project. Defaults to nearest git root.

    Returns:
        dict with keys:
        - config_hash (str): identity of the indexed build config
        - project_id (str): stable hex id derived from git remote URL
        - project_root (str): absolute path to the project
        - build_system (str): one of "mbed-os", "zephyr", "platformio", "unknown"
        - compile_commands (str): path to the compile_commands.json used
        - indexed_at (str): ISO-8601 timestamp of when the index was built
        - symbol_count (int): total symbols in the index
        - file_count (int): total files indexed
        - stale (bool): True when compile_commands.json is newer than the index
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    with open_db(db_path) as conn:
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

        return {
            "config_hash": config_hash,
            "project_id": project_id,
            "project_root": str(root),
            "build_system": _detect_build_system(root),
            "compile_commands": cfg["compile_commands_path"],
            "indexed_at": cfg["created_at"],
            "symbol_count": sym_count,
            "file_count": file_count,
            "stale": _is_stale(cfg, cfg["compile_commands_path"]),
        }


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
        is_definition, signature, docstring. May include a ``warning`` entry
        when the index is stale or individual files were modified.

    Example:
        ``search_code("modem init", kind="method", limit=5)``
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    with open_db(db_path) as conn:
        project_id = derive_project_id(root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return [{"error": "No build config indexed."}]

        limit = min(limit, 100)
        rows = search_symbols(conn, query, cfg["config_hash"], limit=limit)

        if kind:
            rows = [r for r in rows if r["kind"] == kind]

        results: list[dict] = []
        if _is_stale(cfg, cfg["compile_commands_path"]):
            results.append({"warning": "Index may be stale — compile_commands.json changed since last index. Run 'fw-context index' to update."})

        result_rows = [
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
        # _stale_files queries files.path (absolute) — pass absolute paths so the
        # mtime lookup matches (relative file_path would silently never match).
        stale_f = _stale_files(conn, cfg["config_hash"], [_abs_path(root, r["file_path"]) for r in rows])
        if stale_f:
            results.append({"warning": f"File(s) modified since last index — results may be outdated. Run 'fw-context index' or reindex_file(): {stale_f}"})
        results += result_rows
        return results


@mcp.tool()
def lookup_symbol(
    name: str,
    project_root: str | None = None,
    exact: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Look up a symbol by name. Returns all matches — declarations and definitions.

    Use when you know the exact or partial name of a symbol and want its
    location, signature, and whether it's a definition or just a declaration.
    For keyword-based search, use ``search_code`` instead.

    When ``exact=False`` (the default), the lookup uses prefix matching —
    ``lookup_symbol("Box")`` returns ``BoxManager``, ``BoxManager::init``, etc.
    When ``exact=True``, only symbols whose name is exactly ``name`` are returned.

    Args:
        name: Symbol name (case-sensitive). Use prefix match by default.
        project_root: Absolute path to the project. Defaults to nearest git root.
        exact: If True, match exact name only. If False, also match as prefix.
        limit: Maximum number of results (default 50, max 100).

    Returns:
        list of dicts with: name, qualified_name, kind, file, line,
        is_definition, signature, docstring. Definitions are sorted first.
        May include a ``warning`` when the index is stale.

    Example:
        ``lookup_symbol("BoxManager", exact=True)`` → constructor + class
    """
    try:
        root = _resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]

        with open_db(db_path) as conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return [{"error": "No build config indexed."}]

            config_hash = cfg["config_hash"]
            limit = min(limit, 100)
            if exact:
                rows = conn.execute(
                    """SELECT s.* FROM symbols s
                       WHERE s.config_hash=? AND s.name=?
                       ORDER BY s.is_definition DESC, s.line
                       LIMIT ?""",
                    (config_hash, name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT s.* FROM symbols s
                       WHERE s.config_hash=? AND s.name LIKE ?
                       ORDER BY s.is_definition DESC, s.line
                       LIMIT ?""",
                    (config_hash, f"{name}%", limit),
                ).fetchall()

            results: list[dict] = []
            if _is_stale(cfg, cfg["compile_commands_path"]):
                results.append({"warning": "Index may be stale — compile_commands.json changed since last index. Run 'fw-context index' to update."})

            result_rows = [
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
            stale_f = _stale_files(conn, cfg["config_hash"], [_abs_path(root, r["file_path"]) for r in rows])
            if stale_f:
                results.append({"warning": f"File(s) modified since last index — results may be outdated. Run 'fw-context index' or reindex_file(): {stale_f}"})
            results += result_rows
            return results
    except Exception as e:
        log.exception("lookup_symbol failed: %s", e)
        return [{"error": f"lookup_symbol failed: {e}"}]


@mcp.tool()
def list_projects(project_root: str | None = None) -> list[dict]:
    """List all indexed firmware projects with their status.

    Use to discover which projects are available, check their index health
    (symbol count, staleness), and find the project_id for operations.

    Args:
        project_root: If given, returns only the project matching this path.
                      Otherwise scans all db files under ``~/.fw-context/index/``.

    Returns:
        list of dicts, each with: project_id, name, root_path, build_system,
        symbol_count, file_count, indexed_at, stale, db (path to index file).
    """
    cfg = load_config(project_root=Path(project_root).resolve() if project_root else None)
    index_dir = cfg.index.db_dir

    db_files = list(index_dir.glob("*/index.db")) if index_dir.exists() else []
    if not db_files:
        return [{"info": f"No indexed projects found under {index_dir}."}]

    results: list[dict] = []
    for db_path in sorted(db_files):
        try:
            with open_db(db_path) as conn:
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
    """Delete the symbol index for a project so it can be re-indexed from scratch.

    Use after a toolchain change, compiler upgrade, or when the index is corrupt.
    **Always call without confirm first** (dry-run) to see what would be deleted,
    then call with ``confirm=True`` to proceed. After reset, remind the user to
    run ``fw-context index`` to rebuild.

    Args:
        project_root: Absolute path to the project. Defaults to nearest git root.
        confirm: Must be True to actually delete. When False (default), returns
                 what would be deleted without taking any action (dry-run).

    Returns:
        dict with: project_root, db (path), project_id, symbol_count,
        indexed_at, action ("dry_run" or "deleted"), message.
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)

    if not db_path.exists():
        return {"error": f"No index found for {root}. Nothing to reset."}

    project_id = derive_project_id(root)
    cfg_data = None
    sym_count = 0
    with open_db(db_path) as conn:
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

    if not confirm:
        info["action"] = "dry_run"
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
    info["message"] = (
        f"Index deleted. Run 'fw-context index' in {root} to rebuild."
    )
    return info


@mcp.tool()
def reindex_file(
    file_path: str,
    project_root: str | None = None,
) -> dict:
    """Re-parse a single source file and update its symbols in the index.

    Use after editing a .c/.cpp file to keep the index current without running
    a full ``fw-context index``. The tool finds the translation unit in
    compile_commands.json that corresponds to the given file, re-parses it with
    libclang, and replaces the old symbols atomically.

    **Limitations:**
    - Only processes one translation unit per matching entry. If a header appears
      in multiple TUs, only the first matching TU is used — other TUs including
      that header may still have stale symbols. Run ``fw-context index`` for full
      accuracy.
    - The file must appear in compile_commands.json. Header-only files without
      a corresponding .c/.cpp entry cannot be re-indexed this way.

    Args:
        file_path: Absolute path to the source file to re-index (.c/.cpp only).
        project_root: Absolute path to the project. Defaults to nearest git root.

    Returns:
        dict with: file, translation_units (count), symbols_updated, elapsed_s.
        When re-indexing a header via a single TU, includes a warning that other
        TUs may be stale.
    """
    from ..indexer.symbols import extract  # lazy: requires libclang

    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    with open_db(db_path) as conn:
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

        for unit in matching:
            file_path_str = str(unit.file.resolve())
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0

            # Extract first — if this fails, skip TU without touching DB
            try:
                syms = list(extract(unit, source_roots=source_roots, exclude_paths=exclude_paths))
            except Exception as exc:
                log.warning("skip reindex TU %s: %s", unit.file.name, exc)
                continue

            with transaction(conn):
                existing = get_file_mtimes(conn, config_hash)
                if file_path_str in existing:
                    file_id_old, _ = existing[file_path_str]
                    delete_symbols_for_file(conn, file_id_old)

                file_id_cache: dict[str, int] = {}
                rows = []
                for s in syms:
                    sym_file = s.file
                    if sym_file not in file_id_cache:
                        lang = "cpp" if Path(sym_file).suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"} else "c"
                        file_id_cache[sym_file] = upsert_file(conn, config_hash, sym_file, lang, mtime=current_mtime if sym_file == file_path_str else 0.0)
                    try:
                        rel_path = str(Path(sym_file).resolve().relative_to(root))
                    except ValueError:
                        rel_path = sym_file
                    rows.append((
                        config_hash, file_id_cache[sym_file], rel_path,
                        split_tokens(s.name, s.qualified_name),
                        s.usr, s.name, s.qualified_name, s.kind, s.line, s.column,
                        int(s.is_definition), s.signature, s.docstring,
                    ))

                if rows:
                    total_symbols += insert_symbols_batch(conn, rows)

                upsert_file(conn, config_hash, file_path_str, unit.language, mtime=current_mtime)

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
    """Check whether Ollama is running and the configured model is installed.

    Call before the first ``explain_symbol`` or ``smart_search`` call in a session
    to verify the local LLM is available. These tools fall back to direct search
    when Ollama is unavailable, but checking first avoids confusing warnings.

    Args:
        project_root: Absolute path to the project. Defaults to nearest git root.

    Returns:
        dict with:
        - status: "ok" | "model_missing" | "error" | "disabled"
        - ollama_running (bool)
        - ollama_enabled (bool)
        - configured_model (str): the model name from config
        - num_ctx (int): context size setting
        - installed_models (list[str]): all models available in Ollama
        - message (str, when status != "ok"): human-readable description
        - available_code_models (list[str], when model missing): code-related
          models that are already installed and could be used instead
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
    name: str,
    project_root: str | None = None,
    context_lines: int = 40,
) -> dict:
    """Look up a symbol and explain what it does.

    When a local Ollama model is available, sends the symbol's source code
    context for a plain-English explanation (2–4 sentences).

    When Ollama is *not* available (no GPU, no cloud account), the result
    includes ``source`` (the source code snippet) and ``explain_prompt`` (the
    LLM prompt that would have been sent). The calling agent should use its
    own LLM to answer the prompt.

    **Performance:** With Ollama each call takes 10–30 seconds. Do NOT call
    in a loop over many symbols. Call ``check_ollama()`` first to verify the
    model is available.

    Args:
        name: Symbol name (exact match). If multiple symbols share the name,
              the definition is preferred over declarations.
        project_root: Absolute path to the project. Defaults to nearest git root.
        context_lines: Lines of source code to include above and below the
                       definition for context (default 40).

    Returns:
        dict with: name, kind, file, line, signature.
        With Ollama: + explanation (str).
        Without Ollama: + warning (str), source (str), explain_prompt (str).
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    with open_db(db_path) as conn:
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
    """Return the source code of a symbol's definition — no LLM, fast.

    Unlike ``explain_symbol`` (which calls Ollama and takes 10–30 s), this reads
    the definition's full body straight from disk by brace-matching from the
    definition line. Use it to read an implementation when you want the actual
    code, not a prose summary.

    The definition is preferred over declarations, and project-local code
    (``src/``, ``lib/``) is preferred over framework code (mbed-os) when a name
    exists in both (e.g. a Gap event-handler override in ``src/`` vs the mbed-os
    base class).

    Args:
        name: Symbol name (exact match).
        project_root: Absolute path to the project. Defaults to nearest git root.

    Returns:
        dict with: name, qualified_name, kind, file (absolute), line, signature,
        source (numbered lines of the full definition body). Error dict if the
        symbol is not indexed.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    with open_db(db_path) as conn:
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

    source = _read_symbol_body(file_path, row["line"])
    if not source:
        result["warning"] = f"Could not read source from {file_path}"
    else:
        result["source"] = source[:8000] if len(source) > 8000 else source
    return result


def _parse_search_terms(raw: str) -> list[str]:
    """Parse LLM response into FTS5 keyword search terms.

    Tries JSON array first, falls back to line-by-line regex.
    """
    import json

    terms: list[str] = []

    # Try JSON array: just look for the first '['...']' block in case model
    # wraps it in markdown or adds extra text.
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    terms.append(item.strip())
    except (ValueError, json.JSONDecodeError):
        pass

    # Fallback: line-by-line regex cleanup
    if not terms:
        for line in raw.splitlines():
            # Strip parenthetical comments
            line = re.sub(r"\s*\(.*\)\s*$", "", line)
            cleaned = re.sub(r"^[\s\d\.\-\*]+", "", line).strip().strip("`'\"*")
            if cleaned and not cleaned.startswith("#"):
                terms.append(cleaned)

    # Replace underscores with spaces for FTS5 tokenizer.
    # Also strip leading/trailing whitespace from each term — LLM sometimes
    # emits [" key*", " storage*"] with a leading space which breaks FTS5.
    result = []
    for t in terms:
        cleaned = t.replace("_", " ").strip()
        if cleaned:
            result.append(cleaned)
    return result


@mcp.tool()
async def smart_search(
    query: str,
    project_root: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Natural-language search: Ollama generates FTS5 keywords, then searches the index.

    Two-phase approach:
    1) Rough word-split search to gather real symbol names from the index.
    2) Ollama sees those symbols + the original query and generates 2-4 refined
       FTS5 terms that match the project's naming conventions.
    3) Final search with refined terms, merged and deduplicated.

    **When to prefer over search_code:** When you don't know the exact keywords
    and want to describe what you're looking for ("how does the modem connect?",
    "handle BLE pairing failure").

    **Fallback:** When Ollama is unavailable or the model is not installed,
    falls back to direct FTS5 search with word-split terms from the query.
    Results include ``_generated_queries`` and ``_rough_queries`` so you can
    see which terms were used in each phase.

    Args:
        query: Natural language description of what you're looking for.
               Be specific — 5–15 words works best.
        project_root: Absolute path to the project. Defaults to nearest git root.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts. The first entries are ``_generated_queries`` and
        ``_rough_queries``. Subsequent entries are symbol results in the same
        format as ``search_code``. May include a warning when Ollama is
        unavailable or the index is stale.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    # --- Phase 0: translate non-ASCII (e.g. Czech) query to English ---
    translated_from: str | None = None
    if cfg.llm.enabled and not query.isascii():
        translate_prompt = (
            "Translate the following text to English. "
            "Output only the translated text, nothing else.\n\n"
            f"{query}"
        )
        try:
            translated = await call_ollama_async(translate_prompt, cfg.llm)
            translated = translated.strip()
            if translated and translated.isascii():
                translated_from = query
                query = translated
        except Exception:
            pass  # keep original query on failure

    cfg_data = None
    config_hash = ""
    seen: dict[tuple, dict] = {}

    with open_db(db_path) as conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return [{"error": "No build config indexed."}]
        config_hash = cfg_data["config_hash"]
        limit = min(limit, 100)

        def _fmt(r):
            return {
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file": _abs_path(root, r["file_path"]),
                "line": r["line"],
                "is_definition": bool(r["is_definition"]),
                "signature": r["signature"],
                "docstring": r["docstring"],
            }

        # --- Phase 1: rough search to gather real symbol names for Ollama ---
        STOP_WORDS = frozenset({
            "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "shall", "can", "to", "of", "in", "for",
            "on", "with", "at", "by", "from", "and", "or", "not", "but", "if",
            "then", "else", "when", "up", "down", "out", "off", "over", "under",
            "again", "how", "what", "where", "which", "who", "whom", "why",
            "handle", "handler", "using", "that", "this", "it", "its",
        })
        raw_words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
        content_words = [w for w in raw_words if w not in STOP_WORDS and len(w) > 1]

        rough_terms = content_words if content_words else [query]
        rough_samples: list[dict] = []
        rough_seen_names: set[str] = set()

        def _is_noise(name: str) -> bool:
            """Filter out names with special chars that carry no naming convention signal."""
            if set(name) & {"(", ")", "~", "=", "<", ">", "[", "]"}:
                return True
            if len(name) <= 2:
                return True
            return False

        # Phase 1a: phrase searches — combine first word with each other
        # word to find symbols matching multiple query terms (more relevant).
        if len(content_words) >= 2:
            for w in content_words[1:]:
                phrase = f'"{content_words[0]} {w}"'
                try:
                    rows = search_symbols(conn, phrase, config_hash, limit=5)
                except Exception:
                    continue
                for r in rows:
                    name = r["name"]
                    if _is_noise(name) or name in rough_seen_names:
                        continue
                    rough_seen_names.add(name)
                    rough_samples.append(r)
                if len(rough_samples) >= 20:
                    break

        # Phase 1b: individual word searches to fill remaining slots.
        if len(rough_samples) < 20:
            for word in rough_terms:
                try:
                    rows = search_symbols(conn, word, config_hash, limit=8)
                except Exception:
                    continue
                for r in rows:
                    name = r["name"]
                    if _is_noise(name) or name in rough_seen_names:
                        continue
                    rough_seen_names.add(name)
                    rough_samples.append(r)
                if len(rough_samples) >= 20:
                    break

        # --- Phase 2: Ollama generates refined terms using real symbol names ---
        keyword_queries: list[str] = []
        ollama_warning: dict | None = None

        if rough_samples:
            context_lines = [
                f"  {r['name']} ({r['kind']})" for r in rough_samples[:15]
            ]
            context_str = "\n".join(context_lines)

            prompt = (
                "You are a C/C++ code search assistant for an embedded firmware project.\n"
                "Generate 3-5 FTS5 keyword search terms based on the description below.\n"
                "Study the real symbol names to learn the project's naming conventions\n"
                "(prefixes, word order, abbreviations). Match them.\n\n"
                "CRITICAL: Embedded code uses camelCase AND snake_case. The FTS5 tokenizer\n"
                "treats each camelCase word as ONE token — e.g. \"onConnectionComplete\" is\n"
                "a single token, NOT split into \"on\", \"Connection\", \"Complete\".\n"
                "Therefore your queries MUST match token BEGINNINGS (prefixes):\n"
                "- For \"connect\" → generate \"connection*\" (prefix of \"ConnectionComplete\")\n"
                "- For \"init\" → generate \"init*\" (prefix of \"Init\", \"Initialize\")\n"
                "- Generate BOTH snake_case AND camelCase prefix variants\n\n"
                f"Real symbols from this project:\n{context_str}\n\n"
                "Return a JSON array of strings, e.g.: [\"ble_conn*\", \"connection*\", \"on_connect*\"]\n"
                "Rules:\n"
                "- Output MUST be a valid JSON array and nothing else\n"
                "- Mix snake_case and camelCase prefixes: \"modem_init*\", \"Connection*\", \"on_conn*\"\n"
                "- STRONGLY prefer short stems with a trailing wildcard over exact full names\n"
                "- You may use a trailing wildcard: \"ble_gap*\" matches ble_gap_init, etc.\n"
                "- No asterisks anywhere else — FTS5 only supports trailing wildcards\n"
                "- Cover different naming patterns found in the context — e.g. if\n"
                "  the project has \"ble_*\", \"on_*\", and \"connection_*\" prefixes,\n"
                "  generate one stem per pattern\n"
                "- For each concept in the description, generate the MOST LIKELY prefix\n"
                "  that matches real symbol beginnings (not substrings!)\n\n"
                f"Description: {query}\n"
            )
        else:
            prompt = (
                "You are a C/C++ code search assistant for an embedded firmware project.\n"
                "Generate 3-5 FTS5 keyword search terms based on the description below.\n"
                "Return a JSON array of strings, e.g.: [\"modem_init*\", \"connection*\", \"on_connect*\"]\n"
                "Rules:\n"
                "- Output MUST be a valid JSON array and nothing else\n"
                "- Mix snake_case and camelCase prefixes matching C/C++ naming\n"
                "- STRONGLY prefer short stems with a trailing wildcard\n"
                "- You may use a trailing wildcard: \"modem*\" matches modem_init, modem_connect\n"
                "- No asterisks anywhere else — FTS5 only supports trailing wildcards\n"
                "- CRITICAL: camelCase tokens are ONE token — match their BEGINNINGS\n"
                "  e.g. for \"connect\" generate \"connection*\" not \"onConnection*\"\n\n"
                f"Description: {query}\n"
            )

        if cfg.llm.enabled:
            try:
                raw = await call_ollama_async(prompt, cfg.llm)
                keyword_queries = _parse_search_terms(raw)
                keyword_queries = keyword_queries[:5]
            except OllamaModelNotFoundError as e:
                ollama_warning = {"warning": str(e), "hint": "Run: check_ollama()"}
                keyword_queries = rough_terms
            except OllamaError as e:
                ollama_warning = {"warning": f"Ollama unavailable: {e}"}
                keyword_queries = rough_terms
        else:
            keyword_queries = rough_terms

        # --- Phase 3: OR search with client-side term-match scoring ---
        # Using OR instead of AND because Ollama generates diverse terms
        # covering different naming patterns — no single symbol matches all.
        if keyword_queries:
            # Build two OR queries:
            # 1. Standard query across all columns (strong FTS5 ranking for exact matches)
            # 2. name_tokens column-filter query to specifically surface camelCase symbols
            #    whose components match query terms — e.g. "connection*" finds
            #    onConnectionComplete via name_tokens="on connection complete" even when
            #    the name token itself starts with "on" and ranks low in the full search.
            or_query = " OR ".join(keyword_queries)
            # Column-filter expressions "name_tokens : term*" must NOT pass through
            # _expand_query (which would corrupt the ": " as a bare token). They are
            # already valid FTS5 syntax — search_symbols handles them correctly via
            # the ':' guard in _expand_query's bypass check.
            nt_terms = [f"name_tokens : {kq}" for kq in keyword_queries]
            nt_query = " OR ".join(nt_terms)
            # Fetch a larger pool so camelCase symbols buried in FTS5 ranking get included.
            fetch_limit = max(limit * 6, 120)
            rows = []
            seen_keys: set[tuple] = set()
            for q in (or_query, nt_query):
                try:
                    for r in search_symbols(conn, q, config_hash, limit=fetch_limit):
                        k = (r["name"], r["file_path"], r["line"])
                        if k not in seen_keys:
                            seen_keys.add(k)
                            rows.append(r)
                except Exception as e:
                    log.debug("smart_search FTS5 query failed (q=%r): %s", q[:60], e)

            if rows:
                stems = [kq.rstrip("*").lower() for kq in keyword_queries]

                def _score(r) -> int:
                    name   = (r["name"]          or "").lower()
                    ntoks  = (r["name_tokens"]    or "").lower()
                    qname  = (r["qualified_name"] or "").lower()
                    fpath  = (r["file_path"]      or "").lower()

                    # Weighted field matching — each stem counted at highest level only:
                    #   name / name_tokens = 3  (it IS the symbol name, just differently tokenized)
                    #   qualified_name     = 2  (class/namespace context)
                    #   file_path          = 1  (module-level context, weakest)
                    s = 0
                    for stem in stems:
                        if stem in name or stem in ntoks:
                            s += 3
                        elif stem in qname:
                            s += 2
                        elif stem in fpath:
                            s += 1

                    # Bonus for project-local code (src/, lib/ not under mbed-os).
                    if fpath and ("src/" in fpath or "lib/" in fpath) and "mbed-os" not in fpath:
                        s += 1

                    # Kind bonus: push functions/classes above variables/locals.
                    s += _KIND_WEIGHT.get(r["kind"] or "", 0)

                    return s

                # rank is a hidden FTS5 column not exposed via JOIN — use row position as fallback
                scored = [(_score(r), i, r) for i, r in enumerate(rows)]
                # Sort: higher score first, then by original FTS5 position
                scored.sort(key=lambda x: (-x[0], x[1]))

                for _, _, r in scored:
                    name = r["name"] or ""
                    # Skip noise: unnamed symbols, single/two-char local variable names,
                    # and C-style loop/temp variables that leak through file_path matching.
                    if name.startswith("("):
                        continue
                    if len(name) <= 2 and r["kind"] in ("variable", "field"):
                        continue
                    key = (name, r["file_path"], r["line"])
                    if key not in seen:
                        seen[key] = _fmt(r)
            elif not seen:
                # Fallback: try individual queries if OR found nothing
                for kq in keyword_queries:
                    try:
                        rows = search_symbols(conn, kq, config_hash, limit=limit)
                    except Exception:
                        continue
                    for r in rows:
                        key = (r["name"], r["file_path"], r["line"])
                        if key not in seen:
                            seen[key] = _fmt(r)

    results: list[dict] = []

    results.append({"_generated_queries": keyword_queries})
    if rough_terms:
        results.append({"_rough_queries": rough_terms})
    if translated_from:
        results.append({"_translated_from": translated_from, "_translated_to": query})
    if ollama_warning:
        results.append(ollama_warning)
    if not ollama_warning and cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"]):
        results.append({"warning": "Index may be stale — run 'fw-context index'."})

    results += list(seen.values())[:limit]
    if not seen:
        results.append({"info": "No results found for the generated queries."})
    return results


def main() -> None:
    mcp.run()
