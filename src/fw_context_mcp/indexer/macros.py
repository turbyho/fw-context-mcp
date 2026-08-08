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
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _find_clang_binary() -> str:
    """Find the clang CLI binary.

    Tries PATH first, then common Windows LLVM install locations.
    Returns ``"clang"`` as fallback (will fail gracefully via
    ``FileNotFoundError`` in the caller).
    """
    # 1. Check PATH
    found = shutil.which("clang")
    if found:
        return found

    # 2. Check common Windows LLVM install locations
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\LLVM\bin\clang.exe",
            r"C:\Program Files (x86)\LLVM\bin\clang.exe",
        ):
            if Path(candidate).exists():
                return candidate

    # 3. Fallback — caller handles FileNotFoundError
    return "clang"


def resolve_macros_via_preprocessor(
    flags: list[str],
    file_path: Path,
    clang_binary: str = "clang",
    timeout: float = 30.0,
    cwd: str | Path | None = None,
) -> dict[str, str]:
    """Run ``clang -dM -E`` and return {name: expanded_value}.

    WHY ``clang -dM -E`` instead of parsing macros from the AST: libclang's
    AST only exposes macro names and raw values from ``#define`` directives.
    Fully expanded values require preprocessing — ``clang -dM -E`` uses the
    compiler's built-in preprocessor, correctly expanding chains like
    ``#define A B`` + ``#define B 42`` to ``A=42``.  No need to build and
    maintain a macro dependency graph ourselves.

    Uses the same compiler flags as the translation unit so that
    ``-D`` defines and ``-I`` include paths are honoured.

    Args:
        flags: Compiler flags from compile_commands.json (e.g. ``["-Iinclude", "-DNDEBUG"]``).
        file_path: Path to the source file (used as input, even if empty).
        clang_binary: Path to the clang binary (default ``"clang"``).
        timeout: Maximum time for the subprocess in seconds.
        cwd: Working directory for clang — should match the ``directory``
            field in compile_commands.json so relative ``-I`` paths resolve
            correctly.  Defaults to ``None`` (process CWD).

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
            cmd = _sanitize_flags(cmd)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                cwd=file_path.parent if file_path.parent.exists() else Path.cwd(),
            )
        except FileNotFoundError:
            log.debug("clang binary not found: %s — macro expansion skipped", clang_binary)
            return {}
        except subprocess.TimeoutExpired:
            log.debug("clang -dM -E timed out after %.1fs for %s", timeout, file_path.name)
            return {}
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    else:
        # Strip flags that clang -E doesn't need (or that cause issues with -dM)
        cmd = _sanitize_flags(cmd)
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

    WHY sanitize: compile_commands.json contains the full compiler command
    line including object-file flags (``-o``, ``-c``), warning flags (``-W...``),
    optimization (``-O2``), and debug (``-g``).  These are irrelevant for
    preprocessing and some (like ``-c``) conflict with ``-E``.  Keeping them
    would cause clang errors or unnecessary output.

    Also strips dangerous flags that should never reach the compiler
    from compile_commands.json (supply-chain defense-in-depth):
    ``-Xclang``, ``-plugin``, ``-cc1``, ``-emit-llvm``, ``-emit-ast``.

    ``-dM -E`` only needs ``-I``, ``-D``, ``-U``, ``-include``, ``-std=``,
    ``-fmacro-prefix-map=``, and similar preprocessor-level flags.
    """
    _DANGEROUS = frozenset({
        "-Xclang", "-plugin", "-cc1", "-emit-llvm", "-emit-ast",
        "-fplugin=", "-fplugin-arg-",
    })
    keep_with_value = (  # flags whose next arg is a value
        "-I", "-D", "-U", "-include", "-isystem", "-imacros",
        "-idirafter", "-iquote",
    )
    keep_no_value = (  # flags that are self-contained (no next-arg)
        "-std=", "-fmacro-prefix-map=",
    )
    drop_with_value = ("-o", "-MF", "-MT", "-MQ")
    drop_no_value = ("-c",)  # flags that take no argument — just drop
    stripped: list[str] = [cmd[0]]  # keep clang binary path
    skip_next = False
    for i in range(1, len(cmd)):
        if skip_next:
            skip_next = False
            continue
        arg = cmd[i]
        if arg in drop_with_value:
            skip_next = True  # next arg is the value
        elif arg in drop_no_value:
            continue  # flag takes no argument — just drop it
        elif arg in _DANGEROUS or any(arg.startswith(d) for d in _DANGEROUS if d.endswith("=")):
            skip_next = arg in ("-Xclang",)  # -Xclang consumes next arg as sub-flag
        elif arg.startswith(keep_no_value) or arg.startswith("-std=") or arg.startswith("-fmacro-prefix-map="):
            stripped.append(arg)
        elif arg.startswith(keep_with_value):
            stripped.append(arg)
            # For attached form (-I/path, -DNAME=VAL) no skip_next needed.
            # For space-separated form (-I /path, -isystem /path) the next
            # token is the value — keep it too (do NOT skip).
            # Previous code incorrectly set skip_next=True here, which
            # dropped the path entirely and caused "no input files" errors
            # when -isystem was space-separated.
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
        elif not arg.startswith("-"):
            # Non-flag argument — likely the source file path
            stripped.append(arg)
        elif arg in ("-dM", "-E", "-dD") or arg.startswith("-x"):
            # Clang preprocessor mode flags and language spec
            stripped.append(arg)
    return stripped


def resolve_and_update(
    conn,
    config_hash: str,
    flags: list[str],
    file_path: Path,
    clang_binary: str = "clang",
    cwd: str | Path | None = None,
) -> int:
    """Resolve expanded macro values and update the macros table.

    Runs ``clang -dM -E`` with the given *flags*, then updates the
    ``expanded_value`` column of existing macro rows by matching on
    ``(name, config_hash)``.

    ``clang -dM -E`` outputs only the **final** preprocessed value of each
    macro.  When a macro is redefined via ``#undef`` + ``#define``, multiple
    rows exist in the ``macros`` table (one per source line), but they all
    share the same ``name``.  We update **all** rows with that name so every
    row shows the final preprocessed value.  The per-row ``value`` column
    still preserves the original definition text from each source line.

    Args:
        conn: Open SQLite connection.
        config_hash: Build configuration fingerprint.
        flags: Compiler flags from the compile_commands.json entry.
        file_path: Path to the source file.
        clang_binary: Path to clang (default ``"clang"``).
        cwd: Working directory for clang — should match the ``directory``
            field in compile_commands.json so relative ``-I`` paths resolve.

    Returns:
        Number of macro rows updated.
    """
    expanded = resolve_macros_via_preprocessor(
        flags, file_path,
        clang_binary if clang_binary != "clang" else _find_clang_binary(),
        cwd=cwd,
    )
    if not expanded:
        return 0

    # Update ALL rows with this name (not just the first empty one).
    # clang -dM -E gives the final preprocessed value — every row for
    # this macro name should display that final value.  Previously the
    # ``AND expanded_value = ''`` filter caused the final value to be
    # bound to the first empty row (often an old #undef'd definition),
    # leaving the actual final-definition row empty.
    # executemany: single batch UPDATE instead of N individual queries
    rows = [(value, config_hash, name) for name, value in expanded.items()]
    before = conn.total_changes
    conn.executemany(
        "UPDATE macros SET expanded_value = ? WHERE config_hash = ? AND name = ?",
        rows,
    )
    updated = conn.total_changes - before
    if updated:
        conn.commit()  # defensive: _run_postprocess doesn't commit after this point
    return updated
