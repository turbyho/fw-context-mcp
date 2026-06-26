"""fw-context CLI — index and query firmware code intelligence."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__


def cmd_index(args: argparse.Namespace) -> int:
    """Build or rebuild the symbol index from compile_commands.json.

    By default, generates ``compile_commands.json`` from a clean build
    (auto-detecting Mbed OS / Zephyr / PlatformIO), then parses every
    translation unit with libclang and stores symbols, references,
    and optionally embeddings + LLM analysis in SQLite.

    Pass an explicit ``compile_commands.json`` path or ``--no-build`` to
    skip the build step and use an existing compilation database.
    """
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

    # Resolve compile_commands.json path
    # Explicit path in positional arg implies --no-build
    explicit_cc = bool(args.compile_commands)

    if args.no_build or explicit_cc:
        # Use existing compile_commands.json
        compile_commands = Path(args.compile_commands) if explicit_cc else cfg.index.compile_commands
        if not compile_commands.is_absolute():
            compile_commands = (project_root / compile_commands).resolve()
        if not compile_commands.exists():
            print(f"error: {compile_commands} not found", file=sys.stderr)
            print("  Run 'fw-context index' without arguments to build and index automatically.", file=sys.stderr)
            return 1

        # Warn if compile_commands.json looks incomplete
        from .indexer.build import check_completeness
        for warning in check_completeness(compile_commands, project_root):
            print(f"warning: {warning}", file=sys.stderr)
    else:
        # Default: generate compile_commands.json from a freshly built build
        from .indexer.build import generate_compile_commands

        build_cfg = cfg.build
        if args.no_clean:
            build_cfg.clean = False
        try:
            compile_commands = generate_compile_commands(project_root, build_cfg)
            print(f"Generated: {compile_commands}")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
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
        index_refs=False if args.no_refs else cfg.index.index_refs,
        index_embeddings=(
            False if getattr(args, 'no_embeddings', False)
            else getattr(args, 'embeddings', None) or cfg.index.index_embeddings
        ),
        analyze_symbols=(
            False if getattr(args, 'no_analyze', False)
            else getattr(args, 'analyze', False) or cfg.llm.analyze_symbols
        ),
        analyze_files=(
            False if getattr(args, 'no_analyze', False)
            else cfg.llm.analyze_files
        ),
        analyze_overrides=True,
        project_root=project_root,
        project_id=project_id,
        llm_config=cfg.llm,
    )
    print(f"Indexed. config_hash={config_hash[:16]}…  db={db_path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Full-text search over indexed symbols and print results to stdout.

    Queries the FTS5 index for symbols matching the given keywords
    and prints each hit with its kind, qualified name, file, and line.
    """
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
    """List all indexed firmware projects found under the configured index directory.

    Prints each project's name, root path, symbol count, file count,
    and index timestamp. Marks stale projects (compile_commands.json
    changed since last index).
    """
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
    """Delete the symbol index for a project after confirmation.

    Shows project info (root, DB path, symbol count, index date) and
    prompts for confirmation before deleting the SQLite database file.
    Use ``-y``/``--yes`` to skip the prompt.
    """
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
    """Show index status for the current project.

    Prints project root, symbol count, file count, index timestamp,
    and whether the index is stale (compile_commands.json changed
    since last index).
    """
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

    Returns False on any error so staleness checks don't block CLI output.
    """
    import os

    from .utils import MTIME_TOLERANCE_S as _MTS
    try:
        cc = row["compile_commands_path"]
        if not cc or not Path(cc).exists():
            return False
        cc_mtime = os.path.getmtime(cc)
        indexed_at = datetime.fromisoformat(row["created_at"]).replace(tzinfo=UTC)
        return cc_mtime > indexed_at.timestamp() + _MTS
    except Exception:
        return False


def cmd_init(args: argparse.Namespace) -> int:
    """Register fw-context with AI assistants and inject usage instructions.

    Per-tool injection with inheritance awareness, collision detection,
    dry-run preview, and ``--list-tools`` discovery.

    Registers the ``fw-context-mcp`` MCP server with each detected tool's CLI
    and writes fw-context usage instructions into the tool's configuration files
    (CLAUDE.md, AGENTS.md, etc.). Respects tool inheritance — e.g. tools based
    on Claude Code read the parent's instructions automatically.

    Writes ``.fw-context/config.toml`` and ``.fw-context/local.toml`` in the
    project root when using project-scoped injection.
    """
    import shutil

    from .config.settings import _ensure_project_config, _ensure_project_local_config
    from .config.tools import TOOLS, check_target

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
                    print("  [skip] Nothing to do. Use --force to inject anyway.")
                    continue
                print("  [force] Injecting despite inheritance...")
            else:
                print(f"  [warn] Inherits from {parent_name} but parent NOT DETECTED")
                print("  [info] Injecting instructions anyway...")

        # MCP registration (only if not --instructions-only)
        if not args.instructions_only and tool.mcp_registration and mcp_bin:
            _register_mcp(tool, mcp_bin)

        # Instruction injection
        if not tool.targets:
            if not tool.mcp_registration:
                print("  [skip] No instruction targets defined")
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
                print("         Use --force to overwrite, or remove the existing section manually")
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
        local_config = _ensure_project_local_config(project_root)
        print(f"\n[ok] {proj_config}: shared project config ready — edit source_roots, excludes, etc. (commit to git)")
        print(f"[ok] {local_config}: local developer config ready — edit ollama_url, model, etc. (gitignore)")

    if warnings:
        print("\nWarnings:")
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
    """Register fw-context as an MCP server in *tool*'s CLI configuration.

    *tool* is an ``AiTool`` instance; *mcp_bin* is the path or name of the
    ``fw-context-mcp`` executable. No-op when *tool* has no ``mcp_registration``.
    """
    """Register fw-context as an MCP server with a tool's CLI."""
    import shutil
    import subprocess


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
        msg_lower = msg.lower()
        if "already registered" in msg_lower or "already exists" in msg_lower:
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
        # Replace the existing marked block (keep markers for idempotency)
        before = existing[:existing.index(start_tag)]
        after = existing[existing.index(end_tag) + len(end_tag):]
        updated = (
            before.rstrip("\n") + "\n\n"
            + start_tag + "\n" + content + "\n" + end_tag + "\n"
            + after.lstrip("\n")
        )
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
        updated = (
            existing.rstrip("\n") + ("\n\n" if existing.strip() else "")
            + start_tag + "\n" + content + "\n" + end_tag + "\n"
        )

    path.write_text(updated, encoding="utf-8")


