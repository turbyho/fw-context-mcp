"""fw-context CLI — index and query firmware code intelligence."""

# ruff: noqa: I001 — lazy imports in functions must stay near use sites

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__


class VerboseFormatter(logging.Formatter):
    """Structured output with phase headers for ``--verbose`` mode.

    Phase headers are emitted via ``log.info("", extra={"phase": "name"})``
    and rendered as framed separators.  Body messages are indented.  Phase
    results (single-line summaries) use ``extra={"result": True}`` to align
    timing info right after the phase header on the same line.
    """

    WIDTH: int = 60

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()

        # Phase header
        phase = getattr(record, "phase", None)
        if phase:
            header = f"── {phase} "
            padding = max(2, self.WIDTH - len(header))
            return "\n" + header + ("─" * padding)

        # Phase result (same-line summary)
        if getattr(record, "result", False):
            return f"  {msg}"

        # Regular message within a phase
        return f"  {msg}"


def cmd_index(args: argparse.Namespace) -> int:
    """Build or rebuild the symbol index from compile_commands.json.

    By default, reuses an existing ``compile_commands.json`` for fast
    incremental indexing.  When the file is missing, a clean build is
    triggered automatically (auto-detecting Mbed OS / Zephyr / PlatformIO).

    Pass ``--build`` to force a fresh build and full re-index.
    """
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.build import detect_build_system
    from .indexer.runner import run
    from .indexer.validator import is_compile_commands_stale, validate_and_fix
    from .utils import resolve_project_root

    if args.verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(VerboseFormatter())
        logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)

    # Early validation: confirm we detected a known build system
    # Prominent banner so the user can verify the right project is being indexed
    detected_system = detect_build_system(project_root)
    if detected_system:
        print(f"Project: {project_root.name}  path={project_root}  build={detected_system}")
        if detected_system == "platformio":
            from .indexer.builders.platformio import PlatformIOBuildSystem
            builder = PlatformIOBuildSystem()
            for msg in builder.ensure_dep_tracking(project_root, fix=True):
                if msg:
                    print(msg)
    else:
        print(f"Project: {project_root.name}  path={project_root}  build=unknown")

    # Resolve compile_commands.json path
    explicit_cc = bool(args.compile_commands)

    if args.build:
        # Explicit build requested — always run build
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
    elif explicit_cc:
        # Explicit compile_commands.json path given — use as-is
        compile_commands = Path(args.compile_commands)
        if not compile_commands.is_absolute():
            compile_commands = (project_root / compile_commands).resolve()
        if not compile_commands.exists():
            print(f"error: {compile_commands} not found", file=sys.stderr)
            print("  Run 'fw-context index --build' to build and index automatically.", file=sys.stderr)
            return 1

        # Warn if compile_commands.json looks incomplete
        from .indexer.build import check_completeness

        for warning in check_completeness(compile_commands, project_root):
            print(f"warning: {warning}", file=sys.stderr)
    else:
        # Default: reuse existing compile_commands.json, build only if missing
        from .indexer.build import check_completeness, generate_compile_commands

        compile_commands = cfg.index.compile_commands
        if not compile_commands.is_absolute():
            compile_commands = (project_root / compile_commands).resolve()
        if not compile_commands.exists():
            print("compile_commands.json not found, running build...", file=sys.stderr)
            try:
                compile_commands = generate_compile_commands(project_root, cfg.build)
                print(f"Generated: {compile_commands}")
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        else:
            # Warn if compile_commands.json looks incomplete
            for warning in check_completeness(compile_commands, project_root):
                print(f"warning: {warning}", file=sys.stderr)

    project_id = derive_project_id(project_root)

    db_path = cfg.index.db_dir / project_id / "index.db"

    # ── Validate build artifacts before indexing ──
    if detected_system:
        from .indexer.builders import registry as builder_registry
        builder_cls = builder_registry.get(detected_system)
        if builder_cls is not None:
            builder_instance = builder_cls()
            # Check staleness
            stale, stale_reasons = is_compile_commands_stale(compile_commands, project_root)
            if stale:
                print(f"compile_commands.json is stale ({'; '.join(stale_reasons)}).")
                if args.build:
                    print("  Rebuilding (--build flag)...")
                elif not explicit_cc:
                    print("  Run 'fw-context index --build' to regenerate.")
            # Validate artifacts
            issues = validate_and_fix(
                compile_commands, project_root, builder_instance, cfg.build, fix=True,
            )
            errors = [i for i in issues if i.severity == "error"]
            warnings_list = [i for i in issues if i.severity == "warning"]
            for w in warnings_list:
                print(f"warning: {w.message}", file=sys.stderr)
            if errors:
                for e in errors:
                    print(f"error: {e.message}", file=sys.stderr)
                if not args.build:
                    print("Run 'fw-context index --build' to rebuild and fix issues.", file=sys.stderr)
                return 1

    source_roots = [Path(r) for r in args.source_roots] if args.source_roots else cfg.source_root_paths(project_root)
    exclude_paths = cfg.exclude_root_paths(project_root)

    # Override force flag from CLI
    cs_config = cfg.cache_server
    if cs_config is not None and getattr(args, "force", False):
        from dataclasses import replace

        cs_config = replace(cs_config, force=True)

    # Pause background reindex so it releases the write lock.
    # Without this, a concurrent bg reindex holds the lock and the
    # foreground ``fw-context index`` times out after 120 s.
    pause_file = db_path.parent / "reindex.pause"
    try:
        pause_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    try:
        config_hash = run(
            compile_commands=compile_commands,
            db_path=db_path,
            source_roots=source_roots,
            exclude_paths=exclude_paths,
            project_name=args.name or cfg.project.name,
            index_refs=False if args.no_refs else cfg.index.index_refs,
            index_embeddings=(
                False
                if getattr(args, "no_embeddings", False)
                else getattr(args, "embeddings", None) or cfg.index.index_embeddings
            ),
            analyze_symbols=(
                False
                if getattr(args, "no_analyze", False)
                else getattr(args, "analyze", False) or cfg.llm.analyze_symbols
            ),
            analyze_overrides=True,
            project_root=project_root,
            project_id=project_id,
            llm_config=cfg.llm,
            cache_server_config=cs_config,
            config_header=cfg.index.config_header,
            force=args.force,
        )
        print(f"Indexed. config_hash={config_hash[:16]}…  db={db_path}")
        return 0
    finally:
        try:
            if pause_file.exists():
                content = pause_file.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    pause_file.unlink(missing_ok=True)
        except OSError:
            pass


