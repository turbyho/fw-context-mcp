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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import _link_command

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkRecord:
    """What the build states about its link.

    A backend gives a record only when it KNOWS the link of the build.  An
    empty *scripts* then means that the link names no script, and the pass
    removes an old memory map.  A backend that does not know the link gives
    no record, and the pass keeps the rows it has, see `indexer._linker_pass`.

    Attributes:
        scripts: The linker scripts, in the order that `ld` reads them.
        defsyms: The `--defsym` definitions, name to expression, in the
            order of the link command.  None is a name that the command
            defines with no known expression, see
            `linker_script.apply_defsyms`.
    """

    scripts: list[Path] = field(default_factory=list)
    defsyms: dict[str, str | None] = field(default_factory=dict)

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
    is in the order the file names them, with duplicates removed.

    A name that looks like a path and is not a file gives an empty list,
    which means "not known".  Measured on an ESP-IDF build: `-T memory.ld`
    is a name that ld finds through `-L`, not in the build directory.  A
    list of the other scripts would be the memory map of another link.  An
    ESP-IDF build reads its link through `ninja_link`, which knows `-L`.
    Measured on the 22 other `build.ninja` files of this machine (Zephyr,
    NCS): each name resolves, thus they keep their scripts.

    Zephyr passes two scripts, thus the return type is a list.  They are the
    final script and the pre-pass script, and measurement on three images
    found them identical in symbols, lines, and regions — so reading both
    and keeping the first name costs nothing.
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
        # A ninja variable such as `-T$script` names a script that this side
        # cannot resolve, thus the link is not known.  The `$` of a ninja
        # escape (`$$`, `$ `, `$:`) is not a variable.
        if _NINJA_VARIABLE.search(raw.replace("$$", "")):
            log.info("linker script: %s names %r, a ninja variable, thus the link is not known", ninja, raw)
            return []
        # A token with no suffix and no separator is the tail of a longer
        # flag, such as `-Target=x`, and no script.
        if not _looks_like_a_path(raw):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = build_dir / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = None
        if resolved is None or not resolved.is_file():
            log.info("linker script: %s names %r, which is not a file, thus the link is not known", ninja, raw)
            return []
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


# ── The link edge of a ninja file ──────────────────────────────────────────

# The variables of a CMake link edge that hold arguments of the compiler
# driver, in the order of the CMake link rule:
#   `$FLAGS $LINK_FLAGS $in -o $TARGET_FILE $LINK_PATH $LINK_LIBRARIES`.
# `$in` holds the object files.  `PRE_LINK` and `POST_BUILD` hold shell
# commands, which are no arguments of the link.
_LINK_VARIABLES = ("FLAGS", "LINK_FLAGS", "LINK_PATH", "LINK_LIBRARIES")

# A reference to a ninja variable, `$name` or `${name}`.  The escapes `$$`,
# `$ `, `$:` and `$` at the end of a line are handled before.
_NINJA_VARIABLE = re.compile(r"\$\{[A-Za-z0-9_.-]+\}|\$[A-Za-z0-9_-]+")

# Marks a reference to a ninja variable in a value, and a literal dollar sign,
# while the value goes through `shlex`.  Neither character is in a command.
_VARIABLE_MARK = "\x00"
_DOLLAR_MARK = "\x01"


@dataclass
class _NinjaEdge:
    """One `build` statement: its outputs, its rule and its variables."""

    outputs: list[str]
    rule: str
    variables: dict[str, str] = field(default_factory=dict)


def ninja_link(build_dir: Path, target: str | None = None) -> LinkRecord | None:
    """Return the link of the executable *target* in `build_dir/build.ninja`, or None.

    CMake writes one `build` statement for each executable, with a rule
    whose name holds `EXECUTABLE_LINKER`, and the arguments of the compiler
    driver in its variables.  The edge of *target* (a path relative to the
    build directory, as the statement names it) is the link.  With no
    *target*, the only executable edge is the link.  ninja runs the command
    in the build directory, thus a relative path is relative to it.

    The arguments go to `_link_command`, which finds a script that `-T`
    names with no directory in the `-L` directories.  Measured on the
    ESP-IDF fixture (v5.2.5): nine `-T` names with no directory in
    `LINK_FLAGS`, and the directories in `LINK_PATH`.

    None ("not known") when the file or the edge is missing, when more than
    one executable edge can be the link, when an argument holds a ninja
    variable and can be a link input, or when `_link_command` cannot read
    the link.
    """
    edges = [edge for edge in _ninja_edges(build_dir) if "EXECUTABLE_LINKER" in edge.rule]
    if target is not None:
        wanted = os.path.normpath(target)
        exact = [edge for edge in edges if wanted in (os.path.normpath(out) for out in edge.outputs)]
        # With CMAKE_RUNTIME_OUTPUT_DIRECTORY the edge names `bin/app.elf`
        # and ESP-IDF names `app.elf`.  The file name decides, when it is
        # the name of only one edge.
        edges = exact or [
            edge for edge in edges
            if os.path.basename(wanted) in (os.path.basename(out) for out in edge.outputs)
        ]
    if len(edges) != 1:
        log.info(
            "linker script: %s has %d executable link(s) for %r, thus the link is not known",
            build_dir / "build.ninja", len(edges), target,
        )
        return None
    edge = edges[0]
    raw_flags: list[str] = []
    linkflags: list[str | None] = []
    for name in _link_variable_order(build_dir, edge.rule):
        for token in _ninja_value_tokens(edge.variables.get(name, "")):
            raw_flags.append(token.replace(_VARIABLE_MARK, "$X"))
            linkflags.append(None if _VARIABLE_MARK in token else token)
    lost = _link_command._lost_link_input(raw_flags, linkflags)
    if lost:
        log.info("linker script: the link of %s cannot be read: %s", edge.outputs[0], lost)
        return None
    # The words come from a shell split already, see `_ninja_value_tokens`.
    scripts = _link_command.resolve_link_scripts(linkflags, [], build_dir, unquote=False)
    if scripts is None:
        return None
    return LinkRecord(scripts=scripts, defsyms=_link_command.defsyms_from_flags(linkflags))


