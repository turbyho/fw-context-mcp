"""fw-context CLI — index and query firmware code intelligence."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_CLAUDE_MD_INSTRUCTIONS = """\
<!-- fw-context -->
## fw-context — Build-aware code intelligence

`fw-context` MCP tools are available globally. **Use them only in embedded firmware
projects built with Mbed OS, Zephyr, or PlatformIO.** Do not use in Python, JS,
Go, or other projects — the index is built from `compile_commands.json` and only
covers C/C++ translation units.

### When to use

- Looking up a symbol definition or declaration → `lookup_symbol(name)`
- Searching for functions/classes by topic → `search_code(query)` or `smart_search(query)`
- Understanding what a function does → `explain_symbol(name)` (calls local Ollama)
- Checking build metadata or index freshness → `get_active_build()`

### Workflow

1. Call `get_active_build()` first to confirm the index exists and is not stale.
   If `stale: true`, remind the user to run `fw-context index` in the project root.
2. Prefer `lookup_symbol` for exact names, `search_code` for keyword search,
   `smart_search` for natural-language queries.
3. Use `check_ollama()` before the first `explain_symbol` or `smart_search` call in
   a session to verify the local model is available.
4. `explain_symbol` reads source context and calls the local Ollama model — it may
   take 10–30 s. Do not call it in a loop over many symbols.

### Index setup (first use in a project)

```bash
# Mbed OS
bear -- python3 build_app.py --profile release --type DEV
fw-context index

# Zephyr
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
fw-context index build/compile_commands.json

# PlatformIO
pio run --target compiledb
fw-context index
```
<!-- /fw-context -->"""

_OPENCODE_RULES_INSTRUCTIONS = """\
# fw-context — Build-aware code intelligence

`fw-context` MCP tools are available globally. **Use them only in embedded firmware
projects built with Mbed OS, Zephyr, or PlatformIO.** Do not use in Python, JS,
Go, or other projects — the index is built from `compile_commands.json` and only
covers C/C++ translation units.

## When to use

- Looking up a symbol definition or declaration → `lookup_symbol(name)`
- Searching for functions/classes by topic → `search_code(query)` or `smart_search(query)`
- Understanding what a function does → `explain_symbol(name)` (calls local Ollama)
- Checking build metadata or index freshness → `get_active_build()`

## Workflow

1. Call `get_active_build()` first to confirm the index exists and is not stale.
   If `stale: true`, remind the user to run `fw-context index` in the project root.
2. Prefer `lookup_symbol` for exact names, `search_code` for keyword search,
   `smart_search` for natural-language queries.
3. Use `check_ollama()` before the first `explain_symbol` or `smart_search` call in
   a session to verify the local model is available.
4. `explain_symbol` reads source context and calls the local Ollama model — it may
   take 10–30 s. Do not call it in a loop over many symbols.

## Index setup (first use in a project)

```bash
# Mbed OS
bear -- python3 build_app.py --profile release --type DEV
fw-context index

# Zephyr
west build -b <board> -- -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
fw-context index build/compile_commands.json

# PlatformIO
pio run --target compiledb
fw-context index
```
"""


def cmd_index(args: argparse.Namespace) -> int:
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

    import hashlib, subprocess
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        project_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    except Exception:
        project_id = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]

    db_path = cfg.index.db_dir / project_id / "index.db"

    source_roots = [Path(r) for r in args.source_roots] if args.source_roots else cfg.source_root_paths(project_root)
    exclude_paths = cfg.exclude_root_paths(project_root)

    config_hash = run(
        compile_commands=compile_commands,
        db_path=db_path,
        source_roots=source_roots,
        exclude_paths=exclude_paths,
        project_name=args.name or cfg.project.name,
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


def cmd_init(args: argparse.Namespace) -> int:
    import shutil
    import subprocess

    mcp_bin = shutil.which("fw-context-mcp") or str(Path(sys.executable).parent / "fw-context-mcp")
    ok = True

    # 1. Claude Code — global MCP registration
    claude_bin = shutil.which("claude")
    if claude_bin:
        result = subprocess.run(
            [claude_bin, "mcp", "add", "--scope", "user", "fw-context", mcp_bin],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"[ok] Claude Code: fw-context registered ({mcp_bin})")
        else:
            # already registered is not a fatal error
            msg = (result.stderr or result.stdout).strip()
            if "already" in msg.lower() or "exists" in msg.lower():
                print(f"[ok] Claude Code: fw-context already registered")
            else:
                print(f"[warn] Claude Code: {msg}", file=sys.stderr)
    else:
        print("[skip] Claude Code: 'claude' not found in PATH — register manually:")
        print(f"       claude mcp add --scope user fw-context {mcp_bin}")

    # 2. ~/.claude/CLAUDE.md — insert/replace fw-context section
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    _update_marked_section(claude_md, _CLAUDE_MD_INSTRUCTIONS, "fw-context")
    print(f"[ok] {claude_md}: fw-context section updated")

    # 3. OpenCode rules
    opencode_rules = Path.home() / ".config" / "opencode" / "rules"
    if opencode_rules.is_dir():
        rule_file = opencode_rules / "fw-context.md"
        rule_file.write_text(_OPENCODE_RULES_INSTRUCTIONS, encoding="utf-8")
        print(f"[ok] {rule_file}: written")
    else:
        print("[skip] OpenCode rules dir not found — skipping OpenCode setup")

    if ok:
        print("\nSetup complete. Restart your AI assistant to pick up the new MCP server.")
    return 0 if ok else 1


def _update_marked_section(path: Path, content: str, marker: str) -> None:
    """Insert or replace a <!-- marker --> ... <!-- /marker --> block in a markdown file."""
    start_tag = f"<!-- {marker} -->"
    end_tag = f"<!-- /{marker} -->"

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if start_tag in existing and end_tag in existing:
        # Replace the existing block
        before = existing[:existing.index(start_tag)]
        after = existing[existing.index(end_tag) + len(end_tag):]
        updated = before.rstrip("\n") + "\n\n" + content + "\n" + after.lstrip("\n")
    else:
        # Append
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
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search indexed symbols")
    p_search.add_argument("query")
    p_search.add_argument("--project", metavar="DIR")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_init = sub.add_parser("init", help="Register fw-context in Claude Code and OpenCode globally")
    p_init.set_defaults(func=cmd_init)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))