def cmd_search(args: argparse.Namespace) -> int:
    """Full-text search over indexed symbols and print results to stdout.

    Queries the FTS5 index for symbols matching the given keywords
    and prints each hit with its kind, qualified name, file, and line.
    """
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.db import get_active_config, open_db, search_symbols
    from .utils import resolve_project_root

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
    from .utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)

    project_id = derive_project_id(project_root)

    db_path = cfg.index.db_dir / project_id / "index.db"
    if not db_path.exists():
        print(f"No index found for {project_root}.")
        return 1

    conn = open_db(db_path)
    active = get_active_config(conn, project_id)
    sym_count = 0
    if active:
        sym_count = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash=?",
            (active["config_hash"],),
        ).fetchone()[0]
    conn.close()

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
    from .utils import resolve_project_root

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


def _install_skills(
    dry_run: bool = False,
    project_root: Path | None = None,
    scope: str = "project",
) -> bool:
    """Copy fw-context skills to AI tool skills directories.

    Installs the fw-review skill globally (when scope is
    ``"global"`` or ``"all"``) and at the project level (when scope is
    ``"project"`` or ``"all"``, and ``project_root`` is provided).

    Skill directories are driven by the ``TOOLS`` registry — no tool
    paths are hardcoded here.  Project-local skills override global
    ones — if a project has its own copy the user intentionally
    customized it.
    """
    import os
    import shutil

    from . import __file__ as _pkg_init
    from .config.tools import (
        CROSS_TOOL_SKILL_DIRS_GLOBAL,
        CROSS_TOOL_SKILL_DIRS_PROJECT,
        TOOLS,
    )

    pkg_dir = Path(_pkg_init).parent
    skill_dir = pkg_dir / "data" / "skills" / "fw-review"

    if not (skill_dir / "SKILL.md").exists():
        return False

    targets: list[Path] = []
    processed: set[Path] = set()

    # Global targets — from tool definitions and cross-tool standard dirs
    if scope in ("global", "all"):
        for tool in TOOLS.values():
            for dir_template in tool.skill_dirs_global:
                resolved = Path(os.path.expanduser(dir_template)) / "fw-review"
                if resolved not in processed:
                    targets.append(resolved)
                    processed.add(resolved)
        for dir_template in CROSS_TOOL_SKILL_DIRS_GLOBAL:
            resolved = Path(os.path.expanduser(dir_template)) / "fw-review"
            if resolved not in processed:
                targets.append(resolved)
                processed.add(resolved)

    # Project-level targets — from tool definitions and cross-tool standard dirs
    if project_root is not None and scope in ("project", "all"):
        for tool in TOOLS.values():
            for dir_template in tool.skill_dirs_project:
                resolved = Path(dir_template.replace("{project}", str(project_root))) / "fw-review"
                if resolved not in processed:
                    targets.append(resolved)
                    processed.add(resolved)
        for dir_template in CROSS_TOOL_SKILL_DIRS_PROJECT:
            resolved = Path(dir_template.replace("{project}", str(project_root))) / "fw-review"
            if resolved not in processed:
                targets.append(resolved)
                processed.add(resolved)

    installed = False
    for target in targets:
        if target.exists():
            # Check if source is newer — reinstall when skill was updated
            try:
                src_mtime = (skill_dir / "SKILL.md").stat().st_mtime
                dst_mtime = (target / "SKILL.md").stat().st_mtime
                if src_mtime <= dst_mtime:
                    if not dry_run:
                        print(f"  [skip] Skill already up-to-date: {target}")
                    continue
            except OSError:
                pass
            if not dry_run:
                print(f"  [update] Updating skill: {target}")
        if dry_run:
            print(f"  [dry-run] Would install skill to {target}")
            installed = True
            continue

        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_dir / "SKILL.md", target / "SKILL.md")
        print(f"  [ok] Skill installed: {target / 'SKILL.md'}")
        installed = True

    return installed


def _detect_project_ai_tools(project_root: Path) -> list[str]:  # noqa: ARG001 — kept for API compat
    """Return AI tool IDs detected as installed on the system.

    Uses ``AiTool.is_detected()`` — checks for CLI binaries in PATH
    and global config directories (``~/.claude``, ``~/.codex``, etc.).
    """
    from .config.tools import TOOLS

    return [tid for tid, t in TOOLS.items() if t.is_detected()]


def _inject_agent_toml_section(path: Path, content: str, marker: str) -> None:
    """Inject a ``# marker ... # /marker`` block into a Codex TOML agent file.

    Codex agent files use TOML format with an ``[instructions]`` key that
    holds freeform text.  The fw-context block is inserted into the
    ``[instructions]`` section using ``# fw-context`` comment markers.
    When no ``[instructions]`` section exists, one is created at the end
    of the file.
    """
    import re

    start_comment = f"# {marker}"
    end_comment = f"# /{marker}"

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # If markers already exist, replace between them
    if start_comment in existing and end_comment in existing:
        before = existing[: existing.index(start_comment)]
        after = existing[existing.index(end_comment) + len(end_comment):]
        updated = before.rstrip("\n") + "\n" + start_comment + "\n" + content + "\n" + end_comment + "\n" + after.lstrip("\n")
    else:
        # Find [instructions] section and insert the block there
        instructions_match = re.search(r"^\[instructions\]\s*$", existing, re.MULTILINE)
        if instructions_match:
            # Find end of instructions section (next TOML section or EOF)
            section_end = len(existing)
            next_section = re.search(r"^\[", existing[instructions_match.end():], re.MULTILINE)
            if next_section:
                section_end = instructions_match.end() + next_section.start()
            before = existing[:section_end]
            after = existing[section_end:]
            updated = before.rstrip("\n") + "\n" + start_comment + "\n" + content + "\n" + end_comment + "\n" + after
        else:
            # No [instructions] section — create one at end of file
            updated = existing.rstrip("\n") + "\n\n[instructions]\n" + start_comment + "\n" + content + "\n" + end_comment + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")


def _convert_agent_md_to_toml(md_content: str) -> str:
    """Convert agent ``.md`` template to Codex ``.toml`` format.

    YAML frontmatter → TOML comments, ``<!-- fw-context -->`` markers
    → ``# fw-context``, body wrapped in ``[instructions]`` section.
    """
    import re

    parts = md_content.split("---", 2)
    if len(parts) >= 3:
        frontmatter_text = parts[1]
        body = parts[2]
    else:
        frontmatter_text = ""
        body = md_content

    lines: list[str] = []
    for line in frontmatter_text.strip().splitlines():
        m = re.match(r"^(name|description):\s*(.*)", line)
        if m:
            key = m.group(1)
            value = m.group(2).strip()
            lines.append(f"# {key}: {value}")
    if lines:
        lines.append("")

    lines.append("[instructions]")
    # Convert HTML comment markers to TOML hash-comment markers
    body = body.replace("<!-- fw-context -->", "# fw-context")
    body = body.replace("<!-- /fw-context -->", "# /fw-context")
    lines.append(body.strip())
    return "\n".join(lines) + "\n"


