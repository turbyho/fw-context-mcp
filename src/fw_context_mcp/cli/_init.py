"""``fw-context init`` — AI assistant setup and instruction injection."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path


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
    from . import __file__ as _pkg_init
    from ..config.tools import (
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
    from ..config.tools import TOOLS

    return [tid for tid, t in TOOLS.items() if t.is_detected()]


def _build_agent_targets(
    scope: str, project_root: Path | None
) -> list[tuple[Path, str, list[str], bool]]:
    """Build the list of (directory, tool_id, file_patterns, strip_name) tuples."""
    from ..config.tools import CROSS_TOOL_AGENT_DIRS_GLOBAL, CROSS_TOOL_AGENT_DIRS_PROJECT, TOOLS

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
        for dir_template in CROSS_TOOL_AGENT_DIRS_PROJECT:
            resolved = Path(dir_template.replace("{project}", str(project_root)))
            if resolved not in processed_dirs:
                targets.append((resolved, "_cross", ["*.md"], True))
                processed_dirs.add(resolved)

    return targets


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
    from . import __file__ as _pkg_init
    from ..config.tools import AGENT_CRITICAL_BLOCK
    from ._mcp import _convert_agent_md_to_toml, _inject_agent_section, _inject_agent_toml_section

    pkg_dir = Path(_pkg_init).parent
    agents_src = pkg_dir / "data" / "agents"
    templates = sorted(agents_src.glob("*.md")) if agents_src.is_dir() else []

    targets = _build_agent_targets(scope, project_root)

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
            is_compatible = any(template_suffix == p.lstrip("*") for p in patterns)
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


def _select_init_tools(args: argparse.Namespace, project_root: Path) -> list[str] | None:
    """Select which AI tools to act on.

    Returns a list of tool IDs, or None if a fatal error occurred (caller returns 1).
    """
    from ..config.tools import TOOLS

    selected: list[str] = []
    if args.tool:
        selected = [t.strip() for t in args.tool.split(",")]
        for tid in selected:
            if tid not in TOOLS:
                print(f"[error] Unknown tool: {tid}", file=sys.stderr)
                print(f"        Supported: {', '.join(TOOLS.keys())}", file=sys.stderr)
                return None
    else:
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
        else:
            seen = set()
            selected = []
            for tid in system_wide + project_tools:
                if tid not in seen:
                    seen.add(tid)
                    selected.append(tid)

    if not selected:
        print("No AI assistants detected — falling back to Claude Code configuration.")
        selected = ["claude-code"]

    return selected


def _init_one_tool(
    tool_id: str,
    args: argparse.Namespace,
    project_root: Path,
    mcp_bin: str | None,
    selected: list[str],
) -> tuple[bool, list[str]]:
    """Initialize a single AI tool: MCP registration + instruction injection.

    Returns (ok, warnings).  ok=True if at least one action succeeded;
    an inherited tool that requires no action is also considered ok.
    """
    from ..config.tools import TOOLS, check_target
    from ._mcp import _register_mcp, _update_marked_section

    tool = TOOLS[tool_id]
    print(f"\n── {tool.name} ({tool_id}) ──")
    warnings: list[str] = []
    ok = False

    if tool.inherits_from:
        parent = TOOLS.get(tool.inherits_from)
        parent_name = parent.name if parent else tool.inherits_from
        parent_ok = parent and parent.is_detected()
        if parent_ok and tool_id in selected and tool.inherits_from in selected:
            print(f"  [info] Inherits from {parent_name} — already handled above, skipping")
            return True, warnings
        elif parent_ok:
            print(f"  [info] Inherits from {parent_name} which has fw-context instructions")
            ok = True
            if not args.force:
                print("  [skip] Nothing to do. Use --force to inject anyway.")
                return ok, warnings
            print("  [force] Injecting despite inheritance...")
        else:
            print(f"  [warn] Inherits from {parent_name} but parent NOT DETECTED")
            print("  [info] Injecting instructions anyway...")

    if not args.instructions_only and mcp_bin and (tool.mcp_registration or tool.mcp_config_file):
        if args.dry_run:
            _register_mcp(tool, mcp_bin, dry_run=True)
        else:
            _register_mcp(tool, mcp_bin)

    if not tool.targets:
        if not tool.mcp_registration and not tool.mcp_config_file:
            print("  [skip] No instruction targets defined")
        return ok, warnings

    for target in tool.targets:
        if args.scope != "all" and target.scope != args.scope:
            continue

        collision = check_target(target, project_root if target.scope == "project" else None)
        resolved = collision.path
        instructions = target.render_instructions()

        if collision.is_skillshare_managed and not args.force:
            print(f"  [warn] {resolved}: directory managed by skillshare — skipping")
            warnings.append(f"{tool.name}: {resolved} is skillshare-managed, use --force to overwrite")
            continue

        if collision.has_unmarked_content and not args.force:
            print(f"  [warn] {resolved}: found unmarked fw-context content — skipping")
            print("         Use --force to overwrite, or remove the existing section manually")
            warnings.append(f"{tool.name}: {resolved} has unmarked fw-context content")
            continue

        if args.dry_run:
            if collision.has_marked_section:
                print(f"  [dry-run] {resolved}: would UPDATE marked section")
            else:
                print(f"  [dry-run] {resolved}: would CREATE ({target.method})")
            ok = True
            continue

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

    return ok, warnings


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
    from ..config import load as load_project_config
    from ..config.settings import _ensure_project_config, _ensure_project_local_config
    from ..config.settings import _write_project_id, generate_project_id as _gen_pid
    from ..config.tools import TOOLS
    from ..indexer.build import detect_build_system
    from ..utils import resolve_project_root
    from ._mcp import _resolve_mcp_bin

    if args.list_tools:
        print("Supported AI assistants:\n")
        for tool in TOOLS.values():
            print(f"  {tool.status()}")
        print("\nRun 'fw-context init --tool <id>' to set up a specific tool.")
        return 0

    mcp_bin = _resolve_mcp_bin()
    project_root = resolve_project_root(args.project)

    # ── Project ID + global registry ──
    _proj_cfg = load_project_config(project_root=project_root)
    if not _proj_cfg.project.id:
        new_id = _gen_pid()
        _write_project_id(project_root, new_id)
        print(f"Project ID: {new_id}")
        proj_id = new_id
    else:
        print(f"Project ID: {_proj_cfg.project.id} (existing)")
        proj_id = _proj_cfg.project.id

    from ..config.global_db import open_global_db, upsert_project_registry

    proj_name = getattr(args, "name", None) or _proj_cfg.project.name or project_root.name
    glob_conn = open_global_db()
    upsert_project_registry(glob_conn, proj_id, proj_name, "unknown", str(project_root))

    # ── Tool selection ──
    selected = _select_init_tools(args, project_root)
    if selected is None:
        return 1

    # ── Per-tool initialization ──
    ok = False
    all_warnings: list[str] = []
    for tool_id in selected:
        tool_ok, w = _init_one_tool(tool_id, args, project_root, mcp_bin, selected)
        ok = ok or tool_ok
        all_warnings.extend(w)

    # ── Project-level config and assets (skills, agents) ──
    _build_system = detect_build_system(project_root)

    if not args.dry_run and not args.instructions_only:
        from ..config.settings import (
            _PROJECT_DEFAULTS_TEMPLATE,
            _PROJECT_LOCAL_DEFAULTS_TEMPLATE,
            update_global_config,
        )
        update_global_config(fix=True)
        _check_config_file(project_root, ".fw-context/config.toml", _PROJECT_DEFAULTS_TEMPLATE, fix=True)
        _check_config_file(project_root, ".fw-context/local.toml", _PROJECT_LOCAL_DEFAULTS_TEMPLATE, fix=True)

    if ok and not args.dry_run:
        if not args.instructions_only:
            proj_config = _ensure_project_config(project_root)
            local_config = _ensure_project_local_config(project_root)
            print(
                f"\n[ok] {proj_config}: shared project config ready — edit vendor_paths, project_paths, etc. (commit to git)"
            )
            print(f"[ok] {local_config}: local developer config ready — edit ollama_url, model, etc. (gitignore)")
            if _proj_cfg.llm.enabled:
                from ..llm.auto_model import resolve_embed_model
                resolve_embed_model(_proj_cfg.llm)
            _ensure_gitignore(project_root, fix=True, build_system=_build_system)
        _install_skills(dry_run=False, project_root=project_root, scope=args.scope)
        _install_agents(dry_run=False, project_root=project_root, scope=args.scope)

    # ── Diagnostic output ──
    if not args.dry_run:
        print()
        if _build_system:
            print(f"  Build system: {_build_system}")
        else:
            print("  Build system: none detected — set [build] system in config.toml")

        if _proj_cfg.llm.enabled:
            print(f"  Embed model: {_proj_cfg.llm.embed_model}")
        else:
            print("  LLM: disabled — Ollama calls will return raw prompts")

    if all_warnings:
        print("\nWarnings:")
        for w in all_warnings:
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
            logging.getLogger(__name__).warning("Could not update %s: %s", gitignore, e)
    else:
        print(f"  [info] {gitignore}: missing entries: {', '.join(missing)}")
