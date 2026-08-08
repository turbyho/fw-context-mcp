"""``fw-context search``, ``list``, ``status`` — index query commands.

Exposes the same search capabilities as the MCP server (symbol lookup,
full-text search, semantic search) but from the command line.  Useful for
debugging, scripting, and when the MCP server is not running.

WHY a CLI search: the MCP server requires an AI tool client; the CLI
search works standalone.  Users can verify index quality, debug search
results, and integrate with shell scripts without the overhead of MCP.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path


def cmd_search(args: argparse.Namespace) -> int:
    """Full-text search over indexed symbols and print results to stdout.

    Queries the FTS5 index for symbols matching the given keywords
    and prints each hit with its kind, qualified name, file, and line.
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import get_active_config, open_db, search_symbols
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)

    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        print(f"No index found at {db_path}. Run 'fw-context index' first.")
        return 1

    conn = open_db(db_path)
    build_cfg = get_active_config(conn, project_id)
    if not build_cfg:
        print("No build config indexed.")
        return 1

    results = search_symbols(conn, args.query, build_cfg["config_hash"], limit=args.limit)
    for r in results:
        print(f"[{r['kind']:15}] {r['qualified_name']}  @ {Path(r['file_path']).name}:{r['line']}")
        if args.verbose and r["signature"]:
            print(f"               {r['signature']}")
    print(f"\n{len(results)} result(s) for '{args.query}'")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all indexed firmware projects found under the configured index directory.

    Prints a table with project name, root path, symbol count, file count,
    and index timestamp. Stale projects (compile_commands.json changed since
    last index) are marked with ``*``.
    """
    from ..config import load as load_config
    from ..indexer.db import get_all_projects, open_db
    from ..utils import fmt_count, truncate_path_middle

    cfg = load_config()
    index_dir = cfg.index.db_dir
    db_files = sorted(index_dir.glob("*/index.db")) if index_dir.exists() else []

    if not db_files:
        print(f"No indexed projects under {index_dir}.")
        return 0

    # --- Collect all rows first so we can compute column widths ---
    header = ("PROJECT", "PATH", "SYMBOLS", "FILES", "INDEXED")
    rows: list[tuple[str, str, str, str, str]] = []

    for db_path in db_files:
        try:
            conn = open_db(db_path)
            for r in get_all_projects(conn):
                name = r["name"] or r["project_id"]
                if _cli_is_stale(r):
                    name += " *"
                rows.append((
                    name,
                    r["root_path"] or "",
                    fmt_count(r["symbol_count"]),
                    fmt_count(r["file_count"]),
                    r["created_at"] or "",
                ))
                if args.verbose:
                    rows.append(("", f"  db={db_path}", "", "", ""))
                    rows.append(("", f"  cc={r['compile_commands_path']}", "", "", ""))
        except (sqlite3.Error, OSError) as e:
            print(f"[error] {db_path}: {e}", file=sys.stderr)

    if not rows:
        return 0

    # --- Compute column widths ---
    col_widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    # Constrain PATH width to terminal width
    term_width = shutil.get_terminal_size((120, 24)).columns
    # 3 chars between columns, plus some margin
    separator_width = 2 * (len(header) - 1)
    fixed_width = sum(col_widths[i] for i in range(len(header)) if i != 1) + separator_width
    max_path_width = max(term_width - fixed_width - 2, 30)  # min 30 chars for path
    col_widths[1] = min(col_widths[1], max_path_width)

    # --- Print header ---
    def _fmt_row(vals: tuple[str, ...]) -> str:
        parts: list[str] = []
        for i, val in enumerate(vals):
            w = col_widths[i]
            # Left-align everything except SYMBOLS and FILES (right-align)
            align = ">" if header[i] in ("SYMBOLS", "FILES") else "<"
            parts.append(f"{val:{align}{w}}")
        return "  ".join(parts)

    print(_fmt_row(header))

    # --- Print rows ---
    for row in rows:
        # Truncate path in the middle if needed
        vals = list(row)
        path = vals[1]
        if len(path) > col_widths[1]:
            vals[1] = truncate_path_middle(path, col_widths[1])
        print(_fmt_row(tuple(vals)))

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show index status for the current project.

    Prints project root, symbol count, file count, index timestamp,
    and whether the index is stale (compile_commands.json changed
    since last index).
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import get_active_config, open_db
    from ..utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)

    project_id = derive_project_id(project_root)

    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        print("No index found. Run 'fw-context index' first.")
        return 1

    conn = open_db(db_path)
    active = get_active_config(conn, project_id)
    if not active:
        print("No build config indexed.")
        return 1

    sym_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE config_hash=?", (active["config_hash"],)).fetchone()[0]
    file_count = conn.execute("SELECT COUNT(*) FROM files WHERE config_hash=?", (active["config_hash"],)).fetchone()[0]

    stale = _cli_is_stale(active)

    print(f"Project : {project_root}")
    print(f"Symbols : {sym_count}  files={file_count}")
    print(f"Indexed : {active['created_at']}{'  [STALE]' if stale else ''}")
    print(f"DB      : {db_path}")
    if stale:
        print("  compile_commands.json changed — run 'fw-context index' to update")
    return 0


def _cli_is_stale(row) -> bool:
    """Check if a project's compile_commands.json is newer than its index timestamp.

    Delegates to :func:`fw_context_mcp.utils.is_compile_commands_stale`.
    Returns False on any error so staleness checks don't block CLI output.
    """
    from ..utils import is_compile_commands_stale

    cc = row.get("compile_commands_path", "")
    if not cc:
        return False
    stale, _ = is_compile_commands_stale(row["created_at"], cc)
    return stale