def _install_agents(dry_run: bool = False, project_root: Path | None = None, scope: str = "project") -> bool:
    """Inject fw-context CRITICAL block into ALL agent files across ALL AI tools.

    Scans agent directories for every registered tool (and cross-tool
    ``.agents/`` standard directories), injecting ``AGENT_CRITICAL_BLOCK``
    into each existing agent file.  Also installs template agents from
    ``data/agents/`` into compatible directories where the template does
    not yet exist.

    Agent directories and file patterns are driven by the ``TOOLS``
    registry — no tool paths are hardcoded here.
    """
    import re

    from . import __file__ as _pkg_init
    from .config.tools import (
        AGENT_CRITICAL_BLOCK,
        CROSS_TOOL_AGENT_DIRS_GLOBAL,
        CROSS_TOOL_AGENT_DIRS_PROJECT,
        TOOLS,
    )

    pkg_dir = Path(_pkg_init).parent
    agents_src = pkg_dir / "data" / "agents"

    # Collect templates (may be empty if data/agents/ doesn't exist)
    templates = sorted(agents_src.glob("*.md")) if agents_src.is_dir() else []

    # Build target tuples: (directory, tool_id, file_patterns, strip_name)
    targets: list[tuple[Path, str, list[str], bool]] = []
    processed_dirs: set[Path] = set()

    if scope in ("global", "all"):
        for tool_id, tool in TOOLS.items():
            for dir_template in tool.agent_dirs_global:
                resolved = Path(os.path.expanduser(dir_template))
                if resolved in processed_dirs:
                    continue
                if resolved.exists() or scope == "all":
                    targets.append((resolved, tool_id, tool.agent_file_patterns, tool.agent_strip_name))
                    processed_dirs.add(resolved)
        # Cross-tool standard directories
        for dir_template in CROSS_TOOL_AGENT_DIRS_GLOBAL:
            resolved = Path(os.path.expanduser(dir_template))
            if resolved not in processed_dirs:
                targets.append((resolved, "_cross", ["*.md"], True))
                processed_dirs.add(resolved)

    if project_root is not None and scope in ("project", "all"):
        for tool_id, tool in TOOLS.items():
            for dir_template in tool.agent_dirs_project:
                resolved = Path(dir_template.replace("{project}", str(project_root)))
                if resolved in processed_dirs:
                    continue
                if resolved.exists() or scope == "all":
                    targets.append((resolved, tool_id, tool.agent_file_patterns, tool.agent_strip_name))
                    processed_dirs.add(resolved)
        # Cross-tool standard directories
        for dir_template in CROSS_TOOL_AGENT_DIRS_PROJECT:
            resolved = Path(dir_template.replace("{project}", str(project_root)))
            if resolved not in processed_dirs:
                targets.append((resolved, "_cross", ["*.md"], True))
                processed_dirs.add(resolved)

    installed = False
    for agents_dir, _tool_id, patterns, strip_name in targets:
        # ── Step 1: Inject CRITICAL block into ALL existing agent files ──
        existing_files: list[Path] = []
        for pattern in patterns:
            existing_files.extend(sorted(agents_dir.glob(pattern)))

        for agent_path in existing_files:
            if dry_run:
                print(f"  [dry-run] {agent_path}: would UPDATE fw-context section")
                installed = True
                continue

            if agent_path.suffix == ".toml":
                _inject_agent_toml_section(agent_path, AGENT_CRITICAL_BLOCK, "fw-context")
            else:
                _inject_agent_section(agent_path, AGENT_CRITICAL_BLOCK, "fw-context")
            print(f"  [ok] {agent_path}: updated fw-context section")
            installed = True

        # ── Step 2: Install template agents for compatible directories ──
        if not templates:
            continue
        if not agents_dir.exists() and scope != "all":
            continue

        for template_path in templates:
            # Install .md templates into dirs with compatible patterns
            template_suffix = template_path.suffix  # ".md"
            is_compatible = any(
                template_suffix == p.lstrip("*") for p in patterns
            )
            if not is_compatible:
                # Convert .md → .toml when target uses TOML patterns (Codex)
                has_toml = any(".toml" in p for p in patterns)
                if has_toml:
                    target_name = template_path.stem + ".toml"
                    target_path = agents_dir / target_name
                    template_text = _convert_agent_md_to_toml(template_path.read_text(encoding="utf-8"))
                    if strip_name:
                        template_text = re.sub(r"^# name:.*\n", "", template_text, flags=re.MULTILINE)
                    if dry_run:
                        if not target_path.exists():
                            print(f"  [dry-run] {target_path}: would CREATE (TOML)")
                            installed = True
                        continue
                    agents_dir.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(template_text, encoding="utf-8")
                    print(f"  [ok] {target_path}: created (TOML)")
                    installed = True
                # Rename .md → .mdc when target uses Cursor patterns (same content)
                elif any(".mdc" in p for p in patterns):
                    target_name = template_path.stem + ".mdc"
                    target_path = agents_dir / target_name
                    template_text = template_path.read_text(encoding="utf-8")
                    if strip_name:
                        template_text = re.sub(r"^name:.*\n", "", template_text, flags=re.MULTILINE)
                    if dry_run:
                        if not target_path.exists():
                            print(f"  [dry-run] {target_path}: would CREATE")
                            installed = True
                        continue
                    agents_dir.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(template_text, encoding="utf-8")
                    print(f"  [ok] {target_path}: created")
                    installed = True
                continue

            target_path = agents_dir / template_path.name

            if dry_run:
                if not target_path.exists():
                    print(f"  [dry-run] {target_path}: would CREATE with fw-context section")
                    installed = True
                continue

            if not target_path.exists():
                agents_dir.mkdir(parents=True, exist_ok=True)
                template_text = template_path.read_text(encoding="utf-8")
                if strip_name:
                    template_text = re.sub(r"^name:.*\n", "", template_text, flags=re.MULTILINE)
                target_path.write_text(template_text, encoding="utf-8")
                print(f"  [ok] {target_path}: created with fw-context section")
                installed = True

    return installed


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

    # Resolve fw-context-mcp binary — prefer canonical install over dev venv
    mcp_bin = None
    for candidate in [
        Path.home() / ".local" / "bin" / "fw-context-mcp",
        Path.home() / ".fw-context" / ".venv" / "bin" / "fw-context-mcp",
    ]:
        if candidate.exists():
            mcp_bin = str(candidate)
            break
    if not mcp_bin:
        mcp_bin = shutil.which("fw-context-mcp")
    if not mcp_bin:
        dev_candidate = Path(sys.executable).parent / "fw-context-mcp"
        if dev_candidate.exists():
            mcp_bin = str(dev_candidate)

    project_root = Path.cwd() if not args.project else Path(args.project).resolve()

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
        # Detect tools based on scope.
        # For project scope: system-wide tools are always included — if a
        # tool is installed but has no project files yet, we create them.
        project_tools = _detect_project_ai_tools(project_root)
        system_wide = [tid for tid, t in TOOLS.items() if t.is_detected()]
        if args.scope == "project":
            seen: set[str] = set(project_tools)
            selected = list(project_tools)
            for tid in system_wide:
                if tid not in seen:
                    seen.add(tid)
                    selected.append(tid)
        elif args.scope == "global":
            selected = system_wide
        else:  # all
            seen = set()
            selected = []
            for tid in system_wide + project_tools:
                if tid not in seen:
                    seen.add(tid)
                    selected.append(tid)

    if not selected:
        if args.scope == "project":
            print("No AI assistant detected in this project or system-wide.")
            print()
            print("Run an AI assistant (Claude Code, OpenCode, etc.) in this project")
            print("directory first — it will create its config directory. Then re-run")
            print("'fw-context init'.")
            print()
            print("Alternatively, use --scope global to install fw-context for all")
            print("projects, or --tool to target a specific assistant.")
        else:
            print("No AI assistants detected. Use --list-tools to see supported tools.")
        return 1

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
                ok = True  # Inheritance is a valid configuration
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
        if not args.instructions_only and mcp_bin and (tool.mcp_registration or tool.mcp_config_file):
            if args.dry_run:
                _register_mcp(tool, mcp_bin, dry_run=True)
            else:
                _register_mcp(tool, mcp_bin)

        # Instruction injection
        if not tool.targets:
            if not tool.mcp_registration and not tool.mcp_config_file:
                print("  [skip] No instruction targets defined")
            continue

        for target in tool.targets:
            if args.scope != "all" and target.scope != args.scope:
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

    # Project-level config and assets (skills, agents)
    if ok and not args.dry_run:
        if not args.instructions_only:
            proj_config = _ensure_project_config(project_root)
            local_config = _ensure_project_local_config(project_root)
            print(f"\n[ok] {proj_config}: shared project config ready — edit source_roots, excludes, etc. (commit to git)")
            print(f"[ok] {local_config}: local developer config ready — edit ollama_url, model, etc. (gitignore)")
            print("  Run 'fw-context project-init' to set up .gitignore and verify project configuration.")
            print()
        _install_skills(dry_run=False, project_root=project_root, scope=args.scope)
        _install_agents(dry_run=False, project_root=project_root, scope=args.scope)

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  ⚠ {w}")
    if ok:
        if args.dry_run:
            _install_skills(dry_run=True, project_root=project_root, scope=args.scope)
            _install_agents(dry_run=True, project_root=project_root, scope=args.scope)
            print("\nDry-run complete. Run without --dry-run to apply changes.")
        else:
            print("\nSetup complete. Restart your AI assistant to pick up changes.")
    else:
        print("\nNo changes made.", file=sys.stderr)
    return 0 if ok else 1


