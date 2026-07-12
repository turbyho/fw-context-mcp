"""``.d`` dependency file resolution for compile_commands.json entries.

Shared between the indexer (``runner.py``) and build-system validators
(``builders/platformio.py``) so that dep-file path logic stays in one place.
"""

from __future__ import annotations

from pathlib import Path


def try_dep_from_object(object_path: str, directory: Path) -> Path | None:
    """Derive a ``.d`` file path from an object file path and return it if it exists.

    Tries two naming conventions:
    1. Replace ``.o`` with ``.d`` (PlatformIO/GCC default): ``Esp.cpp.o`` → ``Esp.cpp.d``
    2. Append ``.d`` after ``.o`` (CMake convention): ``main.c.o`` → ``main.c.o.d``

    Args:
        object_path: Path to the object file (may be relative to *directory*).
        directory: Base directory for resolving relative paths.

    Returns:
        Absolute ``Path`` to the ``.d`` file if found, or ``None``.
    """
    o = Path(object_path)
    if not o.is_absolute():
        o = directory / o
    o = o.resolve()
    # PlatformIO/GCC convention: replace .o suffix with .d
    dep_replaced = o.with_suffix(".d")
    if dep_replaced.exists():
        return dep_replaced
    # CMake convention: append .d after .o
    dep_appended = o.parent / f"{o.name}.d"
    if dep_appended.exists():
        return dep_appended
    return None


def resolve_dep_path_for_entry(entry: dict, directory: Path) -> Path | None:
    """Locate the ``.d`` file from a raw compile_commands.json entry.

    Tries three strategies:

    1. ``-MF <path>`` flag in the compile command (raw args, then
       response-file-expanded).  Returns the resolved path if the file
       exists.
    2. ``output`` field — derive ``.d`` from the object file path.
       Supports both PlatformIO/GCC convention (``.o`` → ``.d``) and
       CMake convention (``.o`` → ``.o.d``).
    3. ``-o <path>`` in arguments — Arduino CLI puts the object path
       in ``-o`` but omits the ``output`` field.

    Does NOT try the source-sidecar strategy (``<source>.d`` next to
    the source file) — that requires the CompilationUnit ``file``
    attribute and is handled by ``_find_dep_path`` in ``runner.py``.

    Args:
        entry: A raw dict from compile_commands.json with optional
               ``arguments``, ``command``, and ``output`` keys.
        directory: The ``directory`` field from the entry — used to
                   resolve relative paths.

    Returns:
        Absolute ``Path`` to the ``.d`` file if found, or ``None``.
    """
    # ── Strategy 1: -MF flag ──
    raw_args: list[str] = entry.get("arguments") or []
    if not raw_args and "command" in entry:
        raw_args = entry["command"].split()

    for i, token in enumerate(raw_args):
        if token == "-MF" and i + 1 < len(raw_args):
            d = Path(raw_args[i + 1])
            if not d.is_absolute():
                d = directory / d
            d = d.resolve()
            if d.exists():
                return d

    # Also try with expanded response files via normalize_entry
    try:
        from .config_hash import normalize_entry

        norm = normalize_entry(entry)
        for i, arg in enumerate(norm["args"]):
            if arg == "-MF" and i + 1 < len(norm["args"]):
                d = Path(norm["args"][i + 1])
                if not d.is_absolute():
                    d = directory / d
                d = d.resolve()
                if d.exists():
                    return d
    except Exception:
        pass  # normalize_entry may fail on malformed entries

    # ── Strategy 2: output field ──
    output = entry.get("output", "")
    if output:
        dep = try_dep_from_object(output, directory)
        if dep is not None:
            return dep

    # ── Strategy 3: -o <path> in arguments ──
    # Arduino CLI puts the object path in -o but omits the ``output`` field.
    for i, token in enumerate(raw_args):
        if token == "-o" and i + 1 < len(raw_args):
            dep = try_dep_from_object(raw_args[i + 1], directory)
            if dep is not None:
                return dep

    return None
