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


def main() -> None:
    mcp.run()
