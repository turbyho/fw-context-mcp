"""fw-context MCP server — build-aware code intelligence for embedded projects."""

from __future__ import annotations

import json
import logging
import math
import os
import re
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
    delete_refs_for_file,
    delete_symbols_for_file,
    find_refs,
    get_active_config,
    get_all_projects,
    get_file_mtime_indexed,
    get_file_mtimes,
    insert_refs_batch,
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

# In-memory cache of Ollama-generated keyword terms, keyed by (query, config_hash).
# The MCP server is long-lived, so this avoids re-calling Ollama (10-30 s) for
# repeated/similar queries within a session. Bounded; cleared wholesale when full.
_KEYWORD_CACHE: dict[tuple, list[str]] = {}
_KEYWORD_CACHE_MAX = 256

# Lazy-loaded embedding index for semantic search.
# Keyed by config_hash — each build has its own symbol set.
# Values: (descriptions: list[str], embeddings: list[list[float]], symbol_ids: list[int])
_EMBEDDING_CACHE: dict[str, tuple[list[str], list[list[float]], list[int]]] = {}


def _build_symbol_description(r: dict) -> str:
    """Build a hierarchical description string for embedding.

    Format: <path> <file> <class> <name> <signature> [docstring]

    The directory path and class name give the embedding model domain
    context — e.g. 'lib/modem zmodem_driver ZMODEM_DRIVER network_registration'
    clearly belongs to the modem subsystem.
    """
    fp = (r.get("file_path") or "").replace("\\", "/")
    path = ""
    file_ = ""
    if "/" in fp:
        *dirs, file_ = fp.split("/")
        path = "/".join(dirs[-2:])  # last 2 dirs
    elif fp:
        file_ = fp

    # Extract class/namespace from qualified_name
    qname = (r.get("qualified_name") or "")
    name = r.get("name") or ""
    class_ = ""
    if "::" in qname:
        # Everything before the last :: is the class/namespace
        class_ = "::".join(qname.split("::")[:-1])

    sig = (r.get("signature") or "")

    # Append docstring if available (short — hierarchy matters more)
    doc = (r.get("docstring") or "").strip()
    if doc and len(doc) > 20:
        doc = doc[:150]

    parts = [path, file_, class_, name, sig]
    if doc:
        parts.append(doc)
    return " : ".join(p for p in parts if p)


