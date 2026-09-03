"""``fw-context init`` — register fw-context with AI coding assistants.

This command discovers installed AI tools (Claude Code, Codex, Cursor,
OpenCode, etc.) and injects fw-context usage instructions plus MCP server
registration into their configuration files.

Injection has two parts:
1. **MCP registration** — registers the ``fw-context-mcp`` MCP server so
   the assistant can invoke fw-context tools at runtime.
2. **Instruction injection** — writes a CRITICAL block into each agent
   file (CLAUDE.md, AGENTS.md, .cursorrules, etc.) telling the assistant
   to use fw-context tools instead of raw file reads for C/C++ code.

WHY: Manual configuration is error-prone and tool-specific.  This command
automates it across 7+ AI assistants and 3 scopes (global, project, all),
respecting each tool's config format and inheritance model.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.tools import AiTool
    from ..deps import DepCheckResult


def _write_build_key(project_root: Path, key: str, value: str) -> None:
    """Write a key-value pair into the [build] section of local.toml.

    Uses ``set_key`` from the TOML editor to update or insert the key.
    The file is created automatically if it does not exist.

    WHY: Build environment detection (Python path, activate script) is
    done once during ``fw-context init`` and stored in local config.
    Subsequent ``fw-context index`` calls read these values rather than
    re-detecting them, which would require running Mbed CLI / west
    commands again.
    """
    from fw_context_mcp.config._toml_editor import set_key

    local_path = project_root / ".fw-context" / "local.toml"
    set_key(local_path, "build", key, value)


def _install_skills(
    dry_run: bool = False,
    project_root: Path | None = None,
    scope: str = "project",
) -> bool:
    """Copy fw-context review skills to AI tool skills directories.

    Installs the fw-review skill globally (when scope is
    ``"global"`` or ``"all"``) and at the project level (when scope is
    ``"project"`` or ``"all"``, and ``project_root`` is provided).

    Skill directories are driven by the ``TOOLS`` registry — no tool
    paths are hardcoded here.  Project-local skills override global
    ones — if a project has its own copy the user intentionally
    customized it.

    WHY: The fw-review skill teaches AI assistants how to review C/C++
    firmware code using fw-context tools.  Installing it automatically
    saves the user from copying skill files manually across multiple
    tools and projects.
    """
    from ..config.tools import (
        CROSS_TOOL_SKILL_DIRS_GLOBAL,
        CROSS_TOOL_SKILL_DIRS_PROJECT,
        TOOLS,
    )
    pkg_dir = Path(__file__).resolve().parent.parent  # src/fw_context_mcp/
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

    WHY: Detection is the first step of ``fw-context init``.  The
    tool must know which assistants are present before it can inject
    instructions into their configuration files.
    """
    from ..config.tools import TOOLS

    return [tid for tid, t in TOOLS.items() if t.is_detected()]


def _build_agent_targets(
    scope: str, project_root: Path | None
) -> list[tuple[Path, str, list[str], bool]]:
    """Build the list of (directory, tool_id, file_patterns, strip_name) tuples.

    Iterates ``TOOLS`` for each tool's agent directories and
    ``CROSS_TOOL_AGENT_DIRS_*`` for directories shared across tools.

    WHY: Agent directories differ per tool and per scope.  This
    function normalizes all of them into a flat list so downstream
    injection code does not need to know about individual tool
    layouts.
    """
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