def cmd_project_init(args: argparse.Namespace) -> int:
    """Verify or fix project-level fw-context configuration.

    Without ``--fix``: reports the current state of config files,
    ``.gitignore`` entries, build system detection, and index freshness.

    With ``--fix``: creates missing config files, adds missing
    ``.gitignore`` entries, and appends missing config options to
    existing files.
    """
    from .config import load as load_config
    from .config.settings import (
        _PROJECT_DEFAULTS_TEMPLATE,
        _PROJECT_LOCAL_DEFAULTS_TEMPLATE,
    )
    from .utils import resolve_project_root

    project_root = resolve_project_root(args.project)
    cfg = load_config(project_root=project_root)
    fix = args.fix

    label = "[fix]" if fix else "[info]"
    print(f"{label} Project: {project_root}")
    if fix:
        print("Mode: fix — applying corrections\n")
    else:
        print("Mode: verify — use --fix to apply corrections\n")

    # ── 1. Config files ──
    _check_config_file(
        project_root,
        ".fw-context/config.toml",
        _PROJECT_DEFAULTS_TEMPLATE,
        fix,
    )
    _check_config_file(
        project_root,
        ".fw-context/local.toml",
        _PROJECT_LOCAL_DEFAULTS_TEMPLATE,
        fix,
    )

    # ── 2. .gitignore entries ──
    from .indexer.build import detect_build_system

    build_system = detect_build_system(project_root)
    _ensure_gitignore(project_root, fix=fix, build_system=build_system)

    # ── 3. Build system ──
    if build_system:
        print(f"  [ok] build system: {build_system}")
    elif fix:
        print("  [warn] no build system detected — set [build] system in config.toml")
    else:
        print("  [warn] no build system detected — set [build] system in config.toml")

    # ── 4. compile_commands.json ──
    cc = cfg.index.compile_commands
    if not cc.is_absolute():
        cc = (project_root / cc).resolve()
    if cc.exists():
        from .indexer.build import check_completeness

        issues = list(check_completeness(cc, project_root))
        if issues:
            print(f"  [warn] {cc}:")
            for w in issues:
                print(f"         {w}")
        else:
            print(f"  [ok] {cc}")
    else:
        print(f"  [info] {cc} not found — run 'fw-context index --build' to build and index")

    # ── 5. Index health ──
    from .config import derive_project_id
    from .indexer.db import get_active_config, open_db as db_open

    project_id = derive_project_id(project_root)
    db_path = cfg.index.db_dir / project_id / "index.db"
    if db_path.exists():
        try:
            conn = db_open(db_path)
            active = get_active_config(conn, project_id)
            if active:
                sym_count = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE config_hash=?",
                    (active["config_hash"],),
                ).fetchone()[0]
                print(f"  [ok] index: {sym_count} symbols")
                # Check embeddings
                emb_count = conn.execute(
                    "SELECT COUNT(*) FROM embeddings e JOIN symbols s ON s.id = e.symbol_id WHERE s.config_hash = ?",
                    (active["config_hash"],),
                ).fetchone()[0]
                if emb_count == 0:
                    print("  [info] no embeddings yet — run 'fw-context index' to generate")
                # Check LLM analysis
                ana_count = conn.execute(
                    "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash = ?",
                    (active["config_hash"],),
                ).fetchone()[0]
                if ana_count == 0:
                    print("  [info] no LLM symbol analysis yet — run 'fw-context index' to generate")
            conn.close()
        except Exception as e:
            print(f"  [warn] cannot read index: {e}")
    else:
        print("  [info] no index yet — run 'fw-context index' to build")

    if fix:
        print("\nProject fixed. Run 'fw-context index' to (re)build the index.")
    else:
        print("\nProject verified. Run 'fw-context project-init --fix' to apply corrections.")
    return 0


