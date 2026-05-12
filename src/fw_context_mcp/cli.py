"""fw-context CLI — index and query firmware code intelligence."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def cmd_index(args: argparse.Namespace) -> int:
    from .indexer.runner import run

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    compile_commands = Path(args.compile_commands)
    if not compile_commands.exists():
        print(f"error: {compile_commands} not found", file=sys.stderr)
        return 1

    db_dir = Path(args.db_dir) if args.db_dir else Path.home() / ".fw-context" / "index"
    import hashlib
    import subprocess

    project_root = compile_commands.parent.resolve()
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        project_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        project_id = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]

    db_path = db_dir / project_id / "index.db"

    source_roots = None
    if args.source_roots:
        source_roots = [Path(r) for r in args.source_roots]

    config_hash = run(
        compile_commands=compile_commands,
        db_path=db_path,
        source_roots=source_roots,
        project_name=args.name,
    )
    print(f"Indexed. config_hash={config_hash[:16]}…  db={db_path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    import hashlib
    import subprocess
    from .indexer.db import get_active_config, open_db, search_symbols

    project_root = Path(args.project or ".").resolve()
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        project_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        project_id = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]

    db_path = Path.home() / ".fw-context" / "index" / project_id / "index.db"
    if not db_path.exists():
        print(f"No index found at {db_path}. Run 'fw-context index' first.")
        return 1

    conn = open_db(db_path)
    cfg = get_active_config(conn, project_id)
    if not cfg:
        print("No build config indexed.")
        return 1

    results = search_symbols(conn, args.query, cfg["config_hash"], limit=args.limit)
    from pathlib import Path as P
    for r in results:
        print(f"[{r['kind']:15}] {r['qualified_name']}  @ {P(r['file_path']).name}:{r['line']}")
        if args.verbose and r["signature"]:
            print(f"               {r['signature']}")
    print(f"\n{len(results)} result(s) for '{args.query}'")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="fw-context", description="Firmware code intelligence")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    p_index = sub.add_parser("index", help="Build the symbol index from compile_commands.json")
    p_index.add_argument("compile_commands", nargs="?", default="compile_commands.json")
    p_index.add_argument("--source-roots", nargs="+", metavar="DIR")
    p_index.add_argument("--db-dir", metavar="DIR")
    p_index.add_argument("--name", metavar="NAME", help="Project name")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search indexed symbols")
    p_search.add_argument("query")
    p_search.add_argument("--project", metavar="DIR")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))