def _ensure_embeddings(conn, config_hash: str, cfg_llm) -> tuple[list[str], list[list[float]], list[int]]:
    """Load embeddings from DB (fast), or generate + store if not indexed.

    After ``fw-context index --index-embeddings``, embeddings are stored
    in the ``embeddings`` table and loaded directly — no Ollama call needed.
    """
    global _EMBEDDING_CACHE
    if config_hash in _EMBEDDING_CACHE:
        return _EMBEDDING_CACHE[config_hash]

    from ..indexer.db import get_embeddings, _vec_to_blob, upsert_embeddings
    from ..llm.ollama import call_ollama_embed

    # 1. Try loading pre-computed embeddings from DB
    stored = get_embeddings(conn, config_hash, cfg_llm.embed_model)
    if stored:
        # Fetch symbol metadata for the cached rows
        placeholders = ",".join("?" * len(stored))
        rows = conn.execute(
            f"""SELECT id, name, qualified_name, kind, file_path,
                       signature, is_definition, docstring
                FROM symbols
                WHERE id IN ({placeholders})
                ORDER BY id""",
            list(stored.keys()),
        ).fetchall()
        descriptions = [_build_symbol_description(r) for r in rows]
        symbol_ids = [r["id"] for r in rows]
        embeddings = [stored[sid] for sid in symbol_ids]
        _EMBEDDING_CACHE[config_hash] = (descriptions, embeddings, symbol_ids)
        log.info("Loaded %d embeddings from DB", len(embeddings))
        return _EMBEDDING_CACHE[config_hash]

    # 2. Fallback: generate embeddings on the fly and store for next time
    log.info("Building embedding index for config %s...", config_hash[:12])
    rows = conn.execute(
        """SELECT s.id, s.name, s.qualified_name, s.kind, s.file_path,
                  s.signature, s.is_definition, s.docstring
           FROM symbols s
           WHERE s.config_hash = ?
             AND s.is_definition = 1
             AND s.kind IN ('function', 'method', 'constructor', 'destructor',
                            'class', 'struct')
           ORDER BY CASE WHEN s.docstring IS NOT NULL AND LENGTH(s.docstring) > 30
                      THEN 0 ELSE 1 END,
                    CASE WHEN (s.file_path LIKE 'src/%' OR s.file_path LIKE 'lib/%')
                         AND s.file_path NOT LIKE '%mbed-os%'
                      THEN 0 ELSE 1 END""",
        (config_hash,),
    ).fetchall()

    if not rows:
        _EMBEDDING_CACHE[config_hash] = ([], [], [])
        return _EMBEDDING_CACHE[config_hash]

    descriptions = [_build_symbol_description(r) for r in rows]
    symbol_ids = [r["id"] for r in rows]

    all_embeddings: list[list[float]] = []
    chunk_size = 100
    for i in range(0, len(descriptions), chunk_size):
        chunk = descriptions[i:i + chunk_size]
        chunk_rows = rows[i:i + chunk_size]
        try:
            embeddings = call_ollama_embed(chunk, cfg_llm)
            all_embeddings.extend(embeddings)
            # Store to DB for next time
            batch = [(r["id"], _vec_to_blob(emb), cfg_llm.embed_model)
                     for r, emb in zip(chunk_rows, embeddings)]
            upsert_embeddings(conn, batch)
        except Exception as e:
            log.warning("Embedding batch %d failed: %s", i // chunk_size, e)
            all_embeddings.extend([[0.0] * 1024] * len(chunk))

    _EMBEDDING_CACHE[config_hash] = (descriptions, all_embeddings, symbol_ids)
    log.info("Embedding index ready: %d symbols", len(descriptions))
    return _EMBEDDING_CACHE[config_hash]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


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


def _open_db_safe(db_path: Path) -> tuple[sqlite3.Connection | None, dict | None]:
    """Open DB with corruption check.

    Returns ``(conn, None)`` on success — the caller should continue with
    ``with conn:``.  Returns ``(None, error_dict)`` when the database is
    corrupt, so the tool can short-circuit and return the error directly.
    """
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

    Tries exact match on short name first (``ModemMsgManager``), then falls back
    to exact match on qualified name (``zbox::ModemMsgManager``).
    """
    BASE_QUERY = """SELECT s.* FROM symbols s
       WHERE s.config_hash=? AND %s
       ORDER BY %s (CASE WHEN s.file_path LIKE '%%mbed-os%%' THEN 1 ELSE 0 END), s.line
       LIMIT 1"""

    for column in ("s.name", "s.qualified_name"):
        # 1) definition preferred
        row = conn.execute(
            BASE_QUERY % (f"{column}=? AND s.is_definition=1", ""),
            (config_hash, name),
        ).fetchone()
        if row:
            return row

        # 2) any symbol, definitions sorted first
        row = conn.execute(
            BASE_QUERY % (f"{column}=?", "s.is_definition DESC,"),
            (config_hash, name),
        ).fetchone()
        if row:
            return row

    return None


def _read_symbol_body(file_path: str, line_no: int, end_line: int = 0, max_lines: int = 400) -> str:
    """Read a symbol's source as numbered lines.

    Preferred path: when ``end_line`` is known (libclang AST extent stored at
    index time), read the exact ``line_no..end_line`` range — correct for
    multi-line signatures, braces in strings/comments, macros and templates.

    Fallback (end_line == 0, e.g. an index built before the column existed):
    brace-balance forward from the definition line, or a small window when the
    symbol has no brace body. Brace counting is best-effort.
    """
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
    except Exception:
        return ""
    start = line_no - 1  # 0-indexed
    if start < 0 or start >= len(lines):
        return ""

    # Preferred: exact extent from libclang.
    if end_line and end_line >= line_no:
        end = min(len(lines) - 1, end_line - 1)
        return "\n".join(f"{i + 1:4d}  {lines[i]}" for i in range(start, end + 1))

    # Fallback: brace matching.
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


def _count_modified_files(conn, config_hash: str, root: Path) -> int:
    """Count indexed files whose on-disk mtime is newer than the stored mtime.

    More representative of "should I reindex?" than the compile_commands.json
    timestamp alone — source files change far more often. Relative file paths
    are resolved against the project root before stat.
    """
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
    """Reindex stale files in-place. Returns (succeeded, failed).

    Bounded by *max_files* and *timeout_s* to prevent unbounded latency
    when many files are stale (e.g. after a toolchain update touches every
    object file).
    """
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
        - reference_count (int, optional): total cross-references indexed (only
          present when refs are indexed)
        - modified_files_count (int): number of indexed files whose on-disk
          content has changed since the last index
        - stale (bool): True when compile_commands.json or any indexed source
          file has changed since the last index
    """
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
        is_definition, signature, docstring. Stale files found in results
        are auto-reindexed (up to 5 files, 30 s timeout) and the query is
        re-run. May include a ``warning`` entry when auto-reindex fails or
        the compile_commands.json itself is stale.

    Example:
        ``search_code("modem init", kind="method", limit=5)``
    """
    try:
        root = _resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

        conn, err = _open_db_safe(db_path)
        if err:
            return [err]

        # --- inner: run FTS5 search + format results ---
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

        # --- auto-reindex stale files, then re-run search ---
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
        Stale files are auto-reindexed (up to 5 files, 30 s timeout) and
        the lookup is re-run. May include a ``warning`` when auto-reindex
        fails or the compile_commands.json itself is stale.

    Example:
        ``lookup_symbol("BoxManager", exact=True)`` → constructor + class
    """
    try:
        root = _resolve_project_root(project_root)
        db_path = _db_path(root)
        if not db_path.exists():
            return [{"error": f"No index found for {root}."}]

        conn, err = _open_db_safe(db_path)
        if err:
            return [err]

        # --- inner: run SQL lookup + format results ---
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
                # Escape LIKE metacharacters so underscores (ubiquitous in C
                # identifiers) match literally instead of as single-char wildcards.
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

        # --- auto-reindex stale files, then re-run lookup ---
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
        May include ``warning`` (str) when the database is corrupt.
    """
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
    from ..indexer.symbols import extract_all  # lazy: requires libclang

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

        for unit in matching:
            file_path_str = str(unit.file.resolve())
            current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0

            # Extract first — if this fails, skip TU without touching DB
            try:
                syms, refs = extract_all(
                    unit, source_roots=source_roots, exclude_paths=exclude_paths,
                    with_refs=cfg.index.index_refs,
                )
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
                        # Store each file's real mtime — including headers pulled in
                        # via this TU. Writing 0.0 for headers would make them look
                        # perpetually stale and trigger spurious auto-reindex attempts.
                        if sym_file == file_path_str:
                            sym_mtime = current_mtime
                        else:
                            try:
                                sym_mtime = Path(sym_file).stat().st_mtime
                            except OSError:
                                sym_mtime = 0.0
                        file_id_cache[sym_file] = upsert_file(conn, config_hash, sym_file, lang, mtime=sym_mtime)
                    try:
                        rel_path = str(Path(sym_file).resolve().relative_to(root))
                    except ValueError:
                        rel_path = sym_file
                    rows.append((
                        config_hash, file_id_cache[sym_file], rel_path,
                        split_tokens(s.name, s.qualified_name),
                        s.usr, s.name, s.qualified_name, s.kind, s.line, s.column,
                        s.end_line, int(s.is_definition), s.signature, s.docstring,
                    ))

                if rows:
                    total_symbols += insert_symbols_batch(conn, rows)

                if cfg.index.index_refs:
                    def _rel(p: str) -> str:
                        try:
                            return str(Path(p).resolve().relative_to(root))
                        except ValueError:
                            return p
                    tu_rel = _rel(file_path_str)
                    delete_refs_for_file(conn, config_hash, tu_rel)
                    if refs:
                        ref_rows = [
                            (config_hash, r.to_usr, _rel(r.from_file), r.from_line, r.from_usr, r.ref_kind)
                            for r in refs
                        ]
                        insert_refs_batch(conn, ref_rows)

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
        name: Symbol name (exact match on short name, e.g. ``ModemMsgManager``,
              or qualified name, e.g. ``zbox::ModemMsgManager``).
              If multiple symbols share the name, the definition is preferred
              over declarations. Use ``lookup_symbol(name)`` first if unsure
              about the exact name.
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
        name: Symbol name (exact match on short name or qualified name).
        project_root: Absolute path to the project. Defaults to nearest git root.

    Returns:
        dict with: name, qualified_name, kind, file (absolute), line, signature,
        source (numbered lines of the full definition body). Error dict if the
        symbol is not indexed.
    """
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
    """Find call sites of a function/method — who calls ``name``.

    Returns only direct calls (ref_kind='call'). Requires the cross-reference
    graph to be built (``[index] index_refs = true`` then re-index, or
    ``fw-context index --refs``). Without it, returns an info message.

    Args:
        name: Function/method name (exact match on short name, e.g. ``reset_slot_error_lock``,
              or qualified name, e.g. ``zbox::ZRTDATA::reset_slot_error_lock``).
        project_root: Absolute path to the project. Defaults to nearest git root.
        limit: Maximum number of call sites (default 50, max 200).

    Returns:
        list of dicts with: file (absolute), line, ref_kind, caller
        (qualified name of the enclosing function, or "<file scope>"),
        caller_kind. Or a single info/error dict.
    """
    return _references_result(name, project_root, ref_kind="call", limit=limit)


@mcp.tool()
def find_references(name: str, project_root: str | None = None, limit: int = 50) -> list[dict]:
    """Find all references to a symbol — calls, reads, and member accesses.

    Broader than ``find_callers``: includes every use (calls, value references,
    member access). Requires the cross-reference graph (``[index] index_refs =
    true`` then re-index, or ``fw-context index --refs``).

    Args:
        name: Symbol name (exact match on short name, e.g. ``reset_slot_error_lock``,
              or qualified name, e.g. ``zbox::ZRTDATA::reset_slot_error_lock``).
        project_root: Absolute path to the project. Defaults to nearest git root.
        limit: Maximum number of references (default 50, max 200).

    Returns:
        list of dicts with: file (absolute), line, ref_kind ('call'|'ref'|
        'member'), caller (enclosing function qualified name), caller_kind.
        Or a single info/error dict.
    """
    return _references_result(name, project_root, ref_kind=None, limit=limit)


def _parse_understanding_response(raw: str) -> tuple[str, list[str]]:
    """Parse Phase 2a LLM response into (understanding, queries).

    Expected format:
        UNDERSTANDING: <one sentence>
        QUERIES: ["term1*", "term2*", ...]

    Falls back to treating the whole response as a JSON array if the
    structured format is not found.
    """
    understanding = ""
    queries: list[str] = []

    # Try structured format first
    und_match = re.search(r"UNDERSTANDING:\s*(.+?)(?:\n|$)", raw, re.IGNORECASE)
    if und_match:
        understanding = und_match.group(1).strip()

    # Extract JSON array — look for the first [...] block (handles markdown
    # wrapping, stray text, etc.)
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    queries.append(item.strip())
    except (ValueError, json.JSONDecodeError):
        pass

    return understanding, queries


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
    _BOGUS_TERMS = frozenset({"json", "[]"})
    result = []
    for t in terms:
        cleaned = t.replace("_", " ").strip()
        if cleaned and cleaned.lower() not in _BOGUS_TERMS:
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
        ``_rough_queries``. When the query was translated from a non-ASCII
        language, ``_translated_from`` and ``_translated_to`` are included.
        Subsequent entries are symbol results in the same format as
        ``search_code``. May include a warning when Ollama is unavailable
        or the index is stale.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    # --- Phase 0: detect and translate non-English query to English ---
    # Always use the LLM — Czech without diacritics (e.g. "jak se
    # inicializuje watchdog") passes isascii() but is not English.
    # Simple "translate" prompt: English input passes through naturally,
    # non-English gets translated.  We compare output to input to decide
    # whether a translation actually happened.
    translated_from: str | None = None
    if cfg.llm.enabled:
        translate_prompt = (
            "Translate the following text to English. "
            "If the text is already in English, return it unchanged. "
            "IMPORTANT: Your entire response must be ONLY the final "
            "English text — no introductory words, no explanations, "
            "no \"YES\", no \"The text is already\", nothing else.\n\n"
            f"{query}"
        )
        try:
            translated = await call_ollama_async(translate_prompt, cfg.llm)
            # Strip common LLM conversational prefixes that leak through
            # despite instructions (smaller models often do this).
            for prefix in (
                "YES\n\n", "YES\n", "YES. ", "YES ",
                "The text is already in English.\n\n",
                "The text is already in English.\n",
                "The text is in English.\n\n",
                "The text is in English.\n",
                "Already in English.\n\n",
                "Already in English.\n",
            ):
                if translated.startswith(prefix):
                    translated = translated[len(prefix):]
                    break
            translated = translated.strip()
            if translated and translated != query:
                translated_from = query
                query = translated
        except Exception:
            pass  # keep original query on failure

    cfg_data = None
    config_hash = ""
    seen: dict[tuple, dict] = {}

    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    with conn:
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
        # Balanced: allocate slots proportionally across word pairs so a
        # high-frequency word like "connection" doesn't crowd out equally
        # relevant but less frequent terms from the same query.
        if len(content_words) >= 2:
            pairs = content_words[1:]  # one pair per remaining word
            per_pair_budget = max(3, min(5, 20 // len(pairs)))
            for w in pairs:
                phrase = f'"{content_words[0]} {w}"'
                try:
                    rows = search_symbols(conn, phrase, config_hash, limit=per_pair_budget)
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
        # Balanced: each word gets an equal share of remaining slots,
        # preventing a single high-frequency word (e.g. "connection")
        # from dominating the rough sample set.
        remaining = 20 - len(rough_samples)
        if remaining > 0 and rough_terms:
            per_word_budget = max(2, min(8, remaining // len(rough_terms)))
            for word in rough_terms:
                try:
                    rows = search_symbols(conn, word, config_hash, limit=per_word_budget)
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

        # --- Phase 2a: first LLM call — understand query + generate queries ---
        keyword_queries: list[str] = []
        ollama_warning: dict | None = None

        if rough_samples:
            context_lines = [
                f"  {r['name']} ({r['kind']}) — {r['file_path']}"
                for r in rough_samples[:15]
            ]
            context_str = "\n".join(context_lines)

            prompt = (
                "You are a C/C++ code search assistant for an embedded firmware project.\n\n"
                "A developer asked:\n"
                f"  «{query}»\n\n"
                "Step 1 — Understand the question. Read the full sentence:\n"
                "- What subsystem/domain? (modem, BLE, storage, sensors, etc.)\n"
                "- Disambiguate words from context: e.g. \"registers to the network\"\n"
                "  → network registration (modem), NOT hardware register.\n"
                "  \"modem connects\" → network attach/init, NOT BLE onConnection.\n\n"
                "Step 2 — Generate 3-5 FTS5 prefix search terms.\n"
                "CRITICAL: Look at the actual symbol names in the samples.\n"
                "Use THEIR naming style — snake_case for snake_case code,\n"
                "camelCase for camelCase code. Copy real prefixes, don't\n"
                "invent names. If samples show 'network_registration', use\n"
                "'network_reg*', not 'NetworkRegistration*'.\n"
                "Rules: camelCase = ONE token (match BEGINNINGS), prefer short\n"
                "stems with *, both snake_case AND camelCase, no * except trailing.\n\n"
                "Samples (use file paths to identify the right subsystem; samples\n"
                "from unrelated subsystems are noise):\n"
                f"{context_str}\n\n"
                "Output format:\n"
                "UNDERSTANDING: <one sentence — what subsystem, what they really want>\n"
                "QUERIES: [\"term1*\", \"term2*\", ...]\n"
            )
        else:
            prompt = (
                "You are a C/C++ code search assistant for an embedded firmware project.\n\n"
                "A developer asked:\n"
                f"  «{query}»\n\n"
                "Step 1 — Understand: what subsystem/domain? Disambiguate words.\n"
                "Step 2 — Generate 3-5 FTS5 prefix queries. Rules: camelCase = one\n"
                "token, match beginnings, short stems with *, both snake_case and\n"
                "camelCase, no * except trailing.\n\n"
                "Output format:\n"
                "UNDERSTANDING: <one sentence>\n"
                "QUERIES: [\"term1*\", \"term2*\", ...]\n"
            )

        # --- Helper: run a set of queries and return scored, deduped rows ---
        def _search_queries(queries: list[str], fetch_limit: int) -> list[dict]:
            if not queries:
                return []
            or_query = " OR ".join(queries)
            nt_terms = [f"name_tokens : {kq}" for kq in queries]
            nt_query = " OR ".join(nt_terms)
            rows: list[dict] = []
            seen: dict[tuple, int] = {}
            for q in (or_query, nt_query):
                try:
                    for r in search_symbols(conn, q, config_hash, limit=fetch_limit):
                        k = (r["name"], r["file_path"])
                        prev_idx = seen.get(k)
                        if prev_idx is None:
                            seen[k] = len(rows)
                            rows.append(r)
                        elif r["is_definition"] and not rows[prev_idx]["is_definition"]:
                            rows[prev_idx] = r
                except Exception as e:
                    log.debug("smart_search query failed (q=%r): %s", q[:60], e)
            return rows

        # --- Phase 2a: first LLM call ---
        all_queries: list[str] = []
        if cfg.llm.enabled:
            cache_key = (query, config_hash)
            cached = _KEYWORD_CACHE.get(cache_key)
            if cached is not None:
                all_queries = cached
            else:
                try:
                    raw = await call_ollama_async(prompt, cfg.llm)
                    understanding, first_queries = _parse_understanding_response(raw)
                    keyword_queries = first_queries[:5]
                    all_queries = list(keyword_queries)
                except (OllamaModelNotFoundError, OllamaError) as e:
                    ollama_warning = {"warning": str(e)}
                    keyword_queries = rough_terms
                    all_queries = list(rough_terms)
        else:
            keyword_queries = rough_terms
            all_queries = list(rough_terms)

        # --- Phase 3a: run first-round queries ---
        fetch_limit = max(limit * 6, 120)
        all_rows = _search_queries(all_queries, fetch_limit)

        # --- Phase 2b: second LLM call — refine based on actual results ---
        # Run even when all_rows is empty — zero results means the queries
        # were likely wrong (e.g. invented CamelCase names that don't match
        # the project's snake_case convention).
        if cfg.llm.enabled and not ollama_warning:
            # Format top results for LLM feedback
            top_lines = []
            for r in (all_rows or [])[:10]:
                name = r["name"] or "?"
                kind = r["kind"] or "?"
                path = r["file_path"] or "?"
                top_lines.append(f"  {name} ({kind}) — {path}")
            if all_rows:
                top_str = "\n".join(top_lines)
                result_note = f"Top results from those queries:\n{top_str}\n\n"
            else:
                result_note = "Those queries returned ZERO results — they are likely wrong.\n\n"

            refine_prompt = (
                "A developer searched for:\n"
                f"  «{query}»\n\n"
                f"First-round FTS5 queries: {json.dumps(all_queries)}\n\n"
                f"{result_note}"
                "Are these results from the RIGHT subsystem? If the results look\n"
                "misaligned (e.g. BLE connection code for a modem query, hardware\n"
                "register code for a network registration query), generate 3-5\n"
                "BETTER FTS5 prefix queries that target the CORRECT subsystem.\n\n"
                "If the results already look correct, return an empty array: []\n\n"
                "CRITICAL: Use naming patterns from the original samples. If the\n"
                "project uses snake_case (modem_init, network_registration), your\n"
                "queries MUST be snake_case (modem_init*, network_reg*).\n\n"
                "Return ONLY a JSON array: [\"better1*\", \"better2*\"] or []\n"
            )
            try:
                raw2 = await call_ollama_async(refine_prompt, cfg.llm)
                refined = _parse_search_terms(raw2)[:5]
                if refined:
                    keyword_queries = keyword_queries + refined
                    all_queries = all_queries + refined
                    # Run second-round queries and merge
                    round2_rows = _search_queries(refined, fetch_limit)
                    all_rows.extend(round2_rows)
            except Exception:
                pass  # refinement is optional; keep first-round results

            # Update cache with combined queries
            if len(_KEYWORD_CACHE) >= _KEYWORD_CACHE_MAX:
                _KEYWORD_CACHE.clear()
            _KEYWORD_CACHE[(query, config_hash)] = all_queries

        # --- Phase 3: score, sort, dedup, format final results ---
        seen: dict[tuple, dict] = {}
        if all_rows:
            stems = [kq.rstrip("*").lower() for kq in all_queries]

            def _score(r) -> int:
                name   = (r["name"]          or "").lower()
                ntoks  = (r["name_tokens"]    or "").lower()
                qname  = (r["qualified_name"] or "").lower()
                fpath  = (r["file_path"]      or "").lower()

                s = 0
                for stem in stems:
                    if stem in name or stem in ntoks:
                        s += 3
                    elif stem in qname:
                        s += 2
                    elif stem in fpath:
                        s += 1

                if fpath and ("src/" in fpath or "lib/" in fpath) and "mbed-os" not in fpath:
                    s += 1

                s += _KIND_WEIGHT.get(r["kind"] or "", 0)
                return s

            scored = [(_score(r), i, r) for i, r in enumerate(all_rows)]
            scored.sort(key=lambda x: (-x[0], x[1]))

            for _, _, r in scored:
                name = r["name"] or ""
                if name.startswith("("):
                    continue
                if len(name) <= 2 and r["kind"] in ("variable", "field"):
                    continue
                key = (name, r["file_path"])
                prev = seen.get(key)
                if prev is None:
                    seen[key] = _fmt(r)
                elif r["is_definition"] and not prev["is_definition"]:
                    seen[key] = _fmt(r)
        elif not seen:
            # Fallback: individual queries if OR found nothing
            for kq in all_queries:
                try:
                    rows = search_symbols(conn, kq, config_hash, limit=limit)
                except Exception:
                    continue
                for r in rows:
                    key = (r["name"], r["file_path"])
                    prev = seen.get(key)
                    if prev is None:
                        seen[key] = _fmt(r)
                    elif r["is_definition"] and not prev["is_definition"]:
                        seen[key] = _fmt(r)

    # --- Phase 4: embedding-based semantic search ---
    # Complements FTS5 keyword search.  Embeddings understand that
    # "registers to the network" ≈ network_registration even though
    # the tokens don't overlap.  The hierarchical symbol descriptions
    # (dir + class + name + signature) give domain context.
    embedding_used = False
    if cfg.llm.enabled and not ollama_warning:
        try:
            from ..llm.ollama import call_ollama_embed

            descs, embs, sym_ids = _ensure_embeddings(conn, config_hash, cfg.llm)
            if embs:
                # Embed the query
                query_emb = call_ollama_embed([query], cfg.llm)
                if query_emb:
                    query_vec = query_emb[0]
                    # Compute similarities
                    scored = [
                        (_cosine_similarity(query_vec, ev), sym_ids[i])
                        for i, ev in enumerate(embs)
                    ]
                    scored.sort(key=lambda x: -x[0])
                    # Take top 30, fetch symbol rows
                    top_sims = scored[:30]
                    top_ids = [s[1] for s in top_sims if s[0] > 0.4]
                    if top_ids:
                        placeholders = ",".join("?" * len(top_ids))
                        emb_rows = conn.execute(
                            f"""SELECT * FROM symbols
                                WHERE config_hash = ? AND id IN ({placeholders})
                                AND is_definition = 1""",
                            (config_hash, *top_ids),
                        ).fetchall()
                        # Add to seen with embedding score boost marker
                        for r in emb_rows:
                            key = (r["name"], r["file_path"])
                            if key not in seen:
                                seen[key] = _fmt(r)
                        if emb_rows:
                            embedding_used = True
        except Exception:
            pass  # embedding is optional; FTS5 results still work

    results: list[dict] = []

    results.append({"_generated_queries": keyword_queries})
    if embedding_used:
        results.append({"_embedding_used": True})
    if rough_terms:
        results.append({"_rough_queries": rough_terms})
    if translated_from:
        results.append({"_translated_from": translated_from, "_translated_to": query})
    if ollama_warning:
        results.append(ollama_warning)
    if not ollama_warning and cfg_data and _is_stale(cfg_data, cfg_data["compile_commands_path"]):
        results.append({
            "warning": "Index may be stale — compile_commands.json changed since last index.",
            "hint": "Call reindex_file() on modified files or run 'fw-context index' to update.",
        })

    results += list(seen.values())[:limit]
    if not seen:
        results.append({"info": "No results found for the generated queries."})
    return results


def main() -> None:
    mcp.run()
