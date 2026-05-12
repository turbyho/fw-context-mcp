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
    transaction,
    upsert_file,
)
from ..llm.ollama import OllamaError, OllamaModelNotFoundError, call_ollama_async, check_setup

log = logging.getLogger(__name__)

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


def _is_stale(cfg, compile_commands_path: str) -> bool:
    """Return True if compile_commands.json is newer than the indexed timestamp."""
    try:
        cc_mtime = os.path.getmtime(compile_commands_path)
        indexed_at = datetime.fromisoformat(cfg["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + 1
    except Exception:
        return False


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
                "file": r["file_path"],
                "line": r["line"],
                "is_definition": bool(r["is_definition"]),
                "signature": r["signature"],
                "docstring": r["docstring"],
            }
            for r in rows
        ]
        stale_f = _stale_files(conn, cfg["config_hash"], [r["file_path"] for r in rows])
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
                    """SELECT s.*, f.path as file_path FROM symbols s
                       JOIN files f ON f.id = s.file_id
                       WHERE s.config_hash=? AND s.name=?
                       ORDER BY s.is_definition DESC, s.line
                       LIMIT ?""",
                    (config_hash, name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT s.*, f.path as file_path FROM symbols s
                       JOIN files f ON f.id = s.file_id
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
                    "file": r["file_path"],
                    "line": r["line"],
                    "is_definition": bool(r["is_definition"]),
                    "signature": r["signature"],
                    "docstring": r["docstring"],
                }
                for r in rows
            ]
            stale_f = _stale_files(conn, cfg["config_hash"], [r["file_path"] for r in rows])
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
                    rows.append((
                        config_hash, file_id_cache[sym_file], s.usr, s.name,
                        s.qualified_name, s.kind, s.line, s.column,
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
        - status: "ok" | "model_missing" | "error"
        - ollama_running (bool)
        - configured_model (str): the model name from config
        - num_ctx (int): context size setting
        - installed_models (list[str]): all models available in Ollama
        - message (str, when status != "ok"): human-readable description
        - available_code_models (list[str], when model missing): code-related
          models that are already installed and could be used instead
    """
    _, cfg, _, _ = _resolve_context(project_root)
    return check_setup(cfg.llm)


@mcp.tool()
async def explain_symbol(
    name: str,
    project_root: str | None = None,
    context_lines: int = 40,
) -> dict:
    """Look up a symbol and ask Ollama to explain what it does.

    Sends the symbol's source code context (surrounding lines) to a local
    Ollama model for a plain-English explanation. The explanation is 2–4
    sentences, focused on purpose and behaviour.

    **Performance:** Each call takes 10–30 seconds. Do NOT call in a loop
    over many symbols. Call ``check_ollama()`` first to verify the model is
    available. Falls back with a warning when Ollama is unavailable.

    Args:
        name: Symbol name (exact match). If multiple symbols share the name,
              the definition is preferred over declarations.
        project_root: Absolute path to the project. Defaults to nearest git root.
        context_lines: Lines of source code to include above and below the
                       definition for context (default 40).

    Returns:
        dict with: name, kind, file, line, signature, and either explanation
        (str) or warning (str) when Ollama is unavailable.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    with open_db(db_path) as conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return {"error": "No build config indexed."}

        config_hash = cfg_data["config_hash"]
        row = conn.execute(
            """SELECT s.*, f.path as file_path FROM symbols s
               JOIN files f ON f.id = s.file_id
               WHERE s.config_hash=? AND s.name=? AND s.is_definition=1
               ORDER BY s.line LIMIT 1""",
            (config_hash, name),
        ).fetchone()
        if not row:
            row = conn.execute(
                """SELECT s.*, f.path as file_path FROM symbols s
                   JOIN files f ON f.id = s.file_id
                   WHERE s.config_hash=? AND s.name=?
                   ORDER BY s.is_definition DESC, s.line LIMIT 1""",
                (config_hash, name),
            ).fetchone()
        if not row:
            return {"error": f"Symbol not found: {name}"}

        file_path = row["file_path"]
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
        f"Explain what the following {kind} does in 2-4 sentences. "
        f"Be concise and focus on purpose and behaviour.\n\n"
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
    try:
        result["explanation"] = await call_ollama_async(prompt, cfg.llm)
    except OllamaModelNotFoundError as e:
        result["warning"] = str(e)
    except OllamaError as e:
        result["warning"] = f"Ollama unavailable: {e}"

    return result


@mcp.tool()
async def smart_search(
    query: str,
    project_root: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Natural-language search: Ollama generates FTS5 keywords, then searches the index.

    Translates a natural-language description into 2–4 FTS5 search terms by
    asking a local Ollama model, then runs ``search_code`` for each generated
    term and merges unique results.

    **When to prefer over search_code:** When you don't know the exact keywords
    and want to describe what you're looking for ("how does the modem connect?",
    "handle BLE pairing failure").

    **Fallback:** When Ollama is unavailable or the model is not installed,
    falls back to direct FTS5 search with the original query text. Results
    include ``_generated_queries`` so you can see which terms were used.

    Args:
        query: Natural language description of what you're looking for.
               Be specific — 5–15 words works best.
        project_root: Absolute path to the project. Defaults to nearest git root.
        limit: Maximum number of results (default 20, max 100).

    Returns:
        list of dicts. The first entry is ``_generated_queries`` with the list
        of search terms used. Subsequent entries are symbol results in the same
        format as ``search_code``. May include a warning when Ollama is
        unavailable or the index is stale.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    cfg_data = None
    config_hash = ""
    seen: dict[tuple, dict] = {}

    with open_db(db_path) as conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            return [{"error": "No build config indexed."}]
        config_hash = cfg_data["config_hash"]
        limit = min(limit, 100)

        keyword_queries: list[str] = []
        ollama_warning: dict | None = None

        prompt = (
            "You are a C/C++ code search assistant for an embedded firmware project.\n"
            "Generate 2-4 FTS5 keyword search terms for the symbol index based on the description below.\n"
            "Rules:\n"
            "- Output ONLY the search terms, one per line, no numbering, no markdown, no punctuation\n"
            "- Use snake_case identifiers (e.g. modem_init, conn_open)\n"
            "- You may use a trailing wildcard suffix: modem* matches modem_init, modem_connect, etc.\n"
            "- No asterisks anywhere else — FTS5 only supports trailing wildcards\n"
            "- Prefer short stems over full function names\n\n"
            f"Description: {query}\n"
        )
        try:
            raw = await call_ollama_async(prompt, cfg.llm)
            keyword_queries = []
            for line in raw.splitlines():
                # strip markdown: leading numbers/bullets, asterisks, backticks, quotes
                cleaned = re.sub(r'^[\s\d\.\-\*]+', '', line).strip().strip('`\'"*')
                if cleaned and not cleaned.startswith("#"):
                    # FTS5 query parser doesn't split on '_'; replace with space so
                    # 'modem_init' becomes AND('modem','init') rather than a missing token
                    cleaned = cleaned.replace("_", " ")
                    keyword_queries.append(cleaned)
            keyword_queries = keyword_queries[:4]
        except OllamaModelNotFoundError as e:
            ollama_warning = {"warning": str(e), "hint": "Run: check_ollama()"}
            keyword_queries = [query]
        except OllamaError as e:
            ollama_warning = {"warning": f"Ollama unavailable, using direct search: {e}"}
            keyword_queries = [query]

        for kq in keyword_queries:
            try:
                rows = search_symbols(conn, kq, config_hash, limit=limit)
            except Exception:
                continue
            for r in rows:
                key = (r["name"], r["file_path"], r["line"])
                if key not in seen:
                    seen[key] = {
                        "name": r["name"],
                        "qualified_name": r["qualified_name"],
                        "kind": r["kind"],
                        "file": r["file_path"],
                        "line": r["line"],
                        "is_definition": bool(r["is_definition"]),
                        "signature": r["signature"],
                        "docstring": r["docstring"],
                    }

    results: list[dict] = []

    results.append({"_generated_queries": keyword_queries})
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
