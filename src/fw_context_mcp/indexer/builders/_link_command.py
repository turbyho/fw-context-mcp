"""Read the link inputs of a compiler driver command line.

A build system that records the link command gives its arguments here: the
PlatformIO backend from `pio run -t envdump`, and the ninja backends from
the link edge of `build.ninja`.  The module answers which linker scripts
`ld` reads, in which directories it finds them, and which `--defsym`
values the command gives.  Nothing here knows a build system.

**An answer must come from the command, never from a pattern that looks
right**, see `_linker`.  A link input that this module does not read makes
the answer None ("not known"), and the index then keeps what it has.  A
wrong script would put a wrong memory map in front of a user.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

from ..linker_script import strip_comments

log = logging.getLogger(__name__)

# The ld options that name a linker script.  `-T` and `--script` name a
# script that replaces the default one, or that adds to it with INSERT.
# `-dT` and `--default-script` name the default script itself.  The STM32
# Arduino core passes its memory map through `--default-script`.  ld reads
# its options with getopt_long_only, thus a long option also has a form
# with one dash, such as `-script` (tested with GNU ld).
_SCRIPT_OPTIONS = {
    "-T": "T", "--script": "T", "-script": "T", "-dT": "dT", "--default-script": "dT", "-default-script": "dT",
}
_SCRIPT_PREFIXES = {"--script=": "T", "-script=": "T", "--default-script=": "dT", "-default-script=": "dT"}

# ld options that start with `-T` and name a section address, not a script:
# `-Ttext=0x8000`, `-Tbss 0x1`, `-Ttext-segment=0x400000`.
_SECTION_ADDRESS = re.compile(r"^-T(?:text|data|bss|text-segment|rodata-segment|ldata-segment)(?:=|$)")

# The same section options, for the text before a variable that did not
# expand.  Only the form with `=` is safe there: in `-Ttext=$ADDR` the
# variable is the address, and `-Ttext$X` can expand to `-Ttext.ld`.
_SECTION_ADDRESS_HEAD = re.compile(r"^-T(?:text|data|bss|text-segment|rodata-segment|ldata-segment)=")

# The options that can change the answer of this module: a linker script, a
# directory that ld searches for one, a `--defsym` value, an option that
# passes the next argument to ld, and a response file (`@file`), which can
# hold any of them.  The first tuple is for a compiler driver argument, the
# second one for a part of a `-Wl,` argument.
#
# A specs file can add a script (`_specs_problem`), and `-l:<file>` can find
# an implicit script (`implicit_scripts_from_flags`), thus both count.
#
# `--for-linker` is an alias of `-Xlinker` and `--library-directory` of `-L`
# for the driver, and the ld options have a one-dash form, see above.
_LINK_INPUT_DRIVER = (
    "-T", "-L", "-l", "-Wl,", "-Xlinker", "--for-linker", "--library-directory", "-specs=", "--specs=", "@",
)
_LINK_INPUT_LD = (
    "-T", "-dT", "-L", "-l", "--script", "-script", "--default-script", "-default-script",
    "--library-path", "-library-path", "--library", "-library", "--defsym", "-defsym", "@",
)

# The options of the tuples above that take the NEXT argument as their
# value.  A value that did not expand is a lost script, directory or value.
_TAKES_VALUE_DRIVER = frozenset({"-T", "-L", "-l", "-Xlinker", "--for-linker", "--library-directory"})
_TAKES_VALUE_LD = frozenset({
    "-T", "-dT", "-L", "-l", "--script", "-script", "--default-script", "-default-script",
    "--library-path", "-library-path", "--library", "-library", "--defsym", "-defsym",
})

# The ld manual: "INSERT [ AFTER | BEFORE ] output_section".  A script with
# this command adds to the default script, and without it replaces it.
_INSERT = re.compile(r"\bINSERT\s+(?:AFTER|BEFORE)\b")

# The specs files that add no link input.  Measured: the 56 copies of these
# two files in the PlatformIO toolchains name only libraries, and the STM32
# projects pass both.  Other specs files of the same toolchains add a
# script: `pid.specs` and `redboot.specs` add `-T redboot.ld`.  gcc finds a
# specs file with no directory in its own directories, which the dump does
# not state, thus a name of this set is the only one that is known.
_HARMLESS_SPECS = frozenset({"nano.specs", "nosys.specs"})

# A link input in the text of a specs file.  The module does not evaluate
# the specs language, thus each such option makes the link unknown.
_SPECS_LINK_INPUT = re.compile(r"(?<![\w-])(?:-T|-dT|--script|--default-script|--defsym|-L|--library-path|-l:|--library=:)")

# The first bytes of an object file or an archive.  ld reads a file that
# `-l:name` finds and that has none of them as an implicit linker script.
_BINARY_MAGIC = (b"\x7fELF", b"!<arch>\n", b"!<thin>\n")



def _lost_link_input(raw_flags: list[str], linkflags: list[str | None]) -> str:
    """Return why a token that did not expand makes the link unknown, or "".

    A token that did not expand is None in *linkflags*, and its text is
    lost.  The text in *raw_flags* tells if it can name a link input.  A
    token is harmless only when it cannot: an option that is not a link
    input and that holds the variable in its value, such as
    `-Wl,-Map=${PROGNAME}.map`.  Everything else makes the link unknown:

    * The value of an option that takes the next token, such as `-T $X`.
    * A token that is not an option.  It is an input file, and ld reads an
      input file that is not an object as a linker script.  A token that is
      only a variable can also expand to more than one option.
    * An option whose text before the variable can be a link input, such
      as `-T$X`, `-L$X`, `-$X` or `-Wl,$X`.
    * A `-Wl,` token with a link input in any of its parts, also in a part
      with no variable, because the whole token is lost.

    WHY not "any token that did not expand": the STM32 Arduino core writes
    `-Wl,-Map=${PROGNAME}.map`, and a dump with no `PROGNAME` then gives no
    map for every STM32 project.
    """
    for index, (raw, expanded) in enumerate(zip(raw_flags, linkflags, strict=True)):
        if expanded is not None:
            continue
        previous = raw_flags[index - 1] if index else ""
        if previous in _TAKES_VALUE_DRIVER or (
            previous.startswith("-Wl,") and previous[4:].split(",")[-1] in _TAKES_VALUE_LD
        ):
            return f"the value of {previous!r} did not expand: {raw!r}"
        if not raw.startswith("-"):
            return f"an argument that is not an option did not expand: {raw!r}"
        if raw.startswith("-Wl,"):
            if any(_may_be_link_input(part, _LINK_INPUT_LD) for part in raw[4:].split(",")):
                return f"a linker option did not expand: {raw!r}"
        elif _may_be_link_input(raw, _LINK_INPUT_DRIVER):
            return f"a link option did not expand: {raw!r}"
    return ""


def _may_be_link_input(text: str, options: tuple[str, ...]) -> bool:
    """Say if the argument *text* can be one of *options*.

    A text with no variable is complete, and it is a link input when it
    starts with one of *options*.  In a text with a variable, only the part
    before the `$` is known, and the argument can be any option that starts
    with that part.  An empty part, as in `$X`, can be all of them.
    """
    head, dollar, _ = text.partition("$")
    if head.startswith("-T"):
        section = _SECTION_ADDRESS_HEAD if dollar else _SECTION_ADDRESS
        if section.match(head) or head.startswith("-Target"):
            return False
    if dollar:
        return any(option.startswith(head) or head.startswith(option) for option in options)
    return any(head.startswith(option) for option in options)



# ── Link options ───────────────────────────────────────────────────────────


def _is_script_prefix(token: str) -> bool:
    """Say if *token* is `-T<file>` and not a longer option that starts with `-T`."""
    return (
        token.startswith("-T")
        and len(token) > 2
        and not token.startswith("-Target")
        and not _SECTION_ADDRESS.match(token)
    )


def script_options(tokens: list[str | None]) -> list[tuple[str, str]]:
    """Return each script option in *tokens* as `(kind, value)`, in order.

    *kind* is `"T"` for `-T` and `--script`, and `"dT"` for `-dT` and
    `--default-script`.  The forms are `-T x`, `-Tx`, and the `-Wl,` forms
    with the value after a comma or an `=`.  The STM32 Arduino core writes
    `-Wl,--default-script` and puts the path in the NEXT token.  The
    compiler driver gives the two tokens to `ld` in that order, thus `ld`
    reads them as one option.
    """
    found: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        index += 1
        if token is None:
            continue
        if token == "-T":
            if following is not None:
                found.append(("T", following))
            index += 1
        elif _is_script_prefix(token):
            found.append(("T", token[2:]))
        elif token.startswith("-Wl,"):
            values, takes_next = _scripts_in_wl(token[4:].split(","))
            found.extend(values)
            if takes_next is not None:
                if following is not None:
                    found.append((takes_next, following))
                index += 1
    return found


def scripts_from_flags(tokens: list[str | None]) -> list[str]:
    """Return the values of every script option in *tokens*, in order."""
    return [value for _, value in script_options(tokens)]


def _scripts_in_wl(parts: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    """Return the scripts of one `-Wl,` token, and the kind of the next token."""
    found: list[tuple[str, str]] = []
    position = 0
    while position < len(parts):
        part = parts[position]
        if part in _SCRIPT_OPTIONS:
            kind = _SCRIPT_OPTIONS[part]
            if position + 1 == len(parts):
                return found, kind
            found.append((kind, parts[position + 1]))
            position += 2
            continue
        for prefix, kind in _SCRIPT_PREFIXES.items():
            if part.startswith(prefix):
                found.append((kind, part[len(prefix):]))
                break
        else:
            if _is_script_prefix(part):
                found.append(("T", part[2:]))
        position += 1
    return found, None


def library_dirs_from_flags(tokens: list[str | None]) -> tuple[list[str | None], list[str | None]]:
    """Return the `-L` directories of *tokens*: the driver ones and the `-Wl,` ones.

    The compiler driver gives `ld` its own `-L` options first, in the order
    of the command line, and the `-Wl,` options later.  `LINKCOM` puts
    `$LINKFLAGS` before `$_LIBDIRFLAGS`, thus a driver `-L` in `LINKFLAGS`
    comes before `LIBPATH`, and a `-Wl,-L` comes after it.  A value that
    did not expand is None.

    The `-Wl,` forms are `-L<dir>`, `--library-path=<dir>`, and `-L` or
    `--library-path` with the directory in the next part or, at the end of
    the token, in the next token, as `-Wl,--default-script` does.
    """
    driver: list[str | None] = []
    passed: list[str | None] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token is None:
            continue
        if token in ("-L", "--library-directory"):
            # `--library-directory` is an alias of `-L`, measured with `gcc -###`.
            driver.append(_search_dir(tokens[index] if index < len(tokens) else None))
            index += 1
        elif token.startswith("--library-directory="):
            driver.append(_search_dir(token.split("=", 1)[1]))
        elif token.startswith("-L"):
            driver.append(_search_dir(token[2:]))
        elif token.startswith("-Wl,"):
            parts = token[4:].split(",")
            position = 0
            while position < len(parts):
                part = parts[position]
                if part in ("-L", "--library-path", "-library-path"):
                    if position + 1 < len(parts):
                        passed.append(_search_dir(parts[position + 1]))
                        position += 1
                    else:
                        passed.append(_search_dir(tokens[index] if index < len(tokens) else None))
                        index += 1
                elif part.startswith(("--library-path=", "-library-path=")):
                    passed.append(_search_dir(part.split("=", 1)[1]))
                elif part.startswith("-L") and len(part) > 2:
                    passed.append(_search_dir(part[2:]))
                position += 1
    return driver, passed


def _search_dir(value: str | None) -> str | None:
    """Return the directory of one `-L` value, or None when it is not known.

    The ld manual: a directory that starts with `=` or `$SYSROOT` is in the
    sysroot, and ld replaces the prefix with the sysroot.  The dump does not
    state the sysroot, thus such a directory is not known, and the search
    stops there.
    """
    if value is None or value.startswith(("=", "$SYSROOT")):
        return None
    return value


def defsyms_from_flags(tokens: list[str | None]) -> dict[str, str | None]:
    """Return the `--defsym` definitions in *tokens*, name to expression.

    The forms are `-Wl,--defsym=NAME=EXPR`, `-Wl,--defsym,NAME=EXPR`, and
    `-Wl,--defsym` with `NAME=EXPR` in the next token.

    A name that two definitions give with DIFFERENT expressions gets None.
    `ld` evaluates the definitions in their order, thus a definition between
    the two sees the first value.  A dict keeps one position per name and
    would give it the last value, which is a wrong number.  The name stays
    in the result, because `ld` defines it all the same, and a `PROVIDE` of
    that name in a script then defines nothing.  The same expression two
    times is one value: `env.Append` does not remove a duplicate that the
    core and `build_flags` both add.
    """
    definitions: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token is None or not token.startswith("-Wl,"):
            continue
        parts = token[4:].split(",")
        for position, part in enumerate(parts):
            if part.startswith(("--defsym=", "-defsym=")):
                definitions.append(part.split("=", 1)[1])
            elif part in ("--defsym", "-defsym"):
                if position + 1 < len(parts):
                    definitions.append(parts[position + 1])
                elif index < len(tokens) and tokens[index] is not None:
                    definitions.append(str(tokens[index]))
                    index += 1

    found: dict[str, str | None] = {}
    conflicting: set[str] = set()
    for definition in definitions:
        name, equals, expression = definition.partition("=")
        name, expression = name.strip(), expression.strip()
        if not (equals and name and expression):
            continue
        if name in found and found[name] != expression:
            conflicting.add(name)
        found.setdefault(name, expression)
    for name in conflicting:
        log.debug("--defsym %s has two expressions, thus it has no value", name)
        found[name] = None
    return found


def ld_path(token: str, posix: bool | None = None) -> str | None:
    """Return the path that `ld` gets for the argument *token*, or None.

    SCons writes each argument to a shell.  It puts double quotes around an
    argument that holds a space (`quote_spaces`), and the shell then removes
    the quotes and the backslash escapes.  A PlatformIO core escapes a path
    for the shell itself: measured, the STM32 variant directory is in
    `LINKFLAGS` and in `LIBPATH` as `L475R\\(C-E-G\\)T_...`, and the
    directory on disk has no backslash.  The function applies the two steps
    through `shlex`.  A token that gives more or fewer than one word is
    refused.

    On Windows the backslash separates directories, thus only a pair of
    surrounding double quotes is removed.
    """
    if posix is None:
        posix = os.name != "nt"
    if not posix:
        if len(token) >= 2 and token[0] == token[-1] == '"':
            return token[1:-1]
        return token
    quoted = token
    if any(char.isspace() for char in token) and not (
        len(token) >= 2 and token[0] == token[-1] == '"'
    ):
        quoted = f'"{token}"'
    try:
        words = shlex.split(quoted)
    except ValueError:
        return None
    return words[0] if len(words) == 1 else None


def windows_words(text: str) -> list[str]:
    """Return the arguments that a Windows program gets for the command line *text*.

    The rules of `CommandLineToArgvW`, which the C runtime and thus ld and
    gcc use.  A double quote starts or ends a quoted part anywhere in a
    word, `""` in a quoted part is one quote, and a backslash is literal
    unless it comes before a quote: 2n backslashes give n and keep the quote
    as a delimiter, 2n+1 give n and a literal quote.  `shlex` with
    `posix=False` knows a quote only at the start of a word, thus
    `-L"C:/My Projects/x"` became two words.

    The `""` rule is the rule of the C runtime since 2008: the quoted part
    stays open after the literal quote.  The first argument of a command
    line has other rules, and this function never gets one.  The layer of
    `cmd.exe /C` (`^`, `%`), which CMake adds for PRE_LINK and POST_BUILD,
    is not modelled: those commands hold no argument of the link.
    """
    words: list[str] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t":
            index += 1
        if index == len(text):
            break
        word: list[str] = []
        quoted = False
        while index < len(text) and (quoted or text[index] not in " \t"):
            char = text[index]
            if char == "\\":
                end = index
                while end < len(text) and text[end] == "\\":
                    end += 1
                count = end - index
                if end < len(text) and text[end] == '"':
                    word.append("\\" * (count // 2))
                    if count % 2:
                        word.append('"')
                        end += 1
                else:
                    word.append("\\" * count)
                index = end
            elif char == '"':
                if quoted and index + 1 < len(text) and text[index + 1] == '"':
                    word.append('"')
                    index += 2
                    continue
                quoted = not quoted
                index += 1
            else:
                word.append(char)
                index += 1
        words.append("".join(word))
    return words


def shell_words(text: str) -> list[str] | None:
    """Return the arguments that the command line *text* gives, or None when it cannot be split.

    POSIX shell rules on a POSIX host, `windows_words` on Windows: a build
    tool there starts the command through `cmd.exe` or `CreateProcess`.
    """
    if os.name == "nt":
        return windows_words(text)
    try:
        return shlex.split(text)
    except ValueError:
        return None


def _word(raw: str, unquote: bool) -> str | None:
    """Return the argument *raw* as ld gets it.

    *unquote* is False for a word that a shell split already, such as a
    word of a ninja file: a second pass would remove a backslash or refuse
    an apostrophe that is part of the path.
    """
    return ld_path(raw) if unquote else raw


def search_dirs(
    linkflags: list[str | None], libpath: list[str | None], *, unquote: bool = True,
) -> list[Path | None]:
    """Return the directories in which `ld` looks for a script, in its order.

    The driver `-L` options of *linkflags*, then *libpath* (the `LIBPATH` of
    SCons, which `$_LIBDIRFLAGS` puts after `$LINKFLAGS`), then the `-Wl,`
    ones, see `library_dirs_from_flags`.  An entry that did not expand, or
    that `ld_path` refuses, is None: the search stops there.
    """
    driver, passed = library_dirs_from_flags(linkflags)
    result: list[Path | None] = []
    for raw in [*driver, *libpath, *passed]:
        path = _word(raw, unquote) if raw is not None else None
        result.append(Path(path) if path else None)
    return result


def resolve_script(name: str, cwd: Path, directories: list[Path | None]) -> Path | None:
    """Return the file `ld` opens for `-T name`, or None.

    The ld manual: when the file is not in the current directory, ld looks
    for it in the directories of the `-L` options.  The compiler driver puts
    every `-L` option before the `-T` options, thus all of them count.  A
    None in *directories* stops the search, because `ld` looks there first
    and this module cannot.  The driver adds directories of its own at the
    end.  A script that only such a directory holds gives None.

    A relative directory is relative to *cwd*, because `ld` runs there.  The
    indexer runs in another directory (the daemon, the MCP server, a run
    with `--project`), and a probe relative to it can find another script.
    An absolute directory stays as it is: `cwd / directory` gives it.
    """
    path = Path(name)
    if path.is_absolute():
        return path.resolve() if path.is_file() else None
    if (cwd / path).is_file():
        return (cwd / path).resolve()
    for directory in directories:
        if directory is None:
            return None
        candidate = cwd / directory / path
        if candidate.is_file():
            return candidate.resolve()
    return None


def _unread_link_input(tokens: list[str | None], cwd: Path, *, unquote: bool = True) -> str:
    """Return why a link input that this module does not read makes the link unknown, or "".

    * `-Xlinker` passes its value to ld.  The module does not read it, thus
      a value that can be a script, a directory, a value or a library file
      would be lost.  A harmless value such as `-Map=out.map` stays.  An
      input file that it passes is read, see `input_files_from_flags`.
    * A response file (`@file`, also in a `-Wl,` token) can hold any
      argument, such as `-T b.ld`.  With the scripts only in it, the answer
      would be "no script", which deletes a correct map.
    * A specs file can add a link input, see `_specs_problem`.
    """
    for index, token in enumerate(tokens):
        if token is None:
            continue
        if token in ("-Xlinker", "--for-linker") or token.startswith("--for-linker="):
            if "=" in token:
                value: str | None = token.split("=", 1)[1]
            else:
                value = tokens[index + 1] if index + 1 < len(tokens) else None
            if value is None or (value.startswith(("-", "@")) and _may_be_link_input(value, _LINK_INPUT_LD)):
                return f"{token} passes {value!r}, which is not read"
        elif token in ("-specs", "--specs"):
            # gcc also takes the file in the next argument.
            specs = tokens[index + 1] if index + 1 < len(tokens) else None
            problem = _specs_problem(specs, cwd, unquote=unquote) if specs else f"{token} has no file"
            if problem:
                return problem
        elif token.startswith("@") or (
            token.startswith("-Wl,") and any(part.startswith("@") for part in token[4:].split(","))
        ):
            return f"the response file in {token!r} is not read"
        elif token.startswith(("-specs=", "--specs=")):
            problem = _specs_problem(token.split("=", 1)[1], cwd, unquote=unquote)
            if problem:
                return problem
    return ""


def _specs_problem(raw: str, cwd: Path, *, unquote: bool = True) -> str:
    """Return why the specs file *raw* makes the link unknown, or "".

    A name with no directory is found by gcc in its own directories, and
    only the names in `_HARMLESS_SPECS` are known there.  A name with a
    directory is relative to the link directory, and the file is read: a
    file that holds a link input, or that cannot be read, makes the link
    unknown.
    """
    name = _word(raw, unquote)
    if name is None:
        return f"the specs file {raw!r} cannot be read"
    path = Path(name)
    # The text decides, not `path.parts`: pathlib drops the `./` of
    # `./board.specs`, and gcc reads that name in the link directory.
    if not any(separator in name for separator in {"/", os.sep, os.altsep or "/"}):
        return "" if name in _HARMLESS_SPECS else (
            f"the specs file {name!r} is in a directory of gcc that the dump does not state"
        )
    try:
        text = (cwd / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"the specs file {name!r} cannot be read"
    if _SPECS_LINK_INPUT.search(text):
        return f"the specs file {name!r} can add a link input"
    return ""


# The compiler driver options that take the NEXT argument as their value.
# The value is no input file, also when a file of that name is on disk: a
# map file of an earlier build is a text file.  `--for-linker` is an alias
# of `-Xlinker` and `--library-directory` of `-L`, measured with `gcc -###`.
_DRIVER_TAKES_NEXT = frozenset({
    "-T", "-L", "-l", "-Xlinker", "--for-linker", "--library-directory", "-u", "-e", "-z", "-o",
    "-x", "-B", "-include", "-imacros", "-isystem", "-idirafter", "-iprefix", "-iwithprefix",
    "-MF", "-MT", "-MQ", "--param", "-wrapper", "-Xassembler", "-Xpreprocessor", "-aux-info",
    "-specs", "--specs",
})

# The ld options that take the next argument as their value, from `ld
# --help` (GNU ld 2.47).  An argument of ld after one of them is its value,
# and no input file.
_LD_TAKES_NEXT = _TAKES_VALUE_LD | frozenset({
    "-Map", "--Map", "-e", "--entry", "-u", "--undefined", "-z", "-o", "--output", "-y",
    "--trace-symbol", "-R", "--just-symbols", "-a", "-A", "--architecture", "-b", "--format",
    "-m", "-O", "-h", "-soname", "--wrap", "--section-start", "--sort-section", "--hash-style",
    "--dynamic-linker", "-rpath", "-rpath-link", "--version-script", "--retain-symbols-file",
    "--image-base", "-Tbss", "-Tdata", "-Ttext", "-Ttext-segment", "--exclude-libs", "--plugin",
    "-plugin", "--plugin-opt", "-F", "--filter", "-f", "--auxiliary", "-G", "-I", "-P", "-Y",
    "--sysroot", "--dynamic-list", "--export-dynamic-symbol-list", "--section-ordering-file",
    "--remap-inputs-file", "-c", "--mri-script", "--dependency-file", "--out-implib",
    "--output-def", "--error-handling-script", "--audit", "--depaudit",
})

# The ld options that set the format of the input files after them.  Only
# the format `binary` makes a file data: measured with arm-none-eabi-ld
# 2.40, after `-b elf32-littlearm` (or `srec`, `ihex`) ld does not recognise
# a text file, and reads it as a linker script all the same.
_FORMAT_OPTIONS = frozenset({"-b", "--format", "-format"})


def _ld_arguments(tokens: list[str | None]) -> list[str]:
    """Return the arguments that the driver gives to ld, in their order.

    gcc keeps the order of the `-Wl,` parts, the `-Xlinker` values, the `-l`
    options and the positional arguments, thus `-Wl,-Map` and the next
    positional argument are one option of ld, as `-Xlinker -Map -Wl,out.map`
    is.  Another driver option and its value are not in the result: the
    driver handles them.  A token that did not expand is not in the result
    either: the caller decides about it with `_lost_link_input`.
    """
    arguments: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token is None:
            continue
        if token in ("-Xlinker", "--for-linker", "-l"):
            value = tokens[index] if index < len(tokens) else None
            index += 1
            if token == "-l":
                arguments.append(token)
            if value is not None:
                arguments.append(value)
        elif token.startswith("--for-linker="):
            arguments.append(token.split("=", 1)[1])
        elif token.startswith("-Wl,"):
            arguments.extend(part for part in token[4:].split(",") if part)
        elif token.startswith("-l"):
            arguments.append(token)
        elif token.startswith("-"):
            if token in _DRIVER_TAKES_NEXT:
                index += 1
        else:
            arguments.append(token)
    return arguments


def _scan_ld_arguments(tokens: list[str | None]) -> Iterator[tuple[str, str | None, bool]]:
    """Yield each argument of ld with the option it is the value of, and the format state.

    The option is the ld option (`_LD_TAKES_NEXT`) whose value the argument
    is, or None.  The state is True while `-b binary` or `--format=binary`
    is in effect, see `_FORMAT_OPTIONS`.
    """
    pending: str | None = None
    binary = False
    for argument in _ld_arguments(tokens):
        if pending is not None:
            option, pending = pending, None
            if option in _FORMAT_OPTIONS:
                binary = argument == "binary"
            yield argument, option, binary
            continue
        if argument.startswith("-"):
            name, equals, value = argument.partition("=")
            if equals and name in _FORMAT_OPTIONS:
                binary = value == "binary"
            elif argument in _LD_TAKES_NEXT:
                pending = argument
        yield argument, None, binary


def input_files_from_flags(tokens: list[str | None]) -> list[str]:
    """Return the input files of ld in *tokens* that can be a linker script, in order.

    An input file is an argument of ld (`_ld_arguments`) that is no option
    and no value of an option (`_LD_TAKES_NEXT`): `-u app_main`, `-Wl,-Map`
    with `out.map` after it, and `-Xlinker -Map -Xlinker out.map` give no
    input file.  A file after `-b binary` is data, and no script.
    """
    return [
        argument for argument, option, binary in _scan_ld_arguments(tokens)
        if option is None and not argument.startswith("-") and not binary
    ]


def implicit_scripts_from_flags(tokens: list[str | None]) -> list[str]:
    """Return the `-l:<file>` names of *tokens*, in order.

    The forms are `-l:<file>`, `--library=:<file>`, and `-l` or `--library`
    with `:<file>` in the next argument of ld, also from a `-Wl,` token or
    the next token.  A file after `-b binary` is data, and no script.  Only
    the `:` form is here: `-lc` finds `libc.a` in a directory of the
    toolchain, which the dump does not state, and an archive is no script.
    """
    names: list[str] = []
    for argument, option, binary in _scan_ld_arguments(tokens):
        if binary:
            continue
        if option in ("-l", "--library", "-library") and argument.startswith(":"):
            names.append(argument[1:])
        elif option is None and argument.startswith(("-l:", "--library=:", "-library=:")):
            names.append(argument.split(":", 1)[1])
    return names


def _is_text(path: Path) -> bool | None:
    """Say if *path* can be a linker script, or None when it cannot be read.

    An object file, an archive, LLVM bitcode, a COFF or a Mach-O file
    holds a NUL byte in its first kilobyte, or starts with a known magic
    number.  A linker script is text and holds none.
    """
    try:
        with path.open("rb") as stream:
            head = stream.read(1024)
    except OSError:
        return None
    return not (head.startswith(_BINARY_MAGIC) or b"\0" in head)


def _find_implicit_script(name: str, cwd: Path, directories: list[Path | None]) -> tuple[bool, Path | None]:
    """Return `(known, script)` for one `-l:<name>`.

    ld searches only the `-L` directories for it, and then directories of
    its own, which the dump does not state.  Thus a file that is not found,
    or a None directory before it, is not known.  A found object file or
    archive is known and is no script.
    """
    path = Path(name)
    candidates = [path] if path.is_absolute() else []
    for directory in [] if path.is_absolute() else directories:
        if directory is None:
            return False, None
        candidates.append(cwd / directory / path)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        text = _is_text(candidate)
        if text is None:
            return False, None
        return True, (candidate.resolve() if text else None)
    return False, None


def _has_insert(path: Path) -> bool:
    """Say if the script in *path* holds an INSERT command."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _INSERT.search(strip_comments(text)) is not None