def _check_config_file(project_root: Path, rel_path: str, template: str, fix: bool) -> None:
    """Check a single config file — report missing keys, optionally fix."""
    path = project_root / rel_path
    if not path.exists():
        if fix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template)
            print(f"  [fix] created {path}")
        else:
            print(f"  [warn] {path} missing — run with --fix to create")
        return

    # Parse template for option keys (lines like "# key = value" or "key = value")
    import re

    template_keys: set[str] = set()
    for line in template.splitlines():
        # Match both "# key = ..." and "key = ..." lines
        m = re.match(r"^#?\s*(\w+)\s*=", line)
        if m:
            template_keys.add(m.group(1))

    existing_text = path.read_text(encoding="utf-8")
    missing: list[str] = []
    for key in sorted(template_keys):
        if not re.search(rf"^\s*#?\s*{key}\s*=", existing_text, re.MULTILINE):
            # Also check if key appears under a [section] (TOML scoped)
            if not re.search(rf"^\s*{key}\s*=", existing_text, re.MULTILINE):
                missing.append(key)

    if missing:
        if fix:
            with path.open("a", encoding="utf-8") as f:
                f.write("\n")
                for line in template.splitlines():
                    m = re.match(r"^#?\s*(\w+)\s*=", line)
                    if m and m.group(1) in missing:
                        f.write(line + "\n")
            print(f"  [fix] {path}: added {', '.join(missing)}")
        else:
            print(f"  [info] {path}: missing options: {', '.join(missing)}")
    else:
        print(f"  [ok] {path}")


def _ensure_platformio_dep_tracking(project_root: Path, *, fix: bool = False) -> None:
    """Deprecated thin wrapper — delegates to ``PlatformIOBuildSystem.ensure_dep_tracking()``."""
    from .indexer.builders.platformio import PlatformIOBuildSystem
    builder = PlatformIOBuildSystem()
    for msg in builder.ensure_dep_tracking(project_root, fix=fix):
        if msg:
            print(msg)


def _ensure_gitignore(project_root: Path, *, fix: bool = False, build_system: str | None = None) -> None:
    """Add ``compile_commands.json`` and ``.fw-context/local.toml`` to the
    project's ``.gitignore`` if they aren't already listed.

    For Mbed OS projects, also adds ``mbed_config.h`` — the build-generated
    config header that ends up in the project root.

    Reads the existing file (when present), checks each entry as a literal
    line, and appends missing entries.  Idempotent — running multiple times
    adds no duplicates.

    In fix mode (*fix=True*), actually writes the file.  Otherwise only
    reports what would be added.
    """
    entries = [
        "compile_commands.json",
        ".fw-context/local.toml",
    ]
    if build_system == "mbed-os":
        entries.insert(1, "mbed_config.h")

    gitignore = project_root / ".gitignore"
    try:
        existing_lines: set[str] = set()
        if gitignore.exists():
            existing_lines = {
                line.strip()
                for line in gitignore.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }
    except (OSError, PermissionError):
        return

    missing = [e for e in entries if e not in existing_lines]
    if not missing:
        print(f"  [ok] {gitignore}")
        return

    if fix:
        try:
            with gitignore.open("a", encoding="utf-8") as f:
                if gitignore.stat().st_size > 0:
                    f.seek(0, 2)
                f.write("\n# fw-context\n")
                for e in missing:
                    f.write(f"{e}\n")
            print(f"  [fix] {gitignore}: added {', '.join(missing)}")
        except (OSError, PermissionError) as e:
            import logging

            logging.getLogger(__name__).warning("Could not update %s: %s", gitignore, e)
    else:
        print(f"  [info] {gitignore}: missing entries: {', '.join(missing)}")


def _register_mcp(tool, mcp_bin: str, dry_run: bool = False) -> None:
    """Register fw-context as an MCP server with *tool*'s configuration.

    *tool* is an ``AiTool`` instance; *mcp_bin* is the path or name of the
    ``fw-context-mcp`` executable. Dispatches to CLI-based or file-based
    registration depending on which fields are set on *tool*.
    """
    if tool.mcp_config_file:
        _register_mcp_file(tool, mcp_bin, dry_run=dry_run)
    elif tool.mcp_registration:
        if dry_run:
            print(f"  [dry-run] {tool.name}: would register {mcp_bin}")
        else:
            _register_mcp_cli(tool, mcp_bin)


def _register_mcp_cli(tool, mcp_bin: str) -> None:
    """Register fw-context as an MCP server via a CLI command."""
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


def _ensure_subagent_mcp_permission(data: dict, tool_id: str) -> bool:
    """Ensure general subagent has fw-context MCP tool permissions (OpenCode)."""
    if "agent" not in data:
        data["agent"] = {}
    if "general" not in data["agent"]:
        data["agent"]["general"] = {}
    if "permission" not in data["agent"]["general"]:
        data["agent"]["general"]["permission"] = {}
    general_perm = data["agent"]["general"]["permission"]
    if "mcp__fw-context__*" not in general_perm:
        general_perm["mcp__fw-context__*"] = "allow"
        return True
    return False


def _register_mcp_file(tool, mcp_bin: str, dry_run: bool = False) -> None:
    """Register fw-context as an MCP server by editing a JSON config file.

    Used for tools that store MCP server configuration in a JSON file
    rather than exposing a CLI command (e.g. OpenCode's ``opencode.json``).
    Preserves existing file structure (schema, other MCP servers, etc.)
    and marks fw-context as ``enabled: true`` with ``type: local``.
    Also ensures the general subagent has fw-context MCP tool permissions.
    """
    import json
    import os

    if not tool.mcp_config_file:
        return

    config_path = Path(os.path.expanduser(tool.mcp_config_file))

    try:
        if config_path.exists():
            raw = config_path.read_text(encoding="utf-8")
            # Strip JSONC comments (// and /* */) before parsing
            raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
            raw = re.sub(r"//.*$", "", raw, flags=re.MULTILINE)
            data = json.loads(raw)
        else:
            data = {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] {tool.name}: could not read {config_path}: {e}", file=sys.stderr)
        return

    mcp_servers = data.setdefault("mcp", {})
    key = tool.mcp_config_key or "fw-context"

    existing = mcp_servers.get(key)
    mcp_already_registered = isinstance(existing, dict) and existing.get("command") == [mcp_bin]

    # Always ensure subagent permissions, even if MCP is already registered
    perm_added = _ensure_subagent_mcp_permission(data, tool.id)

    if mcp_already_registered:
        if perm_added:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"  [ok] {tool.name}: fw-context already registered, added general subagent permission")
        else:
            print(f"  [ok] {tool.name}: fw-context already registered")
        return

    if dry_run:
        if isinstance(existing, dict):
            print(f"  [dry-run] {tool.name}: {config_path}: would UPDATE fw-context → {mcp_bin}")
        else:
            print(f"  [dry-run] {tool.name}: {config_path}: would ADD fw-context → {mcp_bin}")
        if not mcp_already_registered or perm_added:
            print(f"  [dry-run] {tool.name}: would ensure general subagent fw-context permission")
        return

    mcp_servers[key] = {
        "command": [mcp_bin],
        "enabled": True,
        "type": "local",
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  [ok] {tool.name}: fw-context registered ({mcp_bin})")


def _update_marked_section(path: Path, content: str, marker: str) -> None:
    """Insert or replace a <!-- marker --> ... <!-- /marker --> block in a markdown file."""
    start_tag = f"<!-- {marker} -->"
    end_tag = f"<!-- /{marker} -->"

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if start_tag in existing and end_tag in existing:
        # Replace the existing marked block (keep markers for idempotency)
        before = existing[: existing.index(start_tag)]
        after = existing[existing.index(end_tag) + len(end_tag) :]
        updated = before.rstrip("\n") + "\n\n" + start_tag + "\n" + content + "\n" + end_tag + "\n" + after.lstrip("\n")
    else:
        # Remove any unmarked section with the same heading (idempotency for manual installs)
        heading_match = re.search(r"^## .+", content, re.MULTILINE)
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
            existing.rstrip("\n")
            + ("\n\n" if existing.strip() else "")
            + start_tag
            + "\n"
            + content
            + "\n"
            + end_tag
            + "\n"
        )

    path.write_text(updated, encoding="utf-8")


