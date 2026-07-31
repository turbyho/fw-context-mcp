#!/usr/bin/env python3
"""Bump version and sync catalog metadata across all files.

Usage:
    python bump.py <new-version>              # bump version + sync metadata
    python bump.py <new-version> --dry-run    # preview without writing
    python bump.py --check                    # validate consistency (no changes)
    python bump.py --update-metadata          # sync tool count + descriptions
    python bump.py --update-metadata --dry-run

Updates:
    - pyproject.toml     (version field)
    - server.json        (version + packages[].version + description tool count)
    - glama.json         (version + description tool count)
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SERVER_PY = PROJECT_ROOT / "src" / "fw_context_mcp" / "mcp" / "server.py"


# ── helpers ────────────────────────────────────────────────────────────


def count_tools() -> int:
    """Count active ``mcp.tool()(…)`` registrations in server.py."""
    if not SERVER_PY.exists():
        print(f"Warning: {SERVER_PY} not found — tool count = 0", file=sys.stderr)
        return 0

    text = SERVER_PY.read_text(encoding="utf-8")
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"mcp\.tool\(\)\(", stripped):
            count += 1
    return count


def get_version_from_pyproject() -> str:
    """Read the current version from pyproject.toml."""
    path = PROJECT_ROOT / "pyproject.toml"
    content = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(content)
        return data["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError) as e:
        print(f"Error: version not found in pyproject.toml: {e}", file=sys.stderr)
        sys.exit(1)


def _make_description(tool_count: int) -> str:
    """Build the standard description string with tool count."""
    return (
        "Build-aware code intelligence for embedded C/C++ firmware "
        f"— libclang, FTS5, call graphs, {tool_count} tools"
    )


def _make_glama_description(tool_count: int) -> str:
    """Build the Glama.ai description with tool count."""
    return (
        "Build-aware code intelligence for embedded C/C++ firmware. "
        f"libclang + SQLite+FTS5, {tool_count} MCP tools for symbol "
        "search, call graphs, hotspot analysis, dead code detection, "
        "and vector search."
    )


# ── update functions ───────────────────────────────────────────────────


def bump_pyproject_toml(version: str) -> bool:
    """Update ``version = "..."`` in pyproject.toml."""
    path = PROJECT_ROOT / "pyproject.toml"
    content = path.read_text(encoding="utf-8")
    updated = re.sub(
        r'^version\s*=\s*".*"',
        f'version = "{version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == content:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def bump_server_json(version: str, tool_count: int) -> bool:
    """Update version (top-level + packages) and description tool count."""
    path = PROJECT_ROOT / "server.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False

    if data.get("version") != version:
        data["version"] = version
        changed = True

    for pkg in data.get("packages", []):
        if pkg.get("version") != version:
            pkg["version"] = version
            changed = True

    new_desc = _make_description(tool_count)
    if data.get("description") != new_desc:
        data["description"] = new_desc
        changed = True

    if changed:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return changed


def bump_glama_json(version: str, tool_count: int) -> bool:
    """Update version and description tool count in glama.json."""
    path = PROJECT_ROOT / "glama.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False

    if data.get("version") != version:
        data["version"] = version
        changed = True

    new_desc = _make_glama_description(tool_count)
    if data.get("description") != new_desc:
        data["description"] = new_desc
        changed = True

    if changed:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return changed


# ── consistency check ──────────────────────────────────────────────────


def check_consistency() -> int:
    """Validate that all files agree on version and tool count.

    Returns:
        0 when everything is consistent, 1 when mismatches are found.
    """
    expected_tools = count_tools()
    pyproject_ver = get_version_from_pyproject()

    errors: list[str] = []

    # --- server.json ---
    server_path = PROJECT_ROOT / "server.json"
    if server_path.exists():
        server = json.loads(server_path.read_text(encoding="utf-8"))
        if server.get("version") != pyproject_ver:
            errors.append(
                f"server.json version {server.get('version')!r} != "
                f"pyproject.toml version {pyproject_ver!r}"
            )
        for i, pkg in enumerate(server.get("packages", [])):
            if pkg.get("version") != pyproject_ver:
                errors.append(
                    f"server.json packages[{i}].version {pkg.get('version')!r} "
                    f"!= pyproject.toml version {pyproject_ver!r}"
                )
        expected_desc = _make_description(expected_tools)
        if server.get("description") != expected_desc:
            errors.append(
                f"server.json description tool count mismatch "
                f"(expected {expected_tools}, got {server.get('description')!r})"
            )
    else:
        errors.append("server.json not found")

    # --- glama.json ---
    glama_path = PROJECT_ROOT / "glama.json"
    if glama_path.exists():
        glama = json.loads(glama_path.read_text(encoding="utf-8"))
        if glama.get("version") != pyproject_ver:
            errors.append(
                f"glama.json version {glama.get('version')!r} != "
                f"pyproject.toml version {pyproject_ver!r}"
            )
        expected_glama_desc = _make_glama_description(expected_tools)
        if glama.get("description") != expected_glama_desc:
            errors.append(
                f"glama.json description tool count mismatch "
                f"(expected {expected_tools})"
            )
    else:
        errors.append("glama.json not found")

    # --- report ---
    print(f"Tool count:  {expected_tools}")
    print(f"Version:     {pyproject_ver}")
    print()

    if errors:
        print(f"✗ {len(errors)} inconsistency(ies) found:")
        for e in errors:
            print(f"  - {e}")
        return 1
    else:
        print("✓ All files consistent")
        return 0


# ── main ────────────────────────────────────────────────────────────────


def _print_dry_run_bump(version: str, tool_count: int) -> None:
    """Preview what would change during a version bump."""
    print(f"[dry-run] Would bump to {version} (tool count: {tool_count})")
    print()
    print("  pyproject.toml  version ->", version)

    server_path = PROJECT_ROOT / "server.json"
    if server_path.exists():
        print(f"  server.json     version -> {version} (2 locations)")
        print(f"  server.json     description tool count -> {tool_count}")

    glama_path = PROJECT_ROOT / "glama.json"
    if glama_path.exists():
        print(f"  glama.json      version -> {version}")
        print(f"  glama.json      description tool count -> {tool_count}")


def _print_dry_run_metadata(tool_count: int) -> None:
    """Preview what would change during a metadata-only update."""
    print(f"[dry-run] Would update metadata (tool count: {tool_count})")
    print()
    print(f"  server.json     description tool count -> {tool_count}")
    print(f"  glama.json      description tool count -> {tool_count}")


def main() -> None:
    dry_run = "--dry-run" in sys.argv or "--dryrun" in sys.argv
    check_mode = "--check" in sys.argv
    update_meta = "--update-metadata" in sys.argv

    if check_mode:
        sys.exit(check_consistency())

    tool_count = count_tools()

    if update_meta:
        version = get_version_from_pyproject()

        if dry_run:
            _print_dry_run_metadata(tool_count)
            return

        bumped: list[str] = []
        if bump_server_json(version, tool_count):
            bumped.append("server.json")
        if bump_glama_json(version, tool_count):
            bumped.append("glama.json")

        if bumped:
            print(f"Metadata synced ({len(bumped)} file(s)):")
            for f in bumped:
                print(f"  {f}  tool count -> {tool_count}")
        else:
            print(f"Nothing changed — metadata already in sync (tool count: {tool_count}).")
        return

    # -- version bump mode --
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(
            "Usage: python bump.py <new-version> [--dry-run]",
            file=sys.stderr,
        )
        print(
            "       python bump.py --check              Validate consistency",
            file=sys.stderr,
        )
        print(
            "       python bump.py --update-metadata     Sync tool count + descriptions",
            file=sys.stderr,
        )
        sys.exit(1)

    version = args[0].strip()

    if not re.match(r"^\d+\.\d+\.\d+", version):
        print(
            f"Error: '{version}' is not a valid semver (expected N.N.N...)",
            file=sys.stderr,
        )
        sys.exit(1)

    if dry_run:
        _print_dry_run_bump(version, tool_count)
        return

    bumped: list[str] = []
    if bump_pyproject_toml(version):
        bumped.append("pyproject.toml")
        print(f"  pyproject.toml  version -> {version}")

    if bump_server_json(version, tool_count):
        bumped.append("server.json")
        print(f"  server.json     version -> {version} (2 locations)")
        print(f"  server.json     description tool count -> {tool_count}")

    if bump_glama_json(version, tool_count):
        bumped.append("glama.json")
        print(f"  glama.json      version -> {version}")
        print(f"  glama.json      description tool count -> {tool_count}")

    if bumped:
        print()
        print(f"Version bumped to {version} ({len(bumped)} file(s))")
        print("Run 'git diff' to verify changes, then commit.")
    else:
        print(f"Nothing changed — version {version} is already set everywhere.")


if __name__ == "__main__":
    main()
