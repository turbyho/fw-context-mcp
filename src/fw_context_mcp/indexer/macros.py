"""Driver: resolve expanded macro values via ``clang -dM -E``.

Uses the compiler's built-in preprocessor to obtain fully expanded macro
values — no need to build a macro dependency graph ourselves.

Usage:
    `clang -dM -E <flags> <file>` dumps all defined macros as:
    ``#define NAME value`` where ``value`` is fully expanded.

The flags are taken from ``compile_commands.json`` (the same flags used
for symbol indexing), so ``#ifdef``-conditional macros resolve correctly
for the indexed build configuration.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def resolve_macros_via_preprocessor(
    flags: list[str],
    file_path: Path,
    clang_binary: str = "clang",
    timeout: float = 30.0,
) -> dict[str, str]:
    """Run ``clang -dM -E`` and return {name: expanded_value}.

    Uses the same compiler flags as the translation unit so that
    ``-D`` defines and ``-I`` include paths are honoured.

    Args:
        flags: Compiler flags from compile_commands.json (e.g. ``["-Iinclude", "-DNDEBUG"]``).
        file_path: Path to the source file (used as input, even if empty).
        clang_binary: Path to the clang binary (default ``"clang"``).
        timeout: Maximum time for the subprocess in seconds.

    Returns:
        Dict mapping macro names to their fully expanded values, or an
        empty dict on failure.
    """
    cmd = [clang_binary, "-dM", "-E", *flags, str(file_path)]

    # Workaround: if the source file doesn't exist (e.g., virtual indexing),
    # create a temporary empty C file so clang doesn't error.
    if not file_path.exists():
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as tmp:
            tmp.write("/* empty */\n")
            tmp_path = tmp.name
        cmd = [clang_binary, "-dM", "-E", *flags, tmp_path]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    else:
        # Strip flags that clang -E doesn't need (or that cause issues with -dM)
        cmd = _sanitize_flags(cmd)
        cwd = file_path.parent
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            )
        except FileNotFoundError:
            log.debug("clang binary not found: %s — macro expansion skipped", clang_binary)
            return {}
        except subprocess.TimeoutExpired:
            log.debug("clang -dM -E timed out after %.1fs for %s", timeout, file_path.name)
            return {}

    if result.returncode != 0:
        log.debug(
            "clang -dM -E failed for %s (exit %d): %s",
            file_path.name, result.returncode, result.stderr[:200],
        )
        return {}

    macros: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("#define "):
            continue
        # Parses "#define NAME value" or "#define NAME value args"
        parts = line.split(" ", 2)
        if len(parts) >= 3:
            name = parts[1]
            value = parts[2]
            # Skip function-like macros — -dM -E reports names as
            # "MACRO(args)" so they won't match macros.name anyway,
            # leaving expanded_value empty as intended.
            if "(" not in name:
                macros[name] = value
    return macros


def _sanitize_flags(cmd: list[str]) -> list[str]:
    """Remove flags that interfere with ``-dM -E`` preprocessor-only mode.

    ``-dM -E`` only needs ``-I``, ``-D``, ``-U``, ``-include``, ``-std=``,
    ``-fmacro-prefix-map=``, and similar preprocessor-level flags.
    Strips object-file flags (``-o``, ``-c``), warning flags (``-W...``),
    and optimization/debug flags that have no effect on preprocessing.
    """
    keep_with_value = (  # flags whose next arg is a value
        "-I", "-D", "-U", "-include", "-isystem", "-imacros",
        "-idirafter", "-iquote",
    )
    keep_no_value = (  # flags that are self-contained (no next-arg)
        "-std=", "-fmacro-prefix-map=",
    )
    drop_with_value = ("-o", "-c", "-MF", "-MT", "-MQ")
    stripped: list[str] = [cmd[0]]  # keep clang binary path
    skip_next = False
    for i in range(1, len(cmd)):
        if skip_next:
            skip_next = False
            continue
        arg = cmd[i]
        if arg in drop_with_value:
            skip_next = True  # next arg is the value
        elif arg.startswith(keep_no_value) or arg.startswith("-std=") or arg.startswith("-fmacro-prefix-map="):
            stripped.append(arg)
        elif arg.startswith(keep_with_value):
            stripped.append(arg)
            # For attached form (-I/path, -DNAME=VAL) no skip_next.
            # For space-separated (-I /path, -D NAME=VAL) — check
            # if the arg has an "=" or "/" right after the prefix.
            prefix_len = next(
                (len(p) for p in keep_with_value if arg.startswith(p)), 0,
            )
            if prefix_len and len(arg) == prefix_len:
                skip_next = True  # bare -I /path (space-separated)
        elif arg.startswith("-W"):
            continue  # warning flags are irrelevant for preprocessing
        elif arg.startswith("-O") or arg.startswith("-g") or arg.startswith("-f"):
            # Optimization / debug flags — keep only macro-related -f flags
            if arg.startswith("-fmacro"):
                stripped.append(arg)
        elif arg.startswith("-m") or arg.startswith("-target"):
            # Target/arch flags — needed for platform-specific predefines
            stripped.append(arg)
        elif arg.startswith("--target=") or arg.startswith("--sysroot="):
            stripped.append(arg)
        elif arg == "--sysroot":
            stripped.append(arg)
            skip_next = True
    return stripped


def resolve_and_update(
    conn,
    config_hash: str,
    flags: list[str],
    file_path: Path,
    clang_binary: str = "clang",
) -> int:
    """Resolve expanded macro values and update the macros table.

    Runs ``clang -dM -E`` with the given *flags*, then updates the
    ``expanded_value`` column of existing macro rows by matching on
    ``(name, config_hash)``.

    Args:
        conn: Open SQLite connection.
        config_hash: Build configuration fingerprint.
        flags: Compiler flags from the compile_commands.json entry.
        file_path: Path to the source file.
        clang_binary: Path to clang (default ``"clang"``).

    Returns:
        Number of macro rows updated.
    """
    expanded = resolve_macros_via_preprocessor(flags, file_path, clang_binary)
    if not expanded:
        return 0

    updated = 0
    for name, value in expanded.items():
        cur = conn.execute(
            "UPDATE macros SET expanded_value = ? WHERE config_hash = ? AND name = ? AND expanded_value = ''",
            (value, config_hash, name),
        )
        updated += cur.rowcount
    if updated:
        conn.commit()
    return updated