def _inject_agent_section(path: Path, content: str, marker: str) -> None:
    """Insert or replace a ``<!-- marker --> ... <!-- /marker -->`` block in an agent markdown file.

    Unlike ``_update_marked_section``, when no existing marker is found this
    inserts the block right after the YAML frontmatter (``---`` delimiters)
    so the CRITICAL instructions are the first thing the agent sees.
    """
    start_tag = f"<!-- {marker} -->"
    end_tag = f"<!-- /{marker} -->"

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if start_tag in existing and end_tag in existing:
        # Replace the existing marked block (keep markers for idempotency)
        before = existing[: existing.index(start_tag)]
        after = existing[existing.index(end_tag) + len(end_tag):]
        updated = before.rstrip("\n") + "\n\n" + start_tag + "\n" + content + "\n" + end_tag + "\n" + after.lstrip("\n")
    else:
        # Insert right after YAML frontmatter (after second "---").
        # Only count --- delimiters that appear in the document preamble —
        # once we pass the closing --- (or see non-YAML content before any
        # opening ---), we stop looking.  This avoids false matches on ---
        # inside fenced code blocks later in the file.
        lines = existing.splitlines()
        result_lines: list[str] = []
        frontmatter_dashes = 0
        in_preamble = True
        inserted = False

        for line in lines:
            result_lines.append(line)
            stripped = line.strip()
            if not in_preamble:
                continue
            if stripped == "---":
                frontmatter_dashes += 1
                if frontmatter_dashes == 2 and not inserted:
                    result_lines.append("")
                    result_lines.append(start_tag)
                    result_lines.append(content)
                    result_lines.append(end_tag)
                    inserted = True
                    in_preamble = False
            elif frontmatter_dashes == 0 and stripped:
                # Non-blank, non---- line before opening --- → no frontmatter
                in_preamble = False
            # If frontmatter_dashes == 1 and stripped is non-blank, it's
            # YAML keys inside the frontmatter — stay in_preamble.

        if inserted:
            updated = "\n".join(result_lines) + "\n"
        else:
            # No frontmatter found — prepend to file
            updated = start_tag + "\n" + content + "\n" + end_tag + "\n\n" + existing

    path.parent.mkdir(parents=True, exist_ok=True)
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
    from .utils import resolve_project_root

    project_root = resolve_project_root(args.project)
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
        print(
            f"Exported {output['symbol_count']} symbols"
            f"{' + ' + str(output.get('reference_count', 0)) + ' references' if 'reference_count' in output else ''}"
            f" → {args.output}"
        )
    else:
        print(json_text)

    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Re-run LLM symbol analysis on an existing index (idempotent).

    Requires Ollama (or LM Studio) running and ``[llm] enabled = true``
    in config. Re-generates per-symbol summaries, inputs/outputs analysis
    and method override relationships. Existing analysis rows are skipped
    (idempotent) — only unanalyzed symbols are processed.
    """
    from .config import derive_project_id
    from .config import load as load_config
    from .indexer.db import get_active_config, open_db
    from .indexer.runner import _build_llm_analysis, _build_overrides
    from .utils import resolve_project_root

    if args.verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(VerboseFormatter())
        logging.basicConfig(level=logging.DEBUG, handlers=[handler])
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )

    project_root = resolve_project_root(args.project)
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

    # Compute SDK exclude patterns for this project.
    # When analyze_vendor is True, analyze everything.
    from .indexer.runner import _detect_sdk_exclude_like

    if cfg.llm.analyze_vendor:
        exclude_like: list[str] = []
    else:
        exclude_paths = cfg.exclude_root_paths(project_root)
        config_exclude_strs = [
            str(p.relative_to(project_root)) for p in exclude_paths if p.is_relative_to(project_root)
        ]
        exclude_like = _detect_sdk_exclude_like(project_root, config_exclude_strs)

    # Re-open connection for the analysis (uses its own transactions)
    conn = open_db(db_path)
    try:
        # Create CacheClient from config if available
        cc = None
        if cfg.cache_server and cfg.cache_server.url:
            try:
                from fw_context_mcp.cache_client import CacheClient

                cc = CacheClient(
                    url=cfg.cache_server.url,
                    token=cfg.cache_server.token,
                    force=cfg.cache_server.force,
                    batch_size=cfg.cache_server.batch_size,
                )
            except Exception as e:
                logging.getLogger(__name__).warning("Failed to create CacheClient: %s", e)

        _build_llm_analysis(
            conn,
            config_hash,
            cfg.llm,
            db_path.parent,
            exclude_like=exclude_like,
            cache_client=cc,
            retry_unparseable=True,
        )
        if cc:
            cc.close()
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


def cmd_cache_stats(args: argparse.Namespace) -> int:
    """Show cache statistics for one or both tiers."""
    from .config import derive_project_id
    from .config import load as load_config
    from .utils import resolve_project_root
    from fw_context_mcp.cache_client import local_cache_stats, CacheClient

    show_local = not args.remote
    project_root = resolve_project_root(args.project) if hasattr(args, "project") and args.project else Path.cwd()

    # Tier 1: local global cache
    if show_local:
        local_stats = local_cache_stats()
        print(f"Local cache (Tier 1): {local_stats['total_entries']} entries  ({local_stats['path']})")

    # Tier 2: remote cache
    if args.remote:
        cfg = load_config(project_root=project_root)
        cs = cfg.cache_server
        if not cs or not cs.url:
            print("Remote cache (Tier 2): not configured (set [cache_server] in config)")
            return 0

        cc = CacheClient(url=cs.url, token=cs.token)
        try:
            remote_stats = cc.stats()
            if not remote_stats:
                print(f"Remote cache (Tier 2): {cs.url}")
                print("  Server unreachable — check connectivity")
                return 0

            print(f"Remote cache (Tier 2): {cs.url}")
            total = remote_stats.get("total_entries", 0)
            newest = remote_stats.get("newest_entry", "")
            models = remote_stats.get("models", {})

            print(f"  Total entries: {total}")
            if newest:
                print(f"  Newest entry:  {newest}")
            if models:
                print("  Models:")
                for model, cnt in sorted(models.items()):
                    pct = f" ({cnt / total * 100:.0f}%)" if total else ""
                    print(f"    {model}: {cnt}{pct}")

            # Per-project breakdown: batch-lookup all sym's hash in this project
            project_id = derive_project_id(project_root)
            db_path = cfg.index.db_dir / project_id / "index.db"
            if db_path.exists():
                from .indexer.db import open_db

                conn = open_db(db_path)
                hashes: list[str] = []
                try:
                    hashes = [
                        r[0]
                        for r in conn.execute(
                            "SELECT DISTINCT content_hash FROM llm_analysis WHERE content_hash != ''"
                        ).fetchall()
                    ]
                finally:
                    conn.close()
                if hashes:
                    hits = cc.batch_get(hashes)
                    cached_count = sum(1 for v in hits.values() if v is not None)
                    print(f"  Project cache: {cached_count}/{len(hashes)} cached ({project_id})")
        finally:
            cc.close()

    return 0


def cmd_cache_push(args: argparse.Namespace) -> int:
    """Push all local cache entries to the remote cache server.

    Uses ``--force`` by default (X-Cache-Overwrite) so newer local entries
    replace older remote ones.  Progress is reported in batches.
    """
    from .config import load as load_config
    from .utils import resolve_project_root
    from fw_context_mcp.cache_client import get_local_cache_db, CacheClient

    project_root = resolve_project_root(args.project) if hasattr(args, "project") else None
    if not project_root:
        print("error: --project required for remote cache push", file=sys.stderr)
        return 1

    cfg = load_config(project_root=project_root)
    cs = cfg.cache_server
    if not cs or not cs.url:
        print("error: [cache_server] not configured", file=sys.stderr)
        return 1

    local_db = get_local_cache_db(readonly=True)
    try:
        rows = local_db.execute(
            "SELECT content_hash, summary, inputs, outputs, model FROM llm_analysis_cache"
        ).fetchall()
        total = len(rows)
        if total == 0:
            print("Local cache is empty — nothing to push.")
            return 0

        batch_size = args.batch or cs.batch_size
        cc = CacheClient(url=cs.url, token=cs.token, force=True, batch_size=batch_size)
        try:
            pushed = 0
            for i in range(0, total, batch_size):
                chunk = rows[i : i + batch_size]
                entries = [
                    {"hash": r[0], "summary": r[1], "inputs": r[2], "outputs": r[3], "model": r[4]} for r in chunk
                ]
                n = cc.batch_put(entries)
                pushed += n
                print(f"  [{i + len(chunk)}/{total}] pushed {n} entries")
            print(f"Done: {pushed}/{total} entries pushed to {cs.url}")
        finally:
            cc.close()
    finally:
        local_db.close()

    return 0


def cmd_cache_remote_init(args: argparse.Namespace) -> int:
    """Interactive wizard: configure remote cache server connection.

    Prompts for URL and token, verifies the connection, and writes
    [cache_server] to the global config (~/.fw-context/config.toml).
    """
    import re
    import httpx

    from .config.settings import _ensure_global_config

    # Resolve global config
    config_path = _ensure_global_config()

    # Read existing config
    existing = config_path.read_text(encoding="utf-8")

    # Show current config if any
    current_url = ""
    url_match = re.search(r'\[cache_server\].*?\nurl\s*=\s*"([^"]*)"', existing, re.DOTALL)
    if url_match:
        current_url = url_match.group(1)

    if current_url:
        print(f"Current remote cache: {current_url}")
    else:
        print("No remote cache configured.")

    # --- Step 1: URL ---
    print()
    url_default = current_url or "https://fw-cache.example.com"
    url_input = input(f"Cache server URL [{url_default}]: ").strip()
    url = url_input if url_input else url_default

    # --- Step 2: Token ---
    print()
    token_input = input("Token (paste your read or read+write token): ").strip()
    if not token_input:
        print("error: token is required", file=sys.stderr)
        return 1
    token = token_input

    # --- Step 3: Verify connection ---
    print(f"\nVerifying connection to {url} ...")
    try:
        with httpx.Client(base_url=url, timeout=10.0) as client:
            # Check health first
            health_resp = client.get("/health")
            if health_resp.status_code != 200:
                print(f"error: server returned {health_resp.status_code}", file=sys.stderr)
                return 1

            # Check auth
            auth_resp = client.get(
                "/cache/stats",
                headers={"Authorization": f"Bearer {token}"},
            )
            if auth_resp.status_code == 401:
                print("error: authentication failed (401) — check your token", file=sys.stderr)
                return 1
            if auth_resp.status_code == 403:
                print("error: access denied (403) — token may lack permissions", file=sys.stderr)
                return 1
            if auth_resp.status_code != 200:
                print(f"error: server returned {auth_resp.status_code}", file=sys.stderr)
                return 1

            stats = auth_resp.json()
            total = stats.get("total_entries", 0)
            can_read = stats.get("can_read", False)
            can_write = stats.get("can_write", False)
            can_overwrite = stats.get("can_overwrite", False)

            print(f"  Connected. Server has {total} cached entries.")
            perms = []
            if can_read:
                perms.append("read")
            if can_write:
                perms.append("write")
            if can_overwrite:
                perms.append("overwrite")
            perm_str = ", ".join(perms) if perms else "none"
            print(f"  Token permissions: {perm_str}")

            if not can_write:
                print()
                print("  NOTE: Your token is read-only. Remote cache push and clear")
                print("  will be skipped. To write, use a read+write token instead.")
    except httpx.ConnectError:
        print(f"error: cannot connect to {url} — check the URL and network", file=sys.stderr)
        return 1
    except httpx.TimeoutException:
        print(f"error: connection to {url} timed out", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # --- Step 4: Write config ---
    cache_section = f"""