def cmd_export(args: argparse.Namespace) -> int:
    """Export the symbol index as portable JSON (``_format: "fw-context-export/1"``).

    Writes all symbols and optionally cross-references to a JSON file
    or stdout. The format includes project metadata, all symbol records,
    and reference counts.
    """
    import json

    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.build import detect_build_system
    from .indexer.db import count_refs, get_active_config, open_db

    project_root = Path(args.project or ".").resolve()
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {project_root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    conn = open_db(db_path)
    try:
        build_cfg = get_active_config(conn, project_id)
        if not build_cfg:
            print(f"No build config indexed for {project_root}.", file=sys.stderr)
            return 1
        config_hash = build_cfg["config_hash"]

        output: dict = {
            "_format": "fw-context-export/1",
            "project": {
                "id": project_id,
                "root": str(project_root),
                "build_system": detect_build_system(project_root),
                "compile_commands": build_cfg["compile_commands_path"],
                "indexed_at": build_cfg["created_at"],
            },
            "config_hash": config_hash,
        }

        # Symbols
        symbols = conn.execute(
            """SELECT name, qualified_name, kind, file_path, line, col, end_line,
                      is_definition, signature, docstring
               FROM symbols WHERE config_hash=? ORDER BY kind, name""",
            (config_hash,),
        ).fetchall()
        output["symbols"] = [dict(r) for r in symbols]
        output["symbol_count"] = len(symbols)

        # References (optional)
        if not args.no_refs:
            ref_count = count_refs(conn, config_hash)
            if ref_count > 0:
                refs = conn.execute(
                    """SELECT r.to_usr, r.from_file, r.from_line, r.ref_kind,
                              caller.name AS caller_name,
                              callee.name AS callee_name
                       FROM refs r
                       LEFT JOIN symbols caller ON caller.usr = r.from_usr AND caller.config_hash = r.config_hash
                       LEFT JOIN symbols callee ON callee.usr = r.to_usr AND callee.config_hash = r.config_hash
                       WHERE r.config_hash = ?
                       ORDER BY r.from_file, r.from_line""",
                    (config_hash,),
                ).fetchall()
                output["references"] = [dict(r) for r in refs]
                output["reference_count"] = ref_count
            else:
                output["reference_count"] = 0
    finally:
        conn.close()

    json_text = json.dumps(output, indent=2, ensure_ascii=False, default=str)

    if args.output:
        Path(args.output).write_text(json_text, encoding="utf-8")
        print(f"Exported {output['symbol_count']} symbols"
              f"{' + ' + str(output.get('reference_count', 0)) + ' references' if 'reference_count' in output else ''}"
              f" → {args.output}")
    else:
        print(json_text)

    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch project source files and auto-reindex on changes.

    Monitors the project directory for changes to ``.c``, ``.cpp``, ``.h``,
    and ``.hpp`` files and runs ``reindex_file`` on each modified file.
    Changes are debounced so rapid saves (e.g. from an IDE) trigger at most
    one re-index per *debounce_ms* interval per file.
    """
    import time
    from collections import defaultdict

    from watchfiles import watch

    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.compile_commands import parse as parse_cc
    from .indexer.db import get_active_config, open_db
    from .indexer.ops import store_symbols_for_unit

    root = Path(args.project or ".").resolve()
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    print(f"👀 Watching {root} for changes (debounce={args.debounce}ms)...")
    print("   Press Ctrl+C to stop.")

    debounce_s = args.debounce / 1000.0
    pending: dict[str, float] = defaultdict(float)

    try:
        for changes in watch(root, debounce=debounce_s, recursive=True):
            source_exts = {".c", ".cpp", ".h", ".hpp"}
            changed_files = {
                (Path(root), change)
                for change, root in changes
                if Path(root).suffix.lower() in source_exts
                and "__pycache__" not in root
                and ".git/" not in root
            }

            if not changed_files:
                continue

            now = time.monotonic()
            for path, _ in changed_files:
                pending[str(path)] = now

            # Process files whose debounce window has elapsed
            ready = [p for p, t in pending.items() if now - t >= debounce_s]
            if not ready:
                continue

            # Reload compile_commands to pick up any new TUs
            try:
                conn = open_db(db_path)
                build_cfg = get_active_config(conn, project_id)
                if not build_cfg:
                    conn.close()
                    continue
                cc_path = Path(build_cfg["compile_commands_path"])
                units = list(parse_cc(cc_path))
                config_hash = build_cfg["config_hash"]
                source_roots = cfg.source_root_paths(root)
                exclude_paths = cfg.exclude_root_paths(root)
            except Exception as e:
                print(f"  ⚠ Failed to reload build config: {e}")
                conn.close()
                continue

            for fp in ready:
                del pending[fp]
                try:
                    target = Path(fp).resolve()
                    matching = [u for u in units if Path(u.file).resolve() == target]
                    if not matching:
                        continue
                    total = 0
                    for unit in matching:
                        syms_added, _ = store_symbols_for_unit(
                            conn, unit, config_hash, root,
                            source_roots=source_roots,
                            exclude_paths=exclude_paths,
                            index_refs=cfg.index.index_refs,
                        )
                        total += syms_added
                    conn.commit()
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except Exception:
                        pass
                    if total > 0:
                        rel = target.relative_to(root)
                        print(f"  ✓ reindexed {rel} ({total} symbols)")
                except Exception as e:
                    print(f"  ✗ {fp}: {e}")

            conn.close()

    except KeyboardInterrupt:
        print("\nStopped.")

    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Re-run LLM symbol analysis on an existing index (idempotent).

    Requires Ollama (or LM Studio) running and ``[llm] enabled = true``
    in config. Re-generates per-symbol summaries, inputs/outputs analysis,
    file-level summaries (if ``[llm] analyze_files`` is enabled), and
    method override relationships. Existing analysis rows are skipped
    (idempotent) — only unanalyzed symbols are processed.
    """
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.db import get_active_config, open_db
    from .indexer.runner import _build_file_analysis, _build_llm_analysis, _build_overrides

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    project_root = Path(args.project or ".").resolve()
    cfg = load_config(project_root=project_root)

    if not cfg.llm.enabled:
        print("error: [llm] enabled = false in config. Enable Ollama first.", file=sys.stderr)
        return 1

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"

    if not db_path.exists():
        print(f"No index found for {project_root}. Run 'fw-context index' first.", file=sys.stderr)
        return 1

    conn = open_db(db_path)
    try:
        build_cfg = get_active_config(conn, project_id)
        if not build_cfg:
            print("No build config indexed.", file=sys.stderr)
            return 1
        config_hash = build_cfg["config_hash"]
    finally:
        conn.close()

    # Re-open connection for the analysis (uses its own transactions)
    conn = open_db(db_path)
    try:
        _build_llm_analysis(conn, config_hash, cfg.llm, db_path.parent)
        if cfg.llm.analyze_files:
            _build_file_analysis(conn, config_hash, cfg.llm, db_path.parent)
        _build_overrides(conn, config_hash, db_path.parent)
        conn.commit()
    finally:
        conn.close()

    print(f"LLM analysis complete for {project_root}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print version and exit."""
    from . import __version__
    print(f"fw-context-mcp {__version__}")
    return 0


def main() -> None:
    """Entry point for the ``fw-context`` CLI — dispatches subcommands.

    Subcommands: index, search, list, reset, status, init, export, watch,
    analyze, version. Parses arguments via argparse and calls the
    corresponding ``cmd_*`` handler.
    """
    parser = argparse.ArgumentParser(prog="fw-context", description="Firmware code intelligence")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--version",
        action="version",
        version=f"fw-context-mcp {__version__}",
        help="Show version and exit",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_index = sub.add_parser("index", help="Build the symbol index from compile_commands.json (generates it from a clean build by default)")
    p_index.add_argument("compile_commands", nargs="?", default=None, metavar="compile_commands.json",
                         help="Use an existing compile_commands.json (skips build)")
    p_index.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_index.add_argument("--no-build", action="store_true", help="Skip build — use existing compile_commands.json")
    p_index.add_argument("--no-clean", action="store_true", help="Skip clean build (incremental — may produce incomplete index)")
    p_index.add_argument("--source-roots", nargs="+", metavar="DIR")
    p_index.add_argument("--name", metavar="NAME", help="Project name override")
    p_index.add_argument("--no-refs", action="store_true", help="Skip cross-reference indexing (on by default)")
    p_index.add_argument("--no-embeddings", action="store_true", dest="no_embeddings", help="Skip embedding generation")
    p_index.add_argument("--embeddings", action="store_true", dest="embeddings", default=None, help="Generate symbol embeddings (default)")
    p_index.add_argument("--analyze", action="store_true", dest="analyze", default=False,
                         help="Generate LLM-based symbol analysis (summary, inputs, outputs)")
    p_index.add_argument("--no-analyze", action="store_true", dest="no_analyze", help="Skip LLM analysis generation")
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

    p_export = sub.add_parser("export", help="Export the symbol index as JSON")
    p_export.add_argument("--project", metavar="DIR")
    p_export.add_argument("-o", "--output", metavar="PATH", help="Output file (default: stdout)")
    p_export.add_argument("--no-refs", action="store_true", help="Omit cross-references")
    p_export.set_defaults(func=cmd_export)

    p_watch = sub.add_parser("watch", help="Watch project files and auto-reindex on changes")
    p_watch.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_watch.add_argument("--debounce", type=int, default=2000, metavar="MS",
                         help="Debounce delay in ms (default: 2000)")
    p_watch.set_defaults(func=cmd_watch)

    p_analyze = sub.add_parser("analyze", help="Re-run LLM symbol analysis on existing index (idempotent)")
    p_analyze.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_analyze.set_defaults(func=cmd_analyze)

    p_version = sub.add_parser("version", help="Show version information")
    p_version.set_defaults(func=cmd_version)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