def resolve_link_scripts(
    linkflags: list[str | None], libpath: list[str | None], cwd: Path, *, unquote: bool = True,
) -> list[Path] | None:
    """Return the linker scripts that `ld` reads for one link command, or None.

    *linkflags* are the arguments of the compiler driver, a None for one
    that did not expand.  The caller first decides with `_lost_link_input`
    if such an argument can matter.  *libpath* are directories that the
    build system adds after them, see `search_dirs`.  *cwd* is the directory
    the link runs in.  *unquote* is False when a shell split the arguments
    already, see `_word`.

    `ld` reads the `-T` scripts in their order.  The default script, from
    the LAST `-dT` or `--default-script`, counts only when every `-T` script
    holds INSERT: a `-T` script without INSERT replaces the default script.
    Measured on the STM32 Arduino core: the `-T` script holds INSERT for
    `.noinit`, and the default script of the variant holds the memory map.
    With `build_flags = -Wl,-T,custom.ld` the ELF holds no symbol of the
    variant script.

    A file that `-l:<name>` finds and that is text is an implicit linker
    script.  ld adds it to the main script, thus it comes after the others.

    An input file (`input_files_from_flags`) that is text makes the answer
    "not known", and is not read as a script.  WHY not read it: a word that
    looks like an input file can be the value of an ld option that this
    module does not know, and a map file, a version script or an HTML blob
    of an earlier build is text too.  Its parse would put wrong symbols in
    the index, and a parse of a 12 MB map file did not end in 4 minutes.
    Measured: no such input file in 37 ninja links and 6 PlatformIO links.
    An input file that is not on disk is skipped: ld would stop the link on
    it, thus such a word is the value of an option, and an object file or
    an archive is no script.

    None when the command holds a link input that this module does not read
    (`_unread_link_input`, a text input file), when a script that it names
    is not on disk, or when a name cannot be read.  The answer is then not
    known, and a list without that script would give a map of another link.
    """
    problem = _unread_link_input(linkflags, cwd, unquote=unquote)
    if problem:
        log.info("linker script: the link cannot be read: %s", problem)
        return None
    directories = search_dirs(linkflags, libpath, unquote=unquote)
    scripts: list[Path] = []
    defaults: list[Path] = []
    for kind, raw in script_options(linkflags):
        name = _word(raw, unquote)
        resolved = resolve_script(name, cwd, directories) if name else None
        if resolved is None:
            log.info("linker script: %r of the link is not on disk or cannot be read", raw)
            return None
        (defaults if kind == "dT" else scripts).append(resolved)
    if defaults and all(_has_insert(script) for script in scripts):
        scripts.append(defaults[-1])
    for raw_library in implicit_scripts_from_flags(linkflags):
        library = _word(raw_library, unquote) if raw_library else None
        known, implicit = _find_implicit_script(library, cwd, directories) if library else (False, None)
        if not known:
            log.info("linker script: -l:%s of the link is not found in its -L directories", raw_library)
            return None
        if implicit is not None:
            scripts.append(implicit)
    for raw_input in input_files_from_flags(linkflags):
        name = _word(raw_input, unquote)
        candidate = cwd / name if name else None
        if candidate is None or not candidate.is_file():
            continue
        text = _is_text(candidate)
        if text is None or text:
            log.info("linker script: the input file %r of the link can be an implicit linker script", raw_input)
            return None
    found: list[Path] = []
    for script in scripts:
        if script not in found:
            found.append(script)
    return found
