"""fw-context MCP server — build-aware code intelligence for embedded projects."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import load as load_config
from ..indexer.db import get_active_config, open_db, search_symbols
from ..llm.ollama import OllamaError, call_ollama

log = logging.getLogger(__name__)

mcp = FastMCP(
    "fw-context",
    instructions="Build-aware code intelligence index for embedded firmware (Mbed OS, Zephyr).",
)

def _db_path(project_root: Path) -> Path:
    cfg = load_config(project_root=project_root)
    project_id = _derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _derive_project_id(root: Path) -> str:
    try:
        import subprocess
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


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


def _is_stale(cfg: object, compile_commands_path: str) -> bool:
    """Return True if compile_commands.json is newer than the indexed timestamp."""
    try:
        cc_mtime = os.path.getmtime(compile_commands_path)
        indexed_at = datetime.fromisoformat(cfg["created_at"]).replace(tzinfo=timezone.utc)
        return cc_mtime > indexed_at.timestamp() + 1
    except Exception:
        return False


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

    Args:
        project_root: Absolute path to the project. Defaults to nearest git root.

    Returns a dict with config_hash, project name, compile_commands path, and
    symbol/file counts.
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    conn = open_db(db_path)
    project_id = _derive_project_id(root)
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

    Args:
        query: Search term(s). Supports FTS5 syntax (prefix: "init*", phrase: '"spi init"').
        project_root: Absolute path to the project. Defaults to nearest git root.
        kind: Optional filter by symbol kind: function, method, class, struct, enum,
              typedef, variable, field, enum_constant, namespace.
        limit: Maximum number of results (default 20, max 100).

    Returns list of dicts with name, qualified_name, kind, file, line, signature, docstring.
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    conn = open_db(db_path)
    project_id = _derive_project_id(root)
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

    results += [
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
    return results


@mcp.tool()
def lookup_symbol(
    name: str,
    project_root: str | None = None,
    exact: bool = False,
) -> list[dict]:
    """Look up a symbol by name. Returns all matches (declarations + definitions).

    Args:
        name: Symbol name to look up (case-sensitive).
        project_root: Absolute path to the project.
        exact: If True, match exact name only. If False, also match as prefix.
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}."}]

    conn = open_db(db_path)
    project_id = _derive_project_id(root)
    cfg = get_active_config(conn, project_id)
    if not cfg:
        return [{"error": "No build config indexed."}]

    config_hash = cfg["config_hash"]
    if exact:
        rows = conn.execute(
            """SELECT s.*, f.path as file_path FROM symbols s
               JOIN files f ON f.id = s.file_id
               WHERE s.config_hash=? AND s.name=?
               ORDER BY s.is_definition DESC, s.line""",
            (config_hash, name),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.*, f.path as file_path FROM symbols s
               JOIN files f ON f.id = s.file_id
               WHERE s.config_hash=? AND s.name LIKE ?
               ORDER BY s.is_definition DESC, s.line
               LIMIT 50""",
            (config_hash, f"{name}%"),
        ).fetchall()

    results: list[dict] = []
    if _is_stale(cfg, cfg["compile_commands_path"]):
        results.append({"warning": "Index may be stale — compile_commands.json changed since last index. Run 'fw-context index' to update."})

    results += [
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
    return results


@mcp.tool()
def explain_symbol(
    name: str,
    project_root: str | None = None,
    context_lines: int = 40,
) -> dict:
    """Look up a symbol and ask Ollama to explain what it does.

    Args:
        name: Symbol name (exact match).
        project_root: Absolute path to the project. Defaults to nearest git root.
        context_lines: Lines of source code to include above and below the definition.

    Returns dict with name, file, line, signature, and explanation (or error).
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return {"error": f"No index found for {root}. Run 'fw-context index' first."}

    conn = open_db(db_path)
    project_id = _derive_project_id(root)
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

    cfg = load_config(project_root=root)
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
        result["explanation"] = call_ollama(prompt, cfg.llm)
    except OllamaError as e:
        result["warning"] = f"Ollama unavailable: {e}"

    return result


@mcp.tool()
def smart_search(
    query: str,
    project_root: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Natural-language search: Ollama generates FTS5 keywords, then searches the index.

    Args:
        query: Natural language description of what you're looking for.
        project_root: Absolute path to the project. Defaults to nearest git root.
        limit: Maximum number of results (default 20).

    Falls back to direct FTS5 search when Ollama is unavailable.
    """
    root = _resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]

    conn = open_db(db_path)
    project_id = _derive_project_id(root)
    cfg_data = get_active_config(conn, project_id)
    if not cfg_data:
        return [{"error": "No build config indexed."}]

    cfg = load_config(project_root=root)
    config_hash = cfg_data["config_hash"]
    limit = min(limit, 100)

    keyword_queries: list[str] = []
    ollama_warning: dict | None = None

    prompt = (
        "You are a C/C++ code search assistant.\n"
        "Generate 2-4 FTS5 search keyword queries for the following natural-language "
        "description. Output one query per line, no explanations, no numbering.\n"
        "Use short identifiers, C/C++ naming conventions, and prefix wildcards when useful.\n\n"
        f"Description: {query}\n"
    )
    try:
        raw = call_ollama(prompt, cfg.llm)
        keyword_queries = [
            line.strip().strip('"').strip("'")
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ][:4]
    except OllamaError as e:
        ollama_warning = {"warning": f"Ollama unavailable, using direct search: {e}"}
        keyword_queries = [query]

    seen: dict[tuple, dict] = {}
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
    if ollama_warning:
        results.append(ollama_warning)
    elif _is_stale(cfg_data, cfg_data["compile_commands_path"]):
        results.append({"warning": "Index may be stale — run 'fw-context index'."})

    results += list(seen.values())[:limit]
    return results


def main() -> None:
    mcp.run()