[cache_server]
url = "{url}"
token = "{token}"
"""

    if "[cache_server]" in existing:
        # Replace existing section
        new_content = re.sub(
            r"\[cache_server\].*?(?=\[|$)",
            cache_section.strip(),
            existing,
            flags=re.DOTALL,
        )
    else:
        # Append
        new_content = existing.rstrip("\n") + "\n" + cache_section.strip() + "\n"

    config_path.write_text(new_content, encoding="utf-8")
    print(f"\nRemote cache configured: {url}")
    print(f"Config written to: {config_path}")
    print("Run 'fw-context cache stats --remote' to verify.")

    return 0


def cmd_cache_clear(args: argparse.Namespace) -> int:
    """Delete cache entries for one or both tiers."""
    from .config import derive_project_id
    from .config import load as load_config
    from .utils import resolve_project_root
    from fw_context_mcp.cache_client import local_cache_clear, CacheClient

    project_root = resolve_project_root(args.project) if hasattr(args, "project") else None

    # Determine which tiers to clear
    clear_local = args.all or not args.remote
    clear_remote = args.all or args.remote

    if not args.yes:
        tiers = []
        if clear_local:
            tiers.append("local (Tier 1)")
        if clear_remote:
            tiers.append("remote server (Tier 2)")
        answer = input(f"Delete cache for: {', '.join(tiers)}? This is safe — cache will be rebuilt. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Tier 1: local global cache (single DB shared by all projects)
    if clear_local:
        result = local_cache_clear()
        if result == 0:
            print("Local cache (Tier 1): deleted")
        else:
            print("Local cache (Tier 1): not found")

    # Tier 2: remote cache — clears only this project's entries
    if clear_remote and project_root:
        cfg = load_config(project_root=project_root)
        cs = cfg.cache_server
        if cs and cs.url:
            project_id = derive_project_id(project_root)
            db_path = cfg.index.db_dir / project_id / "index.db"
            hashes = []
            if db_path.exists():
                from .indexer.db import open_db

                conn = open_db(db_path)
                try:
                    hashes = [
                        r[0]
                        for r in conn.execute(
                            "SELECT DISTINCT content_hash FROM llm_analysis WHERE content_hash != ''"
                        ).fetchall()
                    ]
                finally:
                    conn.close()
            if hashes:
                cc = CacheClient(url=cs.url, token=cs.token)
                try:
                    n = cc.clear_remote(hashes)
                    print(f"Remote cache (Tier 2): cleared {n}/{len(hashes)} entries")
                finally:
                    cc.close()
            else:
                print("Remote cache (Tier 2): no project cache entries to clear")
        else:
            print("Remote cache (Tier 2): not configured (set [cache_server] in .fw-context/local.toml)")
    elif clear_remote:
        print("Remote cache (Tier 2): no project resolved")

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

    p_index = sub.add_parser(
        "index",
        help="Build the symbol index from compile_commands.json (reuses existing compile_commands.json, builds only if missing)",
    )
    p_index.add_argument("-v", "--verbose", action="store_true")
    p_index.add_argument(
        "compile_commands",
        nargs="?",
        default=None,
        metavar="compile_commands.json",
        help="Use an explicit compile_commands.json (skips build)",
    )
    p_index.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_index.add_argument(
        "--build", action="store_true", help="Force a clean build and regenerate compile_commands.json"
    )
    p_index.add_argument(
        "--no-clean",
        action="store_true",
        help="With --build: skip clean, do incremental build (may produce incomplete compile_commands.json)",
    )
    p_index.add_argument("--source-roots", nargs="+", metavar="DIR")
    p_index.add_argument("--name", metavar="NAME", help="Project name override")
    p_index.add_argument("--no-refs", action="store_true", help="Skip cross-reference indexing (on by default)")
    p_index.add_argument("--no-embeddings", action="store_true", dest="no_embeddings", help="Skip embedding generation")
    p_index.add_argument(
        "--embeddings",
        action="store_true",
        dest="embeddings",
        default=None,
        help="Generate symbol embeddings (default)",
    )
    p_index.add_argument(
        "--analyze",
        action="store_true",
        dest="analyze",
        default=False,
        help="Generate LLM-based symbol analysis (summary, inputs, outputs)",
    )
    p_index.add_argument("--no-analyze", action="store_true", dest="no_analyze", help="Skip LLM analysis generation")
    p_index.add_argument(
        "--force",
        action="store_true",
        help="Force re-index of all files, embeddings, LLM analysis, overrides, and caches (skip mtime/checksum checks)",
    )
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search indexed symbols")
    p_search.add_argument("-v", "--verbose", action="store_true")
    p_search.add_argument("query")
    p_search.add_argument("--project", metavar="DIR")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_init = sub.add_parser("init", help="Register fw-context with AI assistants and inject instructions")
    p_init.add_argument(
        "--tool", metavar="ID", help="Set up a specific tool (claude-code, opencode, kilocode, codex, cursor)"
    )
    p_init.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    p_init.add_argument("--force", action="store_true", help="Overwrite even when collisions are detected")
    p_init.add_argument(
        "--instructions-only", action="store_true", help="Only inject instructions, skip MCP registration"
    )
    p_init.add_argument(
        "--scope", choices=["all", "global", "project"], default="project",
        help="Which scope to inject (default: project — only the current project)",
    )
    p_init.add_argument("--project", metavar="DIR", help="Project root (for project-scoped targets)")
    p_init.add_argument(
        "--list-tools", action="store_true", help="List supported AI assistants and their detection status"
    )
    p_init.set_defaults(func=cmd_init, tool=None, dry_run=False, force=False, instructions_only=False, list_tools=False)

    p_list = sub.add_parser("list", help="List all indexed projects")
    p_list.add_argument("-v", "--verbose", action="store_true")
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

    p_project_init = sub.add_parser(
        "project-init", help="Verify or fix project setup (config, .gitignore, build system)"
    )
    p_project_init.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_project_init.add_argument("--fix", action="store_true", help="Apply fixes for detected issues")
    p_project_init.set_defaults(func=cmd_project_init)

    p_analyze = sub.add_parser("analyze", help="Re-run LLM symbol analysis on existing index (idempotent)")
    p_analyze.add_argument("-v", "--verbose", action="store_true")
    p_analyze.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_analyze.set_defaults(func=cmd_analyze)

    # Cache management subcommands
    p_cache = sub.add_parser("cache", help="Manage LLM analysis cache (local + remote)")
    p_cache_sub = p_cache.add_subparsers(dest="cache_command")

    p_cache_stats = p_cache_sub.add_parser("stats", help="Show cache statistics")
    p_cache_stats.add_argument("--project", metavar="DIR", help="Project root for remote config (default: cwd)")
    p_cache_stats.add_argument("--remote", action="store_true", help="Show only remote server cache (Tier 2)")
    p_cache_stats.set_defaults(func=cmd_cache_stats)

    p_cache_clear = p_cache_sub.add_parser("clear", help="Delete cache entries")
    p_cache_clear.add_argument("--project", metavar="DIR", help="Project root for remote config (default: cwd)")
    p_cache_clear.add_argument(
        "--remote", action="store_true", help="Clear remote server cache for this project (Tier 2)"
    )
    p_cache_clear.add_argument("--all", action="store_true", help="Clear both local and remote")
    p_cache_clear.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_cache_clear.set_defaults(func=cmd_cache_clear)

    p_cache_push = p_cache_sub.add_parser("push", help="Push all local cache entries to remote server")
    p_cache_push.add_argument("--project", metavar="DIR", help="Project root for remote config (default: cwd)")
    p_cache_push.add_argument("--batch", type=int, metavar="N", help="Batch size (default: from config, 100)")
    p_cache_push.set_defaults(func=cmd_cache_push)

    p_cache_remote = p_cache_sub.add_parser(
        "remote-init", help="Interactive setup: configure remote cache URL and token"
    )
    p_cache_remote.add_argument("--project", metavar="DIR", help="Project root (default: cwd)")
    p_cache_remote.set_defaults(func=cmd_cache_remote_init)

    p_version = sub.add_parser("version", help="Show version information")
    p_version.set_defaults(func=cmd_version)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
