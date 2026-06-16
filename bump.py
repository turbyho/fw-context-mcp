#!/usr/bin/env python3
"""Bump the project version across all metadata files.

Usage:
    python bump.py <new-version>           # bump and print summary
    python bump.py <new-version> --dry-run  # preview without writing

Updates:
    - pyproject.toml  (version field)
    - server.json     (top-level version + each package version)
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def bump_pyproject_toml(version: str) -> bool:
    path = PROJECT_ROOT / "pyproject.toml"
    content = path.read_text(encoding="utf-8")
    updated = re.sub(
        r'^version = ".*"',
        f'version = "{version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == content:
        return False
    path.write_text(updated, encoding="utf-8")
    print(f"  pyproject.toml  version -> {version}")
    return True


def bump_server_json(version: str) -> bool:
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

    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  server.json     version -> {version} (2 locations)")
    return changed


def main() -> None:
    dry_run = False
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if "--dry-run" in sys.argv or "--dryrun" in sys.argv:
        dry_run = True

    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <new-version> [--dry-run]", file=sys.stderr)
        sys.exit(1)

    version = args[0].strip()

    if not re.match(r"^\d+\.\d+\.\d+", version):
        print(f"Error: '{version}' is not a valid semver (expected N.N.N...)", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"[dry-run] Would bump to {version}")
        print()

    bumped: list[str] = []
    if not dry_run:
        if bump_pyproject_toml(version):
            bumped.append("pyproject.toml")
        if bump_server_json(version):
            bumped.append("server.json")
    else:
        print("  pyproject.toml  (version field)")
        print("  server.json     (2 locations)")
        bumped = ["pyproject.toml", "server.json"]

    if bumped:
        print()
        print(f"Version bumped to {version} ({len(bumped)} file(s))")
        if not dry_run:
            print("Run 'git diff' to verify changes, then commit.")
    else:
        print(f"Nothing changed — version {version} is already set everywhere.")


if __name__ == "__main__":
    main()
