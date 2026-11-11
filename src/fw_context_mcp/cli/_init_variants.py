"""CLI for managing ``[[build.variants]]`` in ``.fw-context/config.toml``.

``fw-context init-variants`` is the follow-up to ``fw-context init`` for an
ALREADY initialized project: it adds/removes/lists build variants without
re-running agent/deps/LLM provisioning.

``list`` reads ``[[build.variants]]`` from ``config.toml`` (what the config
declares) — NOT ``get_project_status`` (MCP), which lists ``build_configs`` (what
is actually indexed).  After a change the new variants are unindexed until
``fw-context index --build`` runs.

``add`` and ``remove`` change the file through ``tomlkit``.  WHY not text
lines: a value with a backslash (a Windows path) or a quote must be an
escape in TOML, and a text match of ``name = "..."`` did not find a name
with an escape, a literal string, or a comment after it.  ``tomlkit`` keeps
the comments and the order of the file, as ``housekeeping.py`` does.  One
limit: ``tomlkit`` holds the comment lines after the last key of a table in
that table, thus ``remove`` removes them with it (the text code did the
same).

WHY a check of the result before the write (as ``config._model_list``
does): ``tomlkit`` writes some shapes wrong, for example a table into an
inline array (``variants = [{...}]``), and the result is not valid TOML.
The shapes that a person writes by hand are many.  Thus the command reads
the result again with ``tomllib`` and compares it with the data that it
must give.  On a difference it writes nothing and tells the shape to use.
"""

from __future__ import annotations

import argparse
import copy
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
import tomlkit.exceptions

from fw_context_mcp.config._toml_editor import _atomic_write

# The shape that the check of the result asks for.
_SHAPE = "write each variant as a [[build.variants]] table"


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


def _load(cfg_path: Path) -> tuple[dict[str, Any], tomlkit.TOMLDocument] | None:
    """Read *cfg_path* as data and as a ``tomlkit`` document, or print why not and return None."""
    try:
        text = cfg_path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        document = tomlkit.parse(text)
    except (OSError, ValueError, tomlkit.exceptions.ParseError) as e:
        print(f"error: cannot parse {cfg_path}: {e}", file=sys.stderr)
        return None
    return data, document


def _variants_of(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return ``build.variants`` of *data*, or None when it is not a list of tables."""
    build = data.get("build", {})
    if not isinstance(build, dict):
        return None
    variants = build.get("variants", [])
    if not isinstance(variants, list) or not all(isinstance(v, dict) for v in variants):
        return None
    return variants


def _without_empty_build(data: dict[str, Any]) -> dict[str, Any]:
    """Return *data* without a ``build`` table that is empty.

    WHY: a file with only ``[[build.variants]]`` has no ``[build]`` header,
    thus the removal of its last variant also removes ``build``.  A file
    with a ``[build]`` header keeps an empty table.  The two results are
    the same config.
    """
    if data.get("build") == {}:
        data = {key: value for key, value in data.items() if key != "build"}
    return data


def _write_checked(cfg_path: Path, document: tomlkit.TOMLDocument, expected: dict[str, Any]) -> bool:
    """Write *document* when ``tomllib`` reads *expected* from it; else print why not and return False."""
    result = tomlkit.dumps(document)
    problem: str | None
    try:
        after = tomllib.loads(result)
    except tomllib.TOMLDecodeError as e:
        problem = f"the result is not TOML ({e})"
    else:
        same = _without_empty_build(after) == _without_empty_build(expected)
        problem = None if same else "the result holds values that this command did not write"
    if problem is not None:
        print(f"error: {cfg_path} is not changed: {problem}. {_SHAPE}.", file=sys.stderr)
        return False
    _atomic_write(cfg_path, result)
    return True


def _append_table(document: tomlkit.TOMLDocument, variant: dict[str, Any]) -> None:
    """Append *variant* to ``build.variants`` of *document* as a ``[[build.variants]]`` table."""
    build = document.get("build")
    if build is None:
        build = tomlkit.table()
        document["build"] = build
    variants = build.get("variants")
    if variants is None:
        variants = tomlkit.aot()
        build["variants"] = variants
    table = tomlkit.table()
    for key in ("name", "description", "board"):
        if variant.get(key):
            table[key] = variant[key]
    if variant.get("env"):
        env_table = tomlkit.inline_table()
        env_table.update(variant["env"])
        table["env"] = env_table
    # A blank line before the header, and one after the table when a
    # different table follows it in the file.
    table.trivia.indent = "\n"
    if list(document.keys())[-1] != "build":
        table.add(tomlkit.nl())
    variants.append(table)


def _add_variant(cfg_path: Path, args: argparse.Namespace) -> int:
    name = args.name
    if not name:
        print("error: --name is required", file=sys.stderr)
        return 1
    loaded = _load(cfg_path)
    if loaded is None:
        return 1
    data, document = loaded
    current = _variants_of(data)
    if current is None:
        print(f"error: build.variants of {cfg_path} is not a list of tables. {_SHAPE}.", file=sys.stderr)
        return 1
    if any(v.get("name") == name for v in current):
        print(f"error: variant '{name}' already exists", file=sys.stderr)
        return 1

    variant: dict[str, Any] = {"name": name}
    if getattr(args, "board", None):
        variant["board"] = args.board
    if getattr(args, "description", None):
        variant["description"] = args.description
    env: dict[str, str] = {}
    for kv in getattr(args, "env", None) or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k.strip()] = v.strip()
    if env:
        variant["env"] = env

    expected = copy.deepcopy(data)
    expected.setdefault("build", {})["variants"] = [*current, variant]
    try:
        _append_table(document, variant)
    except (ValueError, TypeError, IndexError, KeyError) as e:
        # tomlkit cannot change some shapes, for example a key out of a dotted table.
        print(f"error: {cfg_path} is not changed: tomlkit cannot change this shape ({e}). {_SHAPE}.",
              file=sys.stderr)
        return 1
    if not _write_checked(cfg_path, document, expected):
        return 1
    print(f"Added variant '{name}'." + ("  board=" + args.board if getattr(args, "board", None) else ""))
    print("Index it with: fw-context index --build --variant " + name)
    return 0


def _remove_variant(cfg_path: Path, name: str) -> int:
    loaded = _load(cfg_path)
    if loaded is None:
        return 1
    data, document = loaded
    current = _variants_of(data) or []
    index = next((i for i, variant in enumerate(current) if variant.get("name") == name), None)
    if index is None:
        print(f"error: variant '{name}' not found", file=sys.stderr)
        return 1
    expected = copy.deepcopy(data)
    remaining = current[:index] + current[index + 1:]
    if remaining:
        expected["build"]["variants"] = remaining
    else:
        del expected["build"]["variants"]
    try:
        variants = document["build"]["variants"]
        del variants[index]
        if len(variants) == 0:
            del document["build"]["variants"]
    except (ValueError, TypeError, IndexError, KeyError) as e:
        print(f"error: {cfg_path} is not changed: tomlkit cannot change this shape ({e}). {_SHAPE}.",
              file=sys.stderr)
        return 1
    if not _write_checked(cfg_path, document, expected):
        return 1
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
