"""CLI for managing ``[[build.variants]]`` in ``.fw-context/config.toml``.

``fw-context init-variants`` is the follow-up to ``fw-context init`` for an
ALREADY initialized project: it adds/removes/lists build variants without
re-running agent/deps/LLM provisioning.

``list`` reads ``[[build.variants]]`` from ``config.toml`` (what the config
declares) — NOT ``list_variants`` (MCP), which reads ``build_configs`` (what
is actually indexed).  After a change the new variants are unindexed until
``fw-context index --build`` runs.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def _fmt_kv(key: str, value) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"{key} = {value}"
    return f'{key} = "{value}"'


def _fmt_variant_block(variant: dict) -> str:
    """Format one ``[[build.variants]]`` table (scalar fields only)."""
    lines = ["[[build.variants]]"]
    for key in ("name", "description", "board", "build_dir"):
        if variant.get(key):
            lines.append(_fmt_kv(key, variant[key]))
    env = variant.get("env")
    if env:
        items = ", ".join(f'{k} = "{v}"' for k, v in sorted(env.items()))
        lines.append(f"env = {{ {items} }}")
    images = variant.get("images")
    if images:
        lines.append("images = [")
        for img in images:
            parts = [f'name = "{img.get("name", "")}"']
            if img.get("dir"):
                parts.append(f'dir = "{img["dir"]}"')
            if img.get("type"):
                parts.append(f'type = "{img["type"]}"')
            if img.get("board"):
                parts.append(f'board = "{img["board"]}"')
            lines.append(f"  {{ {', '.join(parts)} }},")
        lines.append("]")
    return "\n".join(lines)


def _list_variants(cfg_path: Path, root: Path) -> int:
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: cannot parse {cfg_path}: {e}", file=sys.stderr)
        return 1
    variants = data.get("build", {}).get("variants", [])
    if not variants:
        print("No [[build.variants]] declared in config.toml.")
        print("Add one with: fw-context init-variants add --name <n> --board <b>")
        return 0
    print(f"{len(variants)} build variant(s):")
    for v in variants:
        board = v.get("board", "")
        desc = v.get("description", "")
        print(f"  - {v.get('name', '?')}" + (f"  ({board})" if board else "") + (f"  {desc}" if desc else ""))
    return 0


def _add_variant(cfg_path: Path, args) -> int:
    name = args.name
    if not name:
        print("error: --name is required", file=sys.stderr)
        return 1
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"error: cannot parse {cfg_path}: {e}", file=sys.stderr)
        return 1
    existing = [v.get("name") for v in data.get("build", {}).get("variants", [])]
    if name in existing:
        print(f"error: variant '{name}' already exists", file=sys.stderr)
        return 1

    variant: dict = {"name": name}
    if getattr(args, "board", None):
        variant["board"] = args.board
    if getattr(args, "description", None):
        variant["description"] = args.description
    if getattr(args, "build_dir", None):
        variant["build_dir"] = args.build_dir
    env = {}
    for kv in getattr(args, "env", None) or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k.strip()] = v.strip()
    if env:
        variant["env"] = env

    from fw_context_mcp.config._toml_editor import _atomic_write

    block = "\n\n" + _fmt_variant_block(variant) + "\n"
    content = cfg_path.read_text(encoding="utf-8").rstrip("\n") + block
    _atomic_write(cfg_path, content)
    print(f"Added variant '{name}'." + ("  board=" + args.board if getattr(args, "board", None) else ""))
    print("Index it with: fw-context index --build --variant " + name)
    return 0


def _remove_variant(cfg_path: Path, name: str) -> int:
    lines = cfg_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    i = 0
    removed = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[[build.variants]]"):
            # Peek the following lines for the matching `name = "..."`.
            j = i + 1
            matched = False
            while j < len(lines) and not lines[j].strip().startswith("[["):
                m = lines[j].strip()
                if m.startswith("name ="):
                    val = m.split("=", 1)[1].strip().strip('"')
                    matched = val == name
                    break
                j += 1
            if matched:
                # Skip this whole block (until the next [[header]] or [header]).
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("["):
                    i += 1
                removed = True
                continue
        # Normal line (or a [section] header) — keep it, but also drop any
        # legacy nested [[build.variants.images]] that follow a removed block.
        out.append(line)
        i += 1
    if not removed:
        print(f"error: variant '{name}' not found", file=sys.stderr)
        return 1
    from fw_context_mcp.config._toml_editor import _atomic_write

    _atomic_write(cfg_path, "\n".join(out).rstrip("\n") + "\n")
    print(f"Removed variant '{name}'.")
    return 0


def cmd_init_variants(args) -> int:
    """Dispatch ``fw-context init-variants`` to list/add/remove."""
    from fw_context_mcp.utils import resolve_project_root

    root = resolve_project_root(getattr(args, "project", None))
    cfg_path = root / ".fw-context" / "config.toml"
    if not cfg_path.exists():
        print(f"error: {cfg_path} not found — run 'fw-context init' first", file=sys.stderr)
        return 1

    cmd = getattr(args, "variants_command", None) or "list"
    if cmd == "list":
        return _list_variants(cfg_path, root)
    if cmd == "add":
        return _add_variant(cfg_path, args)
    if cmd == "remove":
        return _remove_variant(cfg_path, args.name)
    print("error: unknown subcommand", file=sys.stderr)
    return 1
