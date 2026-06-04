"""fw-context CLI — index and query firmware code intelligence."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC
from pathlib import Path


def cmd_index(args: argparse.Namespace) -> int:
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.runner import run

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    project_root = Path(args.project or ".").resolve()
    cfg = load_config(project_root=project_root)

    compile_commands = Path(args.compile_commands) if args.compile_commands else cfg.index.compile_commands
    if not compile_commands.is_absolute():
        compile_commands = (project_root / compile_commands).resolve()
    if not compile_commands.exists():
        print(f"error: {compile_commands} not found", file=sys.stderr)
        return 1

    project_id = derive_project_id(project_root)

    db_path = cfg.index.db_dir / project_id / "index.db"

    source_roots = [Path(r) for r in args.source_roots] if args.source_roots else cfg.source_root_paths(project_root)
    exclude_paths = cfg.exclude_root_paths(project_root)

    config_hash = run(
        compile_commands=compile_commands,
        db_path=db_path,
        source_roots=source_roots,
        exclude_paths=exclude_paths,
        project_name=args.name or cfg.project.name,
        index_refs=args.refs or cfg.index.index_refs,
        index_embeddings=(
            False if getattr(args, 'no_embeddings', False)
            else getattr(args, 'embeddings', None) or cfg.index.index_embeddings
        ),
        project_root=project_root,
        project_id=project_id,
        llm_config=cfg.llm,
    )
    print(f"Indexed. config_hash={config_hash[:16]}…  db={db_path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.db import get_active_config, open_db, search_symbols

    project_root = Path(args.project or ".").resolve()
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
    from .config import load as load_config
    from .indexer.db import get_all_projects, open_db

    cfg = load_config()
    index_dir = cfg.index.db_dir
    db_files = sorted(index_dir.glob("*/index.db")) if index_dir.exists() else []

    if not db_files:
        print(f"No indexed projects under {index_dir}.")
        return 0

    for db_path in db_files:
        try:
            conn = open_db(db_path)
            rows = get_all_projects(conn)
            for r in rows:
                stale_marker = " [STALE]" if _cli_is_stale(r) else ""
                print(f"{r['name'] or r['project_id']}  {r['root_path']}{stale_marker}")
                print(f"  symbols={r['symbol_count']}  files={r['file_count']}  indexed={r['created_at']}")
                if args.verbose:
                    print(f"  db={db_path}")
                    print(f"  compile_commands={r['compile_commands_path']}")
        except Exception as e:
            print(f"[error] {db_path}: {e}", file=sys.stderr)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.db import get_active_config, open_db

    project_root = Path(args.project or ".").resolve()
    cfg = load_config(project_root=project_root)

    project_id = derive_project_id(project_root)

    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        print(f"No index found for {project_root}.")
        return 1

    conn = open_db(db_path)
    active = get_active_config(conn, project_id)
    conn.close()

    sym_count = 0
    if active:
        from .indexer.db import open_db as _open
        sym_count = _open(db_path).execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash=?",
            (active["config_hash"],),
        ).fetchone()[0]

    print(f"Project : {project_root}")
    print(f"DB      : {db_path}")
    if active:
        print(f"Symbols : {sym_count}  indexed={active['created_at']}")

    if not args.yes:
        answer = input("Delete index? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 0

    db_path.unlink()
    print("Index deleted. Run 'fw-context index' to rebuild.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    import os
    from datetime import datetime

    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.db import get_active_config, open_db

    project_root = Path(args.project or ".").resolve()
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

    sym_count = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (active["config_hash"],)
    ).fetchone()[0]
    file_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE config_hash=?", (active["config_hash"],)
    ).fetchone()[0]

    stale = False
    cc = active["compile_commands_path"]
    if cc and Path(cc).exists():
        cc_mtime = os.path.getmtime(cc)
        indexed_at = datetime.fromisoformat(active["created_at"]).replace(tzinfo=UTC)
        stale = cc_mtime > indexed_at.timestamp() + 1

    print(f"Project : {project_root}")
    print(f"Symbols : {sym_count}  files={file_count}")
    print(f"Indexed : {active['created_at']}{'  [STALE]' if stale else ''}")
    print(f"DB      : {db_path}")
    if stale:
        print("  compile_commands.json changed — run 'fw-context index' to update")
    return 0


def _cli_is_stale(row) -> bool:
    import os
    from datetime import datetime
    try:
        cc = row["compile_commands_path"]
        if not cc or not Path(cc).exists():
            return False
        cc_mtime = os.path.getmtime(cc)
        indexed_at = datetime.fromisoformat(row["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + 1
    except Exception:
        return False


def cmd_init(args: argparse.Namespace) -> int:
    """Register fw-context with AI assistants and inject usage instructions.

    Per-tool injection with inheritance awareness, collision detection,
    dry-run preview, and --list-tools discovery.
    """
    import shutil

    from .config.tools import TOOLS, check_target
    from .config.settings import _ensure_project_config

    # --list-tools: show supported tools and detection status
    if args.list_tools:
        print("Supported AI assistants:\n")
        for tool in TOOLS.values():
            print(f"  {tool.status()}")
        print("\nRun 'fw-context init --tool <id>' to set up a specific tool.")
        return 0

    # Resolve fw-context-mcp binary
    mcp_bin = shutil.which("fw-context-mcp")
    if not mcp_bin:
        for candidate in [
            Path(sys.executable).parent / "fw-context-mcp",
            Path.home() / ".fw-context" / ".venv" / "bin" / "fw-context-mcp",
        ]:
            if candidate.exists():
                mcp_bin = str(candidate)
                break

    # Select tools to act on
    selected: list[str] = []
    if args.tool:
        selected = [t.strip() for t in args.tool.split(",")]
        for tid in selected:
            if tid not in TOOLS:
                print(f"[error] Unknown tool: {tid}", file=sys.stderr)
                print(f"        Supported: {', '.join(TOOLS.keys())}", file=sys.stderr)
                return 1
    else:
        # Default: all detected tools
        selected = [tid for tid, t in TOOLS.items() if t.is_detected()]

    if not selected:
        print("No AI assistants detected. Use --list-tools to see supported tools.")
        return 1

    project_root = Path.cwd() if not args.project else Path(args.project).resolve()
    ok = False
    warnings: list[str] = []

    for tool_id in selected:
        tool = TOOLS[tool_id]
        print(f"\n── {tool.name} ({tool_id}) ──")

        # Inheritance check
        if tool.inherits_from:
            parent = TOOLS.get(tool.inherits_from)
            parent_name = parent.name if parent else tool.inherits_from
            parent_ok = parent and parent.is_detected()
            if parent_ok and tool_id in selected and tool.inherits_from in selected:
                print(f"  [info] Inherits from {parent_name} — already handled above, skipping")
                continue
            elif parent_ok:
                print(f"  [info] Inherits from {parent_name} which has fw-context instructions")
                ok = True  # Inheritance is a valid configuration — not an error
                if not args.force:
                    print(f"  [skip] Nothing to do. Use --force to inject anyway.")
                    continue
                print(f"  [force] Injecting despite inheritance...")
            else:
                print(f"  [warn] Inherits from {parent_name} but parent NOT DETECTED")
                print(f"  [info] Injecting instructions anyway...")

        # MCP registration (only if not --instructions-only)
        if not args.instructions_only and tool.mcp_registration and mcp_bin:
            _register_mcp(tool, mcp_bin)

        # Instruction injection
        if not tool.targets:
            if not tool.mcp_registration:
                print(f"  [skip] No instruction targets defined")
            continue

        for target in tool.targets:
            if target.scope == "project" and args.scope == "global":
                continue
            if target.scope == "global" and args.scope == "project":
                continue

            collision = check_target(target, project_root if target.scope == "project" else None)
            resolved = collision.path
            instructions = target.render_instructions()

            # Collision handling
            if collision.is_skillshare_managed and not args.force:
                print(f"  [warn] {resolved}: directory managed by skillshare — skipping")
                warnings.append(f"{tool.name}: {resolved} is skillshare-managed, use --force to overwrite")
                continue

            if collision.has_unmarked_content and not args.force:
                print(f"  [warn] {resolved}: found unmarked fw-context content — skipping")
                print(f"         Use --force to overwrite, or remove the existing section manually")
                warnings.append(f"{tool.name}: {resolved} has unmarked fw-context content")
                continue

            # Dry-run
            if args.dry_run:
                if collision.has_marked_section:
                    print(f"  [dry-run] {resolved}: would UPDATE marked section")
                else:
                    print(f"  [dry-run] {resolved}: would CREATE ({target.method})")
                ok = True
                continue

            # Write
            if target.method == "marked_section":
                _update_marked_section(resolved, instructions, marker="fw-context")
                if collision.has_marked_section:
                    print(f"  [ok] {resolved}: updated fw-context section")
                else:
                    print(f"  [ok] {resolved}: added fw-context section")
            elif target.method == "separate_file":
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(instructions, encoding="utf-8")
                print(f"  [ok] {resolved}: written")
            ok = True

    # Project-level config (only when something was actually done)
    if ok and not args.instructions_only and not args.dry_run:
        proj_config = _ensure_project_config(project_root)
        print(f"\n[ok] {proj_config}: project config ready — edit source_roots, excludes, etc.")

    if warnings:
        print(f"\nWarnings:")
        for w in warnings:
            print(f"  ⚠ {w}")
    if ok:
        if args.dry_run:
            print("\nDry-run complete. Run without --dry-run to apply changes.")
        else:
            print("\nSetup complete. Restart your AI assistant to pick up changes.")
    else:
        print("\nNo changes made.", file=sys.stderr)
    return 0 if ok else 1


def _register_mcp(tool, mcp_bin: str) -> None:
    """Register fw-context as an MCP server with a tool's CLI."""
    import subprocess
    import shutil
    from .config.tools import AiTool

    if not tool.mcp_registration:
        return

    cmd_str = tool.mcp_registration.replace("{bin}", mcp_bin)
    parts = cmd_str.split()
    binary = parts[0]

    if not shutil.which(binary):
        print(f"  [skip] '{binary}' not found in PATH — register manually:")
        print(f"         {cmd_str}")
        return

    result = subprocess.run(parts, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  [ok] {tool.name}: fw-context registered ({mcp_bin})")
    else:
        msg = (result.stderr or result.stdout).strip()
        if "already" in msg.lower() or "exists" in msg.lower():
            print(f"  [ok] {tool.name}: fw-context already registered")
        else:
            print(f"  [warn] {tool.name}: {msg}", file=sys.stderr)


def _update_marked_section(path: Path, content: str, marker: str) -> None:
    """Insert or replace a <!-- marker --> ... <!-- /marker --> block in a markdown file."""
    start_tag = f"<!-- {marker} -->"
    end_tag = f"<!-- /{marker} -->"

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if start_tag in existing and end_tag in existing:
        # Replace the existing marked block
        before = existing[:existing.index(start_tag)]
        after = existing[existing.index(end_tag) + len(end_tag):]
        updated = before.rstrip("\n") + "\n\n" + content + "\n" + after.lstrip("\n")
    else:
        # Remove any unmarked section with the same heading (idempotency for manual installs)
        heading_match = re.search(r'^## .+', content, re.MULTILINE)
        if heading_match:
            heading = heading_match.group()
            lines = existing.splitlines()
            result_lines: list[str] = []
            skip_until_next_h2 = False
            for line in lines:
                if not skip_until_next_h2:
                    if line.strip() == heading:
                        skip_until_next_h2 = True
                        continue
                    result_lines.append(line)
                else:
                    if line.startswith("## "):
                        skip_until_next_h2 = False
                        result_lines.append(line)
            existing = "\n".join(result_lines)
        updated = existing.rstrip("\n") + ("\n\n" if existing.strip() else "") + content + "\n"

    path.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="fw-context", description="Firmware code intelligence")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    p_index = sub.add_parser("index", help="Build the symbol index from compile_commands.json")
    p_index.add_argument("compile_commands", nargs="?", default=None, metavar="compile_commands.json")
    p_index.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_index.add_argument("--source-roots", nargs="+", metavar="DIR")
    p_index.add_argument("--name", metavar="NAME", help="Project name override")
    p_index.add_argument("--refs", action="store_true", help="Build cross-reference / call graph (find_callers, find_references)")
    p_index.add_argument("--no-embeddings", action="store_true", dest="no_embeddings", help="Skip embedding generation")
    p_index.add_argument("--embeddings", action="store_true", dest="embeddings", default=None, help="Generate symbol embeddings (default)")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search indexed symbols")
    p_search.add_argument("query")
    p_search.add_argument("--project", metavar="DIR")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_init = sub.add_parser("init", help="Register fw-context with AI assistants and inject instructions")
    p_init.add_argument("--tool", metavar="ID", help="Set up a specific tool (claude-code, opencode, kilocode, codex, cursor)")
    p_init.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    p_init.add_argument("--force", action="store_true", help="Overwrite even when collisions are detected")
    p_init.add_argument("--instructions-only", action="store_true", help="Only inject instructions, skip MCP registration")
    p_init.add_argument("--scope", choices=["global", "project"], default="global", help="Which scope to inject (default: global)")
    p_init.add_argument("--project", metavar="DIR", help="Project root (for project-scoped targets)")
    p_init.add_argument("--list-tools", action="store_true", help="List supported AI assistants and their detection status")
    p_init.set_defaults(func=cmd_init, tool=None, dry_run=False, force=False, instructions_only=False, list_tools=False)

    p_list = sub.add_parser("list", help="List all indexed projects")
    p_list.set_defaults(func=cmd_list)

    p_reset = sub.add_parser("reset", help="Delete the index for a project")
    p_reset.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_reset.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_reset.set_defaults(func=cmd_reset)

    p_status = sub.add_parser("status", help="Show index status for the current project")
    p_status.add_argument("--project", metavar="DIR")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))