def _ninja_edges(build_dir: Path) -> list[_NinjaEdge]:
    """Return the `build` statements of `build_dir/build.ninja`, or [] when it cannot be read."""
    lines = _ninja_lines(build_dir / "build.ninja")
    edges: list[_NinjaEdge] = []
    current: _NinjaEdge | None = None
    for line in lines:
        if line.startswith((" ", "\t")):
            if current is not None:
                name, equals, value = line.strip().partition("=")
                if equals:
                    current.variables[name.strip()] = value.strip()
            continue
        current = None
        if line.startswith("build "):
            outputs, rule = _ninja_build_header(line[len("build "):])
            if rule:
                current = _NinjaEdge(outputs=outputs, rule=rule)
                edges.append(current)
    return edges


def _ninja_lines(path: Path) -> list[str]:
    """Return the lines of the ninja file *path*, with each continuation joined.

    A line that ends with an odd number of `$` continues on the next line,
    and ninja removes the leading white space of that line.
    """
    try:
        if path.stat().st_size > _MAX_NINJA_BYTES:
            log.debug("%s is too large to scan", path)
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[str] = []
    pending = ""
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if pending:
            line = line.lstrip(" \t")
        dollars = len(line) - len(line.rstrip("$"))
        if dollars % 2:
            pending += line[:-1]
            continue
        lines.append(pending + line)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _ninja_build_header(text: str) -> tuple[list[str], str]:
    """Return the outputs and the rule of the header of one `build` statement.

    *text* is `out1 out2: rule in1 in2 | deps`.  `$ ` is a space in a path,
    `$:` a colon, and `$$` a dollar sign.  An unescaped `:` ends the outputs.
    """
    outputs: list[str] = []
    word = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "$" and index + 1 < len(text) and text[index + 1] in " :$":
            word += text[index + 1]
            index += 2
            continue
        if char == ":":
            if word:
                outputs.append(word)
            rest = text[index + 1:].split()
            return outputs, rest[0] if rest else ""
        if char == " ":
            if word:
                outputs.append(word)
            word = ""
        else:
            word += char
        index += 1
    return outputs, ""


def _link_variable_order(build_dir: Path, rule: str) -> list[str]:
    """Return the link variables in the order that the command of *rule* names them.

    The rule is in `build.ninja` or in a file that it includes
    (`CMakeFiles/rules.ninja`).  A rule that is not found gives the order of
    the CMake link rule, `_LINK_VARIABLES`.

    A rule with a response file names some variables in `rspfile_content`
    and the file as `@$RSP_FILE` in its command.  CMake does that when the
    command line is too long, which is the normal case for ESP-IDF on
    Windows.  The content takes the place of `$RSP_FILE`, thus those
    variables keep their position in the link.
    """
    files = [build_dir / "build.ninja"]
    for line in _ninja_lines(files[0]):
        if line.startswith("include "):
            files.append(build_dir / line[len("include "):].strip())
    for path in files:
        found: dict[str, str] | None = None
        for line in _ninja_lines(path):
            if not line.startswith((" ", "\t")):
                if found is not None:
                    break
                if line.strip() == f"rule {rule}":
                    found = {}
                continue
            name, equals, value = line.strip().partition("=")
            if found is not None and equals:
                found[name.strip()] = value.strip()
        if found is not None and "command" in found:
            # str.replace, not re.sub: the content can hold a backslash.
            command = found["command"]
            for reference in ("${RSP_FILE}", "$RSP_FILE"):
                command = command.replace(reference, found.get("rspfile_content", ""))
            named = re.findall(r"\$\{?([A-Za-z0-9_]+)\}?", command)
            order = [name for name in dict.fromkeys(named) if name in _LINK_VARIABLES]
            return order or list(_LINK_VARIABLES)
    return list(_LINK_VARIABLES)


def _ninja_value_tokens(value: str) -> list[str]:
    """Return the shell words of the value of one ninja variable.

    The escapes go first: `$$` is a dollar sign, `$ ` a space and `$:` a
    colon.  A reference to a ninja variable stays as `_VARIABLE_MARK` in its
    word, so that the caller can tell a word that did not expand.  A value
    that the shell cannot split gives one word with the mark.  The split
    follows the rules of the host, see `_link_command.shell_words`.
    """
    text = value.replace("$$", _DOLLAR_MARK).replace("$ ", " ").replace("$:", ":")
    text = _NINJA_VARIABLE.sub(_VARIABLE_MARK, text)
    words = _link_command.shell_words(text)
    if words is None:
        return [_VARIABLE_MARK]
    return [word.replace(_DOLLAR_MARK, "$") for word in words]


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