def _inject_critical_block(
    agents_dir: Path,
    patterns: list[str],
    *,
    dry_run: bool = False,
    critical_block: str,
) -> bool:
    """Inject CRITICAL block into ALL existing agent files in *agents_dir*.

    Scans *agents_dir* for files matching *patterns*, injecting
    *critical_block* into each one (TOML vs markdown handled automatically).
    Returns ``True`` if any files were updated (or would be in dry-run mode).

    WHY: Existing agent files already contain user content.  We must
    inject the fw-context critical block without overwriting the rest
    of the file.  This requires format-aware injection: TOML uses
    section headers, markdown uses HTML-style markers.
    """
    from ._mcp import _inject_agent_section, _inject_agent_toml_section

    installed = False
    existing_files: list[Path] = []
    for pattern in patterns:
        existing_files.extend(sorted(agents_dir.glob(pattern)))

    for agent_path in existing_files:
        if dry_run:
            print(f"  [dry-run] {agent_path}: would UPDATE fw-context section")
            installed = True
            continue

        if agent_path.suffix == ".toml":
            _inject_agent_toml_section(agent_path, critical_block, "fw-context")
        else:
            _inject_agent_section(agent_path, critical_block, "fw-context")
        print(f"  [ok] {agent_path}: updated fw-context section")
        installed = True

    return installed


def _install_template_agents(
    agents_dir: Path,
    templates: list[Path],
    patterns: list[str],
    *,
    strip_name: bool = False,
    dry_run: bool = False,
    scope: str = "project",
) -> bool:
    """Install template agents into *agents_dir* where not yet present.

    Handles three formats:
    - ``.md`` → ``.md`` (standard — Claude Code, Cursor, Windsurf)
    - ``.md`` → ``.toml`` (Codex conversion)
    - ``.md`` → ``.mdc`` (Cursor rules, same content)

    Returns ``True`` if any templates were installed (or would be in dry-run).

    WHY: Each AI tool uses a different config format.  We ship a single
    canonical markdown template and convert it per-tool at install time.
    This avoids maintaining duplicate templates that would inevitably
    drift out of sync.
    """
    from ._mcp import _convert_agent_md_to_toml

    if not templates:
        return False
    if not agents_dir.exists() and scope != "all":
        return False

    installed = False
    for template_path in templates:
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


def _install_agents(dry_run: bool = False, project_root: Path | None = None, scope: str = "project") -> bool:
    """Inject fw-context CRITICAL block into ALL agent files across ALL AI tools.

    Scans agent directories for every registered tool (and cross-tool
    ``.agents/`` standard directories), injecting ``AGENT_CRITICAL_BLOCK``
    into each existing agent file.  Also installs template agents from
    ``data/agents/`` into compatible directories where the template does
    not yet exist.

    Agent directories and file patterns are driven by the ``TOOLS``
    registry — no tool paths are hardcoded here.

    WHY: Agent files are the primary way AI assistants learn project-
    specific conventions.  Injecting the CRITICAL block ensures every
    assistant in the project knows to use fw-context for C/C++ code.
    New agent files receive the full template; existing ones get only
    the injected section to preserve user content.
    """
    from ..config.tools import AGENT_CRITICAL_BLOCK
    pkg_dir = Path(__file__).resolve().parent.parent  # src/fw_context_mcp/
    agents_src = pkg_dir / "data" / "agents"
    templates = sorted(agents_src.glob("*.md")) if agents_src.is_dir() else []

    targets = _build_agent_targets(scope, project_root)

    installed = False
    for agents_dir, _tool_id, patterns, strip_name in targets:
        if _inject_critical_block(
            agents_dir, patterns, dry_run=dry_run, critical_block=AGENT_CRITICAL_BLOCK
        ):
            installed = True
        if _install_template_agents(
            agents_dir, templates, patterns,
            strip_name=strip_name, dry_run=dry_run, scope=scope,
        ):
            installed = True

    return installed


