"""Find the linker scripts of a build.

The script is an INPUT to the linker, not a compilation unit, thus
`compile_commands.json` does not name it and each build system hides it in
a different place.  This module holds the mechanisms that more than one
builder shares.  A builder with a mechanism of its own keeps it in its own
module.

**A path this module returns must come from the build, never from a
pattern that looks right.**  A wrong script would put a wrong memory map
and wrong symbols in front of a user, and the user has no way to tell.
When the build records nothing, the answer is an empty list.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# The linker takes its script with `-T <path>`, and a ninja file records the
# whole link command.  Measured on the Zephyr project, every image:
#
#   LINK_LIBRARIES = … -T  zephyr/linker.cmd  -Wl,-Map,…
#
# TWO spaces after the flag, which is why a pattern with one space finds
# nothing.  `[,\s]*` covers one space, two spaces, no space at all, and the
# `-Wl,-T,<path>` form that a compiler driver uses.
#
# The lookbehind keeps `-Target=x` out: without it the pattern matches that
# flag and captures `arget=x`.  `_looks_like_a_path` is the second gate,
# because a lookbehind alone cannot separate `-T` from a longer flag that
# starts with the same two characters.
_DASH_T = re.compile(r"(?<![\w-])-T[,\s]*([^\s,;'\"]+)")

# A ninja file can be large — v5 writes 1.4 MB — and the flag sits in a
# LINK_LIBRARIES or a command line.  Reading the whole file is still the
# simplest correct thing, and it happens once per index run.
_MAX_NINJA_BYTES = 64 * 1024 * 1024


def _looks_like_a_path(raw: str) -> bool:
    """Say whether a captured token can be a file name.

    A linker script has a suffix, or the token names a directory.  This
    keeps the tail of a longer flag out: `-Target=x` captures `arget=x`,
    which has no suffix and no separator.

    The test is on the SHAPE, not on a list of names.  A script can be
    called anything, thus a suffix allowlist would drop a real one.
    """
    return "/" in raw or Path(raw).suffix != ""


def from_ninja(build_dir: Path) -> list[Path]:
    """Return the `-T` scripts that `build_dir/build.ninja` names.

    This is the authoritative mechanism for a CMake or ninja build, because
    the flag is what the linker receives.  It covers Zephyr, ESP-IDF, and a
    plain CMake project with the ninja generator, with no per-vendor
    knowledge of paths.

    A path in a ninja file is relative to the build directory.  The result
    holds only files that exist, in the order the file names them, with
    duplicates removed.

    ESP-IDF passes about ten scripts and Zephyr passes two, thus the return
    type is a list.  Zephyr's two are the final script and the pre-pass
    script, and measurement on three images found them identical in
    symbols, lines, and regions — so reading both and keeping the first
    name costs nothing.
    """
    ninja = build_dir / "build.ninja"
    try:
        if ninja.stat().st_size > _MAX_NINJA_BYTES:
            log.debug("build.ninja at %s is too large to scan", ninja)
            return []
        text = ninja.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    found: list[Path] = []
    seen: set[Path] = set()
    for match in _DASH_T.finditer(text):
        raw = match.group(1)
        # A ninja variable such as `-T$script` names nothing this side can
        # resolve, so skip it rather than build a path from the literal.
        if "$" in raw or not _looks_like_a_path(raw):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = build_dir / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def output_dirs_from_units(
    project_root: Path, units: list | None, marker: str
) -> list[Path]:
    """Return the output directories that the build compiled into.

    A build system can keep one output tree per configuration.  mbed-tools
    compiles into `BUILD/<target>/<toolchain>-<profile>/`, and
    the second Mbed project holds two such trees, `GCC_ARM-DEBUG` and
    `GCC_ARM-DEVELOP`, each with its own linker script.  Only one belongs
    to the build the index describes, and the build itself says which.

    The source is `raw_entry`, the entry as the build wrote it.  NOT
    `clang_args`: those are normalized for libclang, and the normalization
    removes the output flag — measured on the second Mbed project, 349 normalized tokens
    and no `-o` among them, against 105 raw tokens that hold it.

    Two fields carry the answer, in this order:

    * `output`, which the JSON Compilation Database defines for exactly
      this purpose.
    * The argument of `-o`, for a build that writes no `output` field.

    *marker* is the component that starts the tree, such as `BUILD`.  The
    result keeps *marker* and the two components after it, which is the
    depth mbed-tools uses.

    The order is by how many units name each directory, most first, so one
    stray object file outside the tree of this build cannot win.
    """
    if not units:
        return []
    counts: dict[Path, int] = {}
    for unit in units:
        for target in _output_paths(unit):
            directory = _tree_of(target, unit, project_root, marker)
            if directory is not None:
                counts[directory] = counts.get(directory, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    return [path for path, _ in ordered if path.is_dir()]


def _output_paths(unit: object) -> list[str]:
    """Return what one unit says about where its object file went."""
    raw = getattr(unit, "raw_entry", None)
    if not isinstance(raw, dict):
        return []
    declared = raw.get("output")
    if isinstance(declared, str) and declared:
        return [declared]
    arguments = raw.get("arguments")
    if isinstance(arguments, list):
        tokens = [str(item) for item in arguments]
    else:
        command = raw.get("command")
        tokens = str(command).split() if command else []
    found = []
    for index, token in enumerate(tokens):
        if token == "-o" and index + 1 < len(tokens):
            found.append(tokens[index + 1])
        elif token.startswith("-o") and len(token) > 2:
            found.append(token[2:])
    return found


def _tree_of(
    target: str, unit: object, project_root: Path, marker: str
) -> Path | None:
    """Return the output tree that holds *target*, or None.

    A relative path resolves against the working directory of the
    compilation, which is the directory the compiler itself used.
    """
    path = Path(target)
    if not path.is_absolute():
        base = getattr(unit, "directory", None) or project_root
        path = Path(base) / path
    parts = path.parts
    if marker not in parts:
        return None
    at = parts.index(marker)
    # marker plus the two components after it.  Fewer means the object file
    # is not in a per-configuration tree.
    if at + 2 >= len(parts):
        return None
    return Path(*parts[: at + 3])


def single_script_in(directory: Path, preferred: str = "") -> list[Path]:
    """Return the one linker script in *directory*, or an empty list.

    *preferred* is a file name the build system is known to write.  The
    function takes it when it is there.

    Without it the function accepts a `.ld` file ONLY when the directory
    holds exactly one.  A directory with two candidates offers a choice,
    and a choice made by a pattern is a guess — so the answer is nothing,
    and the log says how many were seen.
    """
    if preferred:
        candidate = directory / preferred
        if candidate.is_file():
            return [candidate.resolve()]
    try:
        scripts = sorted(p for p in directory.glob("*.ld") if p.is_file())
    except OSError:
        return []
    if len(scripts) == 1:
        return [scripts[0].resolve()]
    if scripts:
        log.debug(
            "%s holds %d linker scripts and no preferred name, thus none is "
            "used: %s", directory, len(scripts), ", ".join(p.name for p in scripts),
        )
    return []
