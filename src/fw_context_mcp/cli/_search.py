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

    Prints a table with project name, root path, the number of builds, the
    symbol and file counts, and the time of the newest build.  A project
    whose schema or row format is old, or whose compile_commands.json of a
    build changed, is marked with ``*``.  ``fw-context status`` gives each
    reason.

    WHY the MCP tool ``list_projects``: the CLI computed its own counts and
    its own staleness, from the newest build only.  The table now shows
    what the MCP tool gives, thus the two cannot disagree.  A project of
    several builds shows no counts (``-``): a file that two builds compile
    is in the index of each, thus a sum counts it two times.  ``fw-context
    status`` gives the counts of each build.
    """
    from ..mcp.handlers.maintenance import list_projects
    from ..utils import fmt_count, truncate_path_middle

    projects = list_projects()
    if len(projects) == 1 and "info" in projects[0]:
        print(projects[0]["info"])
        return 0

    # --- Collect all rows first so we can compute column widths ---
    header = ("PROJECT", "PATH", "BUILDS", "SYMBOLS", "FILES", "INDEXED")
    rows: list[tuple[str, ...]] = []

    for project in projects:
        if "error" in project:
            print(f"[error] {project['db']}: {project['error']}", file=sys.stderr)
            continue
        name = project["name"] or project["project_id"]
        if project["reindex_needed"]:
            name += " *"
        rows.append((
            name,
            project["root_path"] or "",
            str(project["build_count"]),
            "-" if project["symbol_count"] is None else fmt_count(project["symbol_count"]),
            "-" if project["file_count"] is None else fmt_count(project["file_count"]),
            project["indexed_at"] or "",
        ))
        if args.verbose:
            rows.append(("", f"  db={project['db']}", "", "", "", ""))

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
            # Left-align everything except the counts (right-align)
            align = ">" if header[i] in ("BUILDS", "SYMBOLS", "FILES") else "<"
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
    """Show the state of the index of the current project, and of each of its builds.

    WHY the MCP tool ``get_project_status``: the CLI showed the counts of
    the newest build only, and called the index stale only when that
    build's compile_commands.json was newer.  It did not show the other
    builds, an old schema or row format, the modified files, or an index
    run that is in progress or scheduled.  The command now prints what the
    MCP tool gives, with the same message and the same reasons.
    """
    from ..mcp.handlers.maintenance import get_project_status
    from ..mcp.shared.stale import build_label
    from ..utils import fmt_count, resolve_project_root

    project_root = resolve_project_root(args.project)
    state = get_project_status(project_root=str(project_root))
    if "error" in state:
        print(state["error"], file=sys.stderr)
        return 1
    if state["status"] in ("not_initialized", "no_index"):
        print(state["index_message"])
        return 1

    print(f"Project : {state['project_root']}")
    print(f"Status  : {state['status']}")
    print(f"          {state['index_message']}")
    builds = state.get("builds") or []
    print(f"Builds  : {len(builds)}")
    names = [build_label(b) or "(no name)" for b in builds]
    # The state of a build: its status, and the state of the index run
    # when the index of that build is not current.
    words = [f"{b['status']} ({b['reindex_state']})" if b["reindex_state"] else b["status"] for b in builds]
    name_width = max((len(name) for name in names), default=0)
    word_width = max((len(word) for word in words), default=0)
    for build, name, word in zip(builds, names, words, strict=True):
        print(
            f"  {name:<{name_width}}  {word:<{word_width}}  symbols={fmt_count(build['symbol_count'])}"
            f"  files={fmt_count(build['file_count'])}  indexed={build['indexed_at']}"
        )
        for reason in build["reasons"]:
            # A project of one build gives the text of its reason in the
            # message too; the line above already holds it.
            if reason not in state["index_message"]:
                print(f"      {reason}")
    for missing in state.get("declared_not_indexed") or []:
        print(f"  {missing['variant']}  not indexed — run '{missing['command']}'")
    return 0