def _select_init_tools(args: argparse.Namespace, project_root: Path) -> list[str] | None:
    """Select which AI tools to act on.

    Returns a list of tool IDs, or None if a fatal error occurred (caller returns 1).

    WHY: The user can pass ``--tool claude-code`` to target one tool,
    a comma-separated list for multiple, or omit it to auto-detect all
    installed tools.  Detection uses project-level and system-wide
    heuristics because some tools only install config globally while
    others create project-local files.
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


def _handle_inheritance(
    tool: AiTool,
    tool_id: str,
    *,
    force: bool,
    selected: list[str],
) -> tuple[bool, bool, list[str]]:
    """Resolve tool inheritance — whether to skip, force, or proceed normally.

    Returns ``(should_return, ok, warnings)``.  When *should_return* is
    ``True`` the caller must return ``(ok, warnings)`` immediately.
    Otherwise *ok* is the initial ok flag to carry forward.

    WHY: Some AI tools inherit configuration from a parent tool
    (e.g. kilocode inherits from claude-code).  When the parent already
    has fw-context instructions and is detected, injecting into the child
    is redundant and risks duplication.  The ``--force`` flag overrides
    this safety check.
    """
    from ..config.tools import TOOLS

    if not tool.inherits_from:
        return False, False, []

    parent = TOOLS.get(tool.inherits_from)
    parent_name = parent.name if parent else tool.inherits_from
    parent_ok = parent and parent.is_detected()

    if parent_ok and tool_id in selected and tool.inherits_from in selected:
        print(f"  [info] Inherits from {parent_name} — already handled above, skipping")
        return True, True, []

    if parent_ok:
        print(f"  [info] Inherits from {parent_name} which has fw-context instructions")
        if not force:
            print("  [skip] Nothing to do. Use --force to inject anyway.")
            return True, True, []
        print("  [force] Injecting despite inheritance...")
        return False, True, []

    print(f"  [warn] Inherits from {parent_name} but parent NOT DETECTED")
    print("  [info] Injecting instructions anyway...")
    return False, False, []


def _inject_instructions(
    tool: AiTool,
    args: argparse.Namespace,
    project_root: Path,
) -> tuple[bool, list[str]]:
    """Write rendered instructions into every configured target file.

    Handles collision detection (skillshare-managed, unmarked content),
    dry-run preview, and both ``marked_section`` and ``separate_file``
    write methods.

    Returns ``(ok, warnings)``.

    WHY: Collision detection is critical — some directories are managed
    by external tools (skillshare) and overwriting them would break other
    integrations.  Unmarked content detection prevents accidental
    overwrite of user-written instructions that happen to contain
    fw-context references.
    """
    from ..config.tools import check_target
    from ._mcp import _update_marked_section

    ok = False
    warnings: list[str] = []

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
    from ..config.tools import TOOLS
    from ._mcp import _register_mcp

    tool = TOOLS[tool_id]
    print(f"\n── {tool.name} ({tool_id}) ──")
    warnings: list[str] = []

    # ── Inheritance ──
    should_return, ok, inh_warnings = _handle_inheritance(
        tool, tool_id, force=args.force, selected=selected,
    )
    warnings.extend(inh_warnings)
    if should_return:
        return ok, warnings

    # ── MCP registration ──
    if not args.instructions_only and mcp_bin and (tool.mcp_registration or tool.mcp_config_file):
        if args.dry_run:
            _register_mcp(tool, mcp_bin, dry_run=True)
        else:
            _register_mcp(tool, mcp_bin)

    # ── Instruction injection ──
    inj_ok, inj_warnings = _inject_instructions(tool, args, project_root)
    ok = ok or inj_ok
    warnings.extend(inj_warnings)

    return ok, warnings


def cmd_init(args: argparse.Namespace) -> int:
    """Register fw-context with AI assistants and provision the project.

    One command that checks and auto-fixes dependencies, detects (or asks
    for) the build system, generates ``compile_commands.json`` when
    feasible, registers AI tools, and prints the remaining-steps checklist.

    Per-tool injection with inheritance awareness, collision detection,
    dry-run preview, and ``--list-tools`` discovery.  Idempotent — safe to
    re-run; configured sections are shown with a ``Change? [y/N]`` prompt
    (default N) and already-built artifacts are reused.

    ``--quick`` (and the ``quickstart`` alias) skips only AI-tool
    registration — project ID, config, deps, build, and checklist still run.
    """
    from ..config import load as load_project_config
    from ..config.settings import _update_local_toml, _write_project_id
    from ..config.settings import generate_project_id as _gen_pid
    from ..config.tools import TOOLS
    from ..utils import resolve_project_root
    from ._mcp import _resolve_mcp_bin

    quick = getattr(args, "quick", False)
    non_interactive = getattr(args, "non_interactive", False) or not sys.stdin.isatty()
    skip_doctor = getattr(args, "skip_doctor", False)
    skip_build = getattr(args, "skip_build", False)

    if getattr(args, "list_tools", False) and not quick:
        print("Supported AI assistants:\n")
        for tool in TOOLS.values():
            print(f"  {tool.status()}")
        print("\nRun 'fw-context init --tool <id>' to set up a specific tool.")
        return 0

    mcp_bin = _resolve_mcp_bin()
    project_root = resolve_project_root(args.project)

    # ── 1. Dependency audit + auto-fix (MUST precede any config write) ──
    # Project-ID generation writes config via tomli-w (set_key) — on a clean
    # venv without tomli-w this must run AFTER the deps phase installs it.
    before_results: list[DepCheckResult] = []
    after_results: list[DepCheckResult] = []
    if skip_doctor and not args.dry_run:
        # --skip-doctor bypasses the deps auto-fix, but config writes still
        # need tomli-w — verify it before the first set_key (project ID).
        # --dry-run writes nothing, so the check is skipped there.
        from ..deps._checks import _require_tomli_w

        if not _require_tomli_w():
            print("[error] tomli-w is missing and --skip-doctor was given.", file=sys.stderr)
            print("  Install it with:  pip install tomli-w", file=sys.stderr)
            print("  (tomli-w is required to write config during init.)", file=sys.stderr)
            return 1
    elif not skip_doctor:
        from ._init_deps import _run_deps_auto_fix

        try:
            before_results, after_results = _run_deps_auto_fix(project_root, dry_run=args.dry_run)
        except Exception as exc:  # a fatal deps error aborts provisioning
            print(f"[error] dependency audit/auto-fix failed: {exc}", file=sys.stderr)
            return 1

    # ── 2. Build system (config-first) + interactive fallback ──
    _proj_cfg = load_project_config(project_root=project_root)
    from ._init_interactive import resolve_build_env, resolve_build_params, resolve_build_system

    build_system = resolve_build_system(
        project_root, _proj_cfg, non_interactive=non_interactive, dry_run=args.dry_run,
    )
    resolve_build_params(
        project_root, _proj_cfg, build_system, non_interactive=non_interactive, dry_run=args.dry_run,
    )

    # ── 3. Project ID + global registry (after deps) ──
    provisioned = False
    if not _proj_cfg.project.id:
        if args.dry_run:
            print("  [dry-run] would generate a project ID")
        else:
            new_id = _gen_pid()
            _write_project_id(project_root, new_id)
            print(f"Project ID: {new_id}")
            _proj_cfg = load_project_config(project_root=project_root)
    else:
        print(f"Project ID: {_proj_cfg.project.id} (existing)")

    proj_id = _proj_cfg.project.id
    if proj_id:
        provisioned = True
        if not args.dry_run:
            from ..config.global_db import open_global_db, upsert_project_registry

            proj_name = getattr(args, "name", None) or _proj_cfg.project.name or project_root.name
            try:
                glob_conn = open_global_db()
                upsert_project_registry(
                    glob_conn, proj_id, proj_name, build_system or "unknown", str(project_root)
                )
                glob_conn.close()
            except Exception as exc:  # registry is best-effort — never fatal
                print(f"  [warn] could not update global registry: {exc}")

    # Reload config so board/fqbn/... written above are visible to the build.
    if build_system or _proj_cfg.build.system:
        _proj_cfg = load_project_config(project_root=project_root)

    # ── 4. Build environment auto-detect (config-first, machine-specific) ──
    resolve_build_env(
        project_root, _proj_cfg, build_system, non_interactive=non_interactive, dry_run=args.dry_run,
    )

    # Reload so the python/activate/idf_path written by resolve_build_env are
    # visible to the auto-build below.  Without this the ESP-IDF/Zephyr build
    # runs without sourcing export.sh / nordic_minimal_setup.sh — the toolchain
    # is then missing from PATH and the build fails at configure time.
    _proj_cfg = load_project_config(project_root=project_root)

    # ── 5. Config templates (NOT gated by ok / instructions_only) ──
    if not args.dry_run:
        from ..config.settings import (
            _PROJECT_DEFAULTS_TEMPLATE,
            _PROJECT_LOCAL_DEFAULTS_TEMPLATE,
            update_global_config,
        )
        update_global_config(fix=True)
        _check_config_file(project_root, ".fw-context/config.toml", _PROJECT_DEFAULTS_TEMPLATE, fix=True)
        _check_config_file(project_root, ".fw-context/local.toml", _PROJECT_LOCAL_DEFAULTS_TEMPLATE, fix=True)

    # ── 6. Auto-build compile_commands.json ──
    build_ok, cc_path, cc_err = False, None, None
    if args.dry_run:
        print("  [dry-run] would generate compile_commands.json when feasible")
    elif _proj_cfg.build.variants:
        print("  [build] multi-variant project — build via 'fw-context index --build'")
    elif skip_build:
        print("  [build] skipped (--skip-build)")
    else:
        from ._init_build import _auto_build_if_possible

        build_ok, cc_path, cc_err = _auto_build_if_possible(project_root, build_system, _proj_cfg)
        if not build_ok:
            print(f"  [build] compile_commands.json not generated: {cc_err}")

    # ── 7. AI tool registration (unless quick) ──
    ok = False
    all_warnings: list[str] = []
    tools_registered: list[str] = []
    if not quick:
        selected = _select_init_tools(args, project_root)
        if selected is None:
            return 1
        for tool_id in selected:
            tool_ok, w = _init_one_tool(tool_id, args, project_root, mcp_bin, selected)
            if tool_ok:
                tools_registered.append(tool_id)
            ok = ok or tool_ok
            all_warnings.extend(w)

    # ── 8. gitignore + skills + agents (gitignore NOT ok-gated) ──
    # The gitignore step runs in a dry run as well, with fix=False.  It can
    # REMOVE a line — a blanket `.fw-context/` that hides config.toml — and
    # a dry run must show that before the run that does it.
    _ensure_gitignore(project_root, fix=not args.dry_run, build_system=build_system)
    if not args.dry_run:
        if not quick:
            scope = getattr(args, "scope", "project")
            _install_skills(dry_run=False, project_root=project_root, scope=scope)
            _install_agents(dry_run=False, project_root=project_root, scope=scope)

    # ── 9. LLM config (interactive; no-op in non-interactive mode) ──
    if not args.dry_run:
        from ._init_interactive import prompt_llm_config

        prompt_llm_config(project_root, _proj_cfg, non_interactive=non_interactive)

    # ── 10. Resolve embed model (skip_pull) + persist name ──
    if not args.dry_run:
        _proj_cfg = load_project_config(project_root=project_root)
        from ..llm.auto_model import resolve_embed_model

        resolve_embed_model(_proj_cfg.llm, skip_pull=True)
        # Persist the resolved name so the next load() short-circuits and
        # never auto-pulls without consent (init is the single writer).
        if _proj_cfg.llm.enabled and _proj_cfg.llm.embed_model:
            _update_local_toml(project_root, {"embed_model": _proj_cfg.llm.embed_model})

    # ── 11. Diagnostic output ──
    if not args.dry_run:
        print()
        if build_system:
            print(f"  Build system: {build_system}")
        else:
            print("  Build system: none detected — set [build] system in config.toml")

    if all_warnings:
        print("\nWarnings:")
        for w in all_warnings:  # type: ignore[assignment]  # mypy false positive — w is str from list[str]
            print(f"  ⚠ {w}")

    # ── 12. Checklist ──
    if not args.dry_run:
        from ._init_deps import _index_exists, _model_status, _print_checklist

        _print_checklist(
            after_results,
            _model_status(project_root),
            build_ok=build_ok,
            cc_path=cc_path,
            build_system=build_system,
            tools_registered=tools_registered,
            index_exists=_index_exists(_proj_cfg),
            build_skipped=skip_build,
            multi_variant=bool(_proj_cfg.build.variants),
        )

    # ── Return logic ──
    if args.dry_run:
        print("\nDry-run complete. Run without --dry-run to apply changes.")
        return 0
    if quick:
        if not provisioned:
            print("\nProvisioning incomplete — see errors above.", file=sys.stderr)
            return 1
        print("\nQuickstart complete. Project provisioned (AI tool registration skipped).")
        return 0
    if ok or provisioned:
        print("\nSetup complete. Restart your AI assistant to pick up changes.")
        return 0
    print("\nNo changes made.", file=sys.stderr)
    return 1


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

    from fw_context_mcp.config._toml_editor import merge_template

    added = merge_template(path, template)
    if added:
        if fix:
            print(f"  [fix] {path}: added {', '.join(added)}")
        else:
            print(f"  [info] {path}: missing options: {', '.join(added)}")
    else:
        print(f"  [ok] {path}")


#: The two lines that control what git sees under ``.fw-context/``.
#:
#: The ``/*`` decides the whole rule.  ``.fw-context/`` excludes the
#: DIRECTORY, and git does not descend into an excluded directory, thus no
#: later negation can bring ``config.toml`` back.  ``.fw-context/*``
#: excludes the CONTENTS, git reads each entry, and the negation applies.
#:
#: The ``**/`` prefix gives the rule every depth.  A repository can hold
#: more than one initialized project (a bootloader beside an application),
#: and only the root usually has a ``.gitignore``.  Without the prefix the
#: pair holds a leading path element, which anchors it to the root, and
#: the ``local.toml`` of a project one level down reaches git.
#:
#: The order is part of the rule — a later line wins in ``.gitignore``,
#: thus the negation must FOLLOW the exclude.
FW_CONTEXT_IGNORE_PAIR = ("**/.fw-context/*", "!**/.fw-context/config.toml")

#: Lines that ``FW_CONTEXT_IGNORE_PAIR`` replaces, thus ``init`` removes
#: them.  The four blanket forms exclude the directory and hide
#: ``config.toml``; the two root-anchored lines are the same pair without
#: the ``**/`` prefix and miss every project below the root.
FW_CONTEXT_SUPERSEDED_IGNORES = frozenset(
    {
        ".fw-context/",
        ".fw-context",
        "/.fw-context/",
        "/.fw-context",
        ".fw-context/*",
        "!.fw-context/config.toml",
    }
)


def _pair_is_ordered(lines: list[str]) -> bool:
    """Tell whether the ``.fw-context`` exclude and its negation are correct.

    Both lines must be present, and the negation must come after the
    exclude.  A negation before the exclude has no result, because the
    last line that matches a path wins.

    Args:
        lines: Lines of the ``.gitignore``, with their line ends removed.

    Returns:
        True when the pair is present and in the correct order.
    """
    exclude = negation = None
    for index, line in enumerate(lines):
        text = line.strip()
        if text == FW_CONTEXT_IGNORE_PAIR[0]:
            exclude = index
        elif text == FW_CONTEXT_IGNORE_PAIR[1]:
            negation = index
    return exclude is not None and negation is not None and negation > exclude


def plan_gitignore(
    raw: list[str], build_system: str | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Decide what a ``.gitignore`` must lose and gain.

    Pure — it reads lines and returns lines, thus the rules can be tested
    without a file.  ``_ensure_gitignore`` does the input and the output.

    ``.fw-context/`` holds one file that the team shares and several that
    it must not.  ``config.toml`` holds the build configuration of the
    project and belongs in the repository, thus every developer gets the
    same index.  ``local.toml`` holds the settings of one developer
    (paths, API keys), and ``build/`` and ``autobuild/`` hold generated
    output; none of the three belongs in the repository.
    ``FW_CONTEXT_IGNORE_PAIR`` gives that split, and its documentation
    tells why the exclude needs the ``/*``.

    A blanket ``.fw-context/`` line goes.  Such a line hides
    ``config.toml``, and a project that got one from an earlier version
    of fw-context, or from a person, cannot commit the file until the
    line goes.  A pair in the wrong order goes for the same reason, and
    comes back in the correct order.  No other line is touched.

    Args:
        raw: Lines of the current ``.gitignore``, with the line ends
            removed.  An empty list stands for a file that is not there.
        build_system: Build system key.  ``"mbed-os"`` adds
            ``mbed_config.h``, the build-generated header that lands in
            the project root.

    Returns:
        ``(kept, removed, append)`` — the lines that stay, the text of
        each line that must go, and the entries to write at the end.
    """
    needs_pair = not _pair_is_ordered(raw)
    # A stray half of the pair goes together with the superseded lines, so
    # that both lines come back as one ordered block.
    drop = set(FW_CONTEXT_SUPERSEDED_IGNORES)
    if needs_pair:
        drop.update(FW_CONTEXT_IGNORE_PAIR)

    removed = sorted({line.strip() for line in raw if line.strip() in drop})
    kept = [line for line in raw if line.strip() not in drop]
    present = {line.strip() for line in kept if line.strip() and not line.strip().startswith("#")}

    plain = ["compile_commands.json"]
    if build_system == "mbed-os":
        plain.append("mbed_config.h")
    append = [entry for entry in plain if entry not in present]
    if needs_pair:
        append.extend(FW_CONTEXT_IGNORE_PAIR)

    return kept, removed, append


