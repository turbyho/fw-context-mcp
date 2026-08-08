"""MCP server registration helpers for ``fw-context init``.

Registers fw-context-mcp as an MCP server in AI coding tools (Claude Code,
OpenCode, Codex, Cursor, etc.) during ``fw-context init``.  Each tool
stores MCP configuration differently — CLI commands, JSON files, TOML files,
or markdown with YAML frontmatter.  This module provides a unified interface
that dispatches to the right registration method per tool.

WHY registration during init: users should not need to manually edit MCP
config files — the tool should integrate itself into the user's existing
AI coding setup automatically.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_mcp_bin() -> str | None:
    """Find the fw-context-mcp binary, preferring canonical install over dev venv.

    Search order (priority):
    1. ``~/.local/bin/fw-context-mcp`` — canonical user install (pipx / pip --user)
    2. ``~/.fw-context/.venv/bin/fw-context-mcp`` — managed venv
    3. ``shutil.which("fw-context-mcp")`` — system PATH
    4. ``sys.executable / fw-context-mcp`` — dev venv (editable install)

    WHY canonical install first: the managed venv and system PATH may point
    to different versions or stale installations.  The canonical path is
    the most likely to match the currently installed package.
    """
    for candidate in [
        Path.home() / ".local" / "bin" / "fw-context-mcp",
        Path.home() / ".fw-context" / ".venv" / "bin" / "fw-context-mcp",
    ]:
        if candidate.exists():
            return str(candidate)
    mcp_bin = shutil.which("fw-context-mcp")
    if mcp_bin:
        return mcp_bin
    dev_candidate = Path(sys.executable).parent / "fw-context-mcp"
    if dev_candidate.exists():
        return str(dev_candidate)
    return None


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
    if not tool.mcp_config_file:
        return

    config_path = Path(os.path.expanduser(tool.mcp_config_file))

    try:
        if config_path.exists():
            raw = config_path.read_text(encoding="utf-8")
            # Strip JSONC comments (// and /* */) before parsing.
            # OpenCode config files support comments for human readability
            # but they are not valid JSON.  We strip them before json.loads()
            # to avoid parse errors, then write back as plain JSON.
            # Regex patterns:
            #   /\*.*?\*/ — block comments (non-greedy, dotall for multiline)
            #   (?<!:)//.*$ — line comments (// not preceded by colon, to
            #     preserve URLs like "http://")
            raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
            raw = re.sub(r'(?<!:)//.*$', '', raw, flags=re.MULTILINE)
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

    # Always create a backup before modifying, so users can recover
    # their custom content if the section detection is wrong.
    if path.exists() and existing.strip():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(existing, encoding="utf-8")

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
        after = existing[existing.index(end_tag) + len(end_tag) :]
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


def _inject_agent_toml_section(path: Path, content: str, marker: str) -> None:
    """Inject a ``# marker ... # /marker`` block into a Codex TOML agent file.

    Codex agent files use TOML format with an ``[instructions]`` key that
    holds freeform text.  The fw-context block is inserted into the
    ``[instructions]`` section using ``# fw-context`` comment markers.
    When no ``[instructions]`` section exists, one is created at the end
    of the file.
    """
    start_comment = f"# {marker}"
    end_comment = f"# /{marker}"

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # If markers already exist, replace between them
    if start_comment in existing and end_comment in existing:
        before = existing[: existing.index(start_comment)]
        after = existing[existing.index(end_comment) + len(end_comment) :]
        updated = (
            before.rstrip("\n") + "\n" + start_comment + "\n" + content + "\n" + end_comment + "\n" + after.lstrip("\n")
        )
    else:
        # Find [instructions] section and insert the block there
        instructions_match = re.search(r"^\[instructions\]\s*$", existing, re.MULTILINE)
        if instructions_match:
            # Find end of instructions section (next TOML section or EOF)
            section_end = len(existing)
            next_section = re.search(r"^\[", existing[instructions_match.end() :], re.MULTILINE)
            if next_section:
                section_end = instructions_match.end() + next_section.start()
            before = existing[:section_end]
            after = existing[section_end:]
            updated = before.rstrip("\n") + "\n" + start_comment + "\n" + content + "\n" + end_comment + "\n" + after
        else:
            # No [instructions] section — create one at end of file
            updated = (
                existing.rstrip("\n")
                + "\n\n[instructions]\n"
                + start_comment
                + "\n"
                + content
                + "\n"
                + end_comment
                + "\n"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")


def _convert_agent_md_to_toml(md_content: str) -> str:
    """Convert agent ``.md`` template to Codex ``.toml`` format.

    YAML frontmatter → TOML comments, ``<!-- fw-context -->`` markers
    → ``# fw-context``, body wrapped in ``[instructions]`` section.
    """
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
