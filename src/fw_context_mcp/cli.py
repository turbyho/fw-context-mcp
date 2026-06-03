"""fw-context CLI — index and query firmware code intelligence."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC
from pathlib import Path

_CLAUDE_MD_INSTRUCTIONS = """\
<!-- fw-context -->
## fw-context — Build-aware code intelligence

`fw-context` MCP tools are available globally. **Use them only in embedded firmware
projects built with Mbed OS, Zephyr, or PlatformIO.** Do not use in Python, JS,
Go, or other projects — the index is built from `compile_commands.json` and only
covers C/C++ translation units.

### lean-ctx compatibility

Do NOT use lean-ctx tools (`ctx_read`, `ctx_shell`, `ctx_search`, `ctx_tree`,
`ctx_edit`, `ctx_multi_read`) to process or display results from fw-context code
queries (`search_code`, `lookup_symbol`, `explain_symbol`, `smart_search`,
`reindex_file`). C/C++ code results must stay uncompressed — use native tools.

When working on the `fw-context-mcp` source code itself
(`~/dev/sw/work/tools/fw-context-mcp/`), do NOT use lean-ctx for C/C++ code.

### When to use

**Code search:**
- Looking up a symbol definition or declaration → `lookup_symbol(name)`
- Searching for functions/classes by topic → `search_code(query)` or `smart_search(query)`
- Understanding what a function does → `explain_symbol(name)` (uses local Ollama
   when available; falls back to returning source + prompt for you to explain)
- Checking build metadata or index freshness → `get_active_build()`

**Index administration:**
- List all indexed projects → `list_projects()`
- Delete index for a project (e.g. after toolchain change) → `reset_index(project_root?)`;
  always call without `confirm` first (dry-run), then with `confirm=True` to proceed
- Check Ollama model availability → `check_ollama()`

### Workflow

1. Call `get_active_build()` first to confirm the index exists and is not stale.
   If `stale: true`, remind the user to run `fw-context index` in the project root.
2. Prefer `lookup_symbol` for exact names, `search_code` for keyword search,
   `smart_search` for natural-language queries.
3. Use `check_ollama()` before the first `explain_symbol` or `smart_search` call in
   a session to verify the local model is available.
4. `explain_symbol` reads source context and calls the local Ollama model — it may
   take 10–30 s. Do not call it in a loop over many symbols.
5. When `explain_symbol` returns a `warning` with `source` and `explain_prompt`
   instead of `explanation`, Ollama is unavailable — answer the prompt yourself.

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

## lean-ctx compatibility

Do NOT use lean-ctx tools (`ctx_read`, `ctx_shell`, `ctx_search`, `ctx_tree`,
`ctx_edit`, `ctx_multi_read`) to process or display results from fw-context code
queries (`search_code`, `lookup_symbol`, `explain_symbol`, `smart_search`,
`reindex_file`). C/C++ code results must stay uncompressed — use native tools.

When working on the `fw-context-mcp` source code itself
(`~/dev/sw/work/tools/fw-context-mcp/`), do NOT use lean-ctx at C/C++ code.

## When to use

**Code search:**
- Looking up a symbol definition or declaration → `lookup_symbol(name)`
- Searching for functions/classes by topic → `search_code(query)` or `smart_search(query)`
- Understanding what a function does → `explain_symbol(name)` (uses local Ollama
   when available; falls back to returning source + prompt for you to explain)
- Checking build metadata or index freshness → `get_active_build()`

**Index administration:**
- List all indexed projects → `list_projects()`
- Delete index for a project (e.g. after toolchain change) → `reset_index(project_root?)`;
  always call without `confirm` first (dry-run), then with `confirm=True` to proceed
- Check Ollama model availability → `check_ollama()`

## Workflow

1. Call `get_active_build()` first to confirm the index exists and is not stale.
   If `stale: true`, remind the user to run `fw-context index` in the project root.
2. Prefer `lookup_symbol` for exact names, `search_code` for keyword search,
   `smart_search` for natural-language queries.
3. Use `check_ollama()` before the first `explain_symbol` or `smart_search` call in
   a session to verify the local model is available.
4. `explain_symbol` reads source context and calls the local Ollama model — it may
   take 10–30 s. Do not call it in a loop over many symbols.
5. When `explain_symbol` returns a `warning` with `source` and `explain_prompt`
   instead of `explanation`, Ollama is unavailable — answer the prompt yourself.

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
    import shutil
    import subprocess

    from .config.settings import _ensure_project_config

    mcp_bin = shutil.which("fw-context-mcp")
    if not mcp_bin:
        for candidate in [
            Path(sys.executable).parent / "fw-context-mcp",
            Path.home() / ".fw-context" / ".venv" / "bin" / "fw-context-mcp",
        ]:
            if candidate.exists():
                mcp_bin = str(candidate)
                break
    if not mcp_bin:
        print("[error] fw-context-mcp not found in PATH. "
              "Run: uv pip install ~/.fw-context/src/", file=sys.stderr)
        return 1
    ok = False

    # 1. Claude Code — global MCP registration
    claude_bin = shutil.which("claude")
    if claude_bin:
        result = subprocess.run(
            [claude_bin, "mcp", "add", "--scope", "user", "fw-context", mcp_bin],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"[ok] Claude Code: fw-context registered ({mcp_bin})")
            ok = True
        else:
            # already registered is not a fatal error
            msg = (result.stderr or result.stdout).strip()
            if "already" in msg.lower() or "exists" in msg.lower():
                print("[ok] Claude Code: fw-context already registered")
                ok = True
            else:
                print(f"[warn] Claude Code: {msg}", file=sys.stderr)
    else:
        print("[skip] Claude Code: 'claude' not found in PATH — register manually:")
        print(f"       claude mcp add --scope user fw-context {mcp_bin}")

    # 2. ~/.claude/CLAUDE.md — insert/replace fw-context section
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    _update_marked_section(claude_md, _CLAUDE_MD_INSTRUCTIONS, "fw-context")
    print(f"[ok] {claude_md}: fw-context section updated")
    ok = True

    # 3. OpenCode rules
    opencode_rules = Path.home() / ".config" / "opencode" / "rules"
    if opencode_rules.is_dir():
        rule_file = opencode_rules / "fw-context.md"
        rule_file.write_text(_OPENCODE_RULES_INSTRUCTIONS, encoding="utf-8")
        print(f"[ok] {rule_file}: written")
        ok = True
    else:
        print("[skip] OpenCode rules dir not found — skipping OpenCode setup")

    # 4. Project-level config — create .fw-context/config.toml in cwd
    cwd = Path.cwd()
    proj_config = _ensure_project_config(cwd)
    print(f"[ok] {proj_config}: project config ready — edit source_roots, excludes, etc.")

    if ok:
        print("\nSetup complete. Restart your AI assistant to pick up the new MCP server.")
    else:
        print("\nNo AI assistants were configured. Try manual setup — see README-MCP.md#integration.", file=sys.stderr)
    return 0 if ok else 1


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

    p_init = sub.add_parser("init", help="Register fw-context in Claude Code and OpenCode globally")
    p_init.set_defaults(func=cmd_init)

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