def _ensure_gitignore(project_root: Path, *, fix: bool = False, build_system: str | None = None) -> None:
    """Make git ignore the fw-context artifacts, but keep ``config.toml``.

    ``plan_gitignore`` holds the rules and the reasons for them.  This
    function reads the file, reports the plan, and — with *fix* — writes
    it.  It is idempotent: more runs add no duplicates.

    Args:
        project_root: Directory that holds the ``.gitignore``.
        fix: True writes the file.  False only reports the changes.
        build_system: Build system key, for the Mbed OS entry.
    """
    gitignore = project_root / ".gitignore"
    try:
        raw = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    except (OSError, PermissionError):
        return

    kept, removed, append = plan_gitignore(raw, build_system)
    if not append and not removed:
        print(f"  [ok] {gitignore}")
        return

    verb, mark = ("would remove", "[info]") if not fix else ("removed", "[fix]")
    if removed:
        print(f"  {mark} {gitignore}: {verb} {', '.join(removed)} — it hides .fw-context/config.toml")
    if append:
        word = "missing entries:" if not fix else "added"
        print(f"  {mark} {gitignore}: {word} {', '.join(append)}")
    if not fix:
        return

    try:
        _write_gitignore(gitignore, kept if removed else None, append)
    except (OSError, PermissionError) as e:
        logging.getLogger(__name__).warning("Could not update %s: %s", gitignore, e)


def _write_gitignore(gitignore: Path, kept: list[str] | None, append: list[str]) -> None:
    """Write the planned ``.gitignore``.

    Args:
        gitignore: File to write.
        kept: Lines to keep, when a line was removed and the file needs a
            rewrite.  ``None`` leaves the current content in place.
        append: Entries to write at the end, under one comment.
    """
    if kept is not None:
        gitignore.write_text("\n".join(kept) + "\n" if kept else "", encoding="utf-8")
    if not append:
        return
    with gitignore.open("a", encoding="utf-8") as f:
        if gitignore.stat().st_size > 0:
            f.write("\n")
        f.write("# fw-context\n")
        for entry in append:
            f.write(f"{entry}\n")
