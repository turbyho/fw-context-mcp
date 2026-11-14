"""Read the compile commands of a build from the log of ``make -Bnkwp``.

``make -Bnkwp`` prints each command of the build and runs none:

- ``-B`` makes each target.
- ``-n`` prints the commands.
- ``-k`` goes on after an error.
- ``-w`` prints ``Entering directory`` and ``Leaving directory``.
- ``-p`` prints the data base of each make, after its commands.

The data base gives ``SHELL`` and the other variables that decide how the
shell of that make reads a command.  This module reads that log into the
entries of ``compile_commands.json``.

WHY a reader of its own and not compiledb: compiledb read the log with
bashlex, thus as bash.  On Windows the backslash of a path
(``C:\\fwctx-sdk\\...``) is an escape for bash, and the path broke.  It
also knew a compiler only without ``.exe``: ``arm-zephyr-eabi-gcc.EXE`` of
a Windows build gave an empty database (measured 2026-11-13).  This reader
splits each command by the rules of the shell that make uses for it
(``_recipe_shell``), and knows a compiler by the name grammar of the
driver query (``_driver_query.is_compiler_driver_name``).

Rules of the reader:

- Each make (``Entering directory`` up to ``Leaving directory``) has its
  directory and its data base.  The shell of its commands comes from
  ``SHELL`` of its data base, by the rules of GNU make for the host
  (:func:`_is_posix_shell`).
- A command is a compile when its program is a compiler driver.  Before
  the program there can be assignments (``CCACHE_DIR=x``, POSIX shell),
  the wrappers of ``_WRAPPER_FLAGS`` (``env``, ``nice``, ``time`` ...), and
  one launcher of ``LAUNCHERS`` (``ccache``).  The entry starts at the
  compiler.  Other programs that run a command (``xargs``, ``sh -c``,
  ``find -exec``) are not followed.
- Each source of a compile (``SOURCE_SUFFIXES``, and no value of an option
  of ``DRIVER_TAKES_NEXT``) is an entry of its own, with the other sources
  of the command left out.  A source that does not exist is left out: a
  target that a dry run only prints can name a file that no step made.
- A file that the build compiles more than once keeps the entry of its
  last compile: the index holds one parse of a file for each build.
- A line that the reader cannot read is an error when a word of it names
  a compiler, else make printed text that is not a command
  (``$(info ...)``).  A compile that the shell runs only under a
  condition, in a directory that the shell computes, or with a word that
  the shell computes, is an error: the database would hold a guess.

Limit: the log does not tell the output of ``$(info ...)`` from a command.
A line of ``$(info ...)`` that reads as a compile of an existing source
gives an entry.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from ...utils import TU_EXTENSIONS
from .._driver_options import DRIVER_TAKES_NEXT
from .._driver_query import is_compiler_driver_name
from ._recipe_shell import Command, Unreadable, Word, cmd_commands, make_fast_words, posix_commands, recipe_commands

#: The suffixes of the sources that a compile command compiles: C, C++,
#: and the assembly of GCC (``.s``, and ``.S`` and ``.sx`` with the
#: preprocessor).  The index reads an assembly unit with a reader of its own.
SOURCE_SUFFIXES: frozenset[str] = TU_EXTENSIONS | {".s", ".S", ".sx"}

#: The programs that run a compiler for a build: the compiler is the word
#: after them.
LAUNCHERS: frozenset[str] = frozenset({"ccache", "sccache", "distcc", "icecc", "buildcache"})

#: The names of the POSIX shells.  On Windows GNU make takes a shell as a
#: POSIX shell when its path holds "sh" (``find_and_set_default_shell``),
#: and each name here does.
POSIX_SHELLS: frozenset[str] = frozenset({"sh", "bash", "dash", "ash", "ksh", "mksh", "zsh", "yash", "posh"})

#: The programs that run the command after their options, with their
#: options without a value.  The options are those of POSIX and of GNU
#: coreutils.  An other option is an error: the reader cannot tell where
#: the command starts.
_WRAPPER_FLAGS: dict[str, frozenset[str]] = {
    "env": frozenset({"-i", "-", "--ignore-environment"}),
    "exec": frozenset({"-c", "-l", "-cl", "-lc"}),
    "command": frozenset({"-p"}),
    "nice": frozenset(),
    "nohup": frozenset(),
    "time": frozenset({"-p"}),
    "timeout": frozenset({"--preserve-status", "--foreground", "-v", "--verbose"}),
}

#: The options of a wrapper with a value: ``-u NAME``, ``-uNAME`` and
#: ``--unset=NAME``.
_WRAPPER_VALUES: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "exec": frozenset({"-a"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
}

_DIRECTORY_LINE = re.compile(
    r"^(?:# )?(?P<program>[^\s\[\]:]+)(?:\[\d+\])?: (?P<action>Entering|Leaving) "
    r"(?:directory [`'](?P<directory>.*)'|an unknown directory)$"
)
_MESSAGE_LINE = re.compile(r"^(?:# )?(?P<program>[^\s\[\]:]+)(?:\[\d+\])?: ")
_DATA_BASE_START = "# Make data base, printed on "
_DATA_BASE_END = "# Finished Make data base on "
_SECTIONS = {
    "# variables": "variables",
    "# pattern-specific variable values": "patterns",
    "# directories": "other",
    "# implicit rules": "other",
    "# files": "files",
    "# vpath search paths": "other",
}
_GLOBAL_VARIABLE = re.compile(
    r"^(?P<name>SHELL|\.SHELLFLAGS|IFS|PATH|CDPATH|MAKE_RESTARTS) (?P<flavor>[:+]?)= (?P<value>.*)$"
)
_PATTERN_VARIABLE = re.compile(r"^# (?P<name>SHELL|\.SHELLFLAGS|IFS) [:+]?= (?P<value>.*)$")
_TARGET_VARIABLE = re.compile(r"^[^\s#].*?: (?P<name>SHELL|\.SHELLFLAGS|IFS) [:+]?= (?P<value>.*)$")
_WHITESPACE_VALUE = re.compile(r"\$\(subst ,,(?P<value>\s*)\)")
_UNREADABLE_WORDS = re.compile(r"[\s;&|()<>'\"`]+")
#: A byte of the log that is not UTF-8: the decode with surrogateescape
#: gives it as a lone surrogate (U+DC80 to U+DCFF).
_NOT_UTF8 = re.compile("[\udc80-\udcff]")


@dataclass(frozen=True)
class CompileCommand:
    """One compile of the log: its directory, its arguments from the compiler on, and the positions of its sources."""

    directory: Path
    arguments: tuple[str, ...]
    sources: tuple[int, ...]

    def outputs(self) -> list[Path]:
        """Return the files that the compile writes, by the rules of the GCC driver.

        ``-o`` names the file.  Without it, ``-c`` writes ``<stem>.o`` and
        ``-S`` writes ``<stem>.s`` for each source, in the directory of the
        command, and a link writes ``a.out`` (``man gcc``, ``-o``).  ``-E``,
        ``-M``, ``-MM`` and ``-fsyntax-only`` without ``-o`` write no file.
        A command without an input file writes nothing.
        """
        output: str | None = None
        modes: set[str] = set()
        inputs = 0
        index = 1
        while index < len(self.arguments):
            word = self.arguments[index]
            if word in ("-o", "--output") and index + 1 < len(self.arguments):
                output = self.arguments[index + 1]
                index += 2
                continue
            if word.startswith("--output="):
                output = word.split("=", 1)[1]
            elif word.startswith("-o") and len(word) > 2:
                output = word[2:]
            elif word in DRIVER_TAKES_NEXT:
                index += 2
                continue
            elif word in ("-c", "-S", "-E", "-M", "-MM", "-fsyntax-only"):
                modes.add(word)
            elif not word.startswith("-"):
                inputs += 1
            index += 1
        if not inputs or output == "-" or (output is None and modes & {"-E", "-M", "-MM", "-fsyntax-only"}):
            return []
        if output is not None:
            return [_normal(self.directory / output)]
        if "-S" in modes or "-c" in modes:
            suffix = ".s" if "-S" in modes else ".o"
            return [_normal(self.directory / (PurePath(self.arguments[source]).stem + suffix)) for source in self.sources]
        return [_normal(self.directory / "a.out")]


@dataclass
class _Database:
    """The variables of the data base of one make that decide how its shell reads a command."""

    variables: dict[str, tuple[str, bool]] = field(default_factory=dict)
    specific: list[tuple[str, str]] = field(default_factory=list)
    one_shell: bool = False
    section: str = ""

    def read(self, line: str) -> None:
        """Read one line of the data base (``print_variable`` in ``variable.c`` of GNU make)."""
        section = _SECTIONS.get(line.lower())
        if section is not None:
            self.section = section
            return
        if self.section == "variables":
            found = _GLOBAL_VARIABLE.match(line)
            if found is not None:
                self.variables[found.group("name")] = _variable_value(found.group("flavor"), found.group("value"))
        elif self.section == "patterns":
            found = _PATTERN_VARIABLE.match(line)
            if found is not None:
                self.specific.append((found.group("name"), found.group("value")))
        elif self.section == "files":
            if line.startswith(".ONESHELL:"):
                self.one_shell = True
            found = _TARGET_VARIABLE.match(line)
            if found is not None:
                self.specific.append((found.group("name"), found.group("value")))

    @property
    def restarts(self) -> int:
        """Return how many times this make restarted before it printed this data base.

        WHY: when make remakes a makefile that it read (``include $(DEPS)``
        with a rule for ``%.d``), it reads its makefiles again and sets
        ``MAKE_RESTARTS`` (``main.c`` of GNU make).  Without a restart the
        variable is not set.
        """
        value = self.variables.get("MAKE_RESTARTS", ("0", True))[0].lstrip("-")
        return int(value) if value.isdigit() else 0


def _variable_value(flavor: str, printed: str) -> tuple[str, bool]:
    """Return the value of a variable as the data base prints it, and if it is the expanded value.

    make prints a simple variable (``:=``) with each ``$`` doubled, and a
    recursive variable (``=``) as it is, with its references.  A value of
    whitespace only is printed as ``$(subst ,,<value>)``.
    """
    whitespace = _WHITESPACE_VALUE.fullmatch(printed)
    if whitespace is not None:
        return whitespace.group("value"), True
    if flavor == ":":
        return printed.replace("$$", "$"), True
    return printed, "$" not in printed


@dataclass(frozen=True)
class _Shell:
    """How the shell of one make reads a command."""

    posix: bool
    fast_path: bool
    cdpath: bool


@dataclass
class _Level:
    """One make of the log: its directory, and each pass with its command lines and its data base.

    A make that restarts (GNU make 4, ``MAKE_RESTARTS``) prints the commands
    of its first pass and a data base, then the commands of the next pass
    and a data base, in one directory.  Each pass has its own data base,
    thus its own shell.  *lines* holds the command lines of the pass that
    has no data base yet, each with its position in the log.
    """

    directory: Path
    lines: list[tuple[int, str]] = field(default_factory=list)
    passes: list[tuple[list[tuple[int, str]], _Database]] = field(default_factory=list)


def read_log(log: str, *, windows: bool) -> list[CompileCommand]:
    """Return the compiles of the log *log* of ``make -Bnkwp``, in the sequence of the log.

    *windows* is true when make ran on Windows: the rules of GNU make
    for Windows select the shell, and a file name has no case.

    Raises RuntimeError for a log that the reader cannot read completely,
    and for a compile that it cannot tell.
    """
    levels: list[_Level] = []
    programs: set[str] = set()
    found: list[tuple[int, CompileCommand]] = []
    database: _Database | None = None
    position = 0
    before_directory = False
    lines = log.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if database is not None:
            if line.startswith(_DATA_BASE_END):
                level = levels[-1]
                if level.passes and database.restarts <= level.passes[-1][1].restarts:
                    raise RuntimeError(
                        "make printed a data base of a make that printed no directory. A sub-make with "
                        "--no-print-directory hides its directory, and fw-context cannot tell where its "
                        "commands run."
                    )
                level.passes.append((level.lines, database))
                level.lines = []
                database = None
            else:
                database.read(line)
            continue
        if line.startswith(_DATA_BASE_START):
            if not levels:
                raise RuntimeError(
                    "make printed a data base of a make that printed no directory. A sub-make with "
                    "--no-print-directory hides its directory, and fw-context cannot tell where its commands run."
                )
            database = _Database()
            continue
        directory_line = _DIRECTORY_LINE.match(line)
        if directory_line is not None:
            programs.add(directory_line.group("program"))
            found += _directory_change(directory_line, levels, windows=windows)
            continue
        message = _MESSAGE_LINE.match(line)
        if (message is not None and message.group("program") in programs) or not line.strip():
            continue
        if not levels:
            # Before the first directory line make prints only its own
            # output, never a command.  GNU make 3.81 (the make of macOS)
            # prints the directory with its first message (misc.c:203 of
            # 3.81), and each command is such a message.  Its version (-p)
            # and the text of $(info) come before it (measured on macOS 26).
            # make 4.4 prints the directory first.
            before_directory = True
            continue
        while _continues(line) and index < len(lines):
            line += "\n" + lines[index]
            index += 1
        position += 1
        levels[-1].lines.append((position, line))
    if database is not None:
        raise RuntimeError("the log of make stops in a data base: make stopped early")
    if levels:
        raise RuntimeError(f"the log of make stops in {levels[-1].directory}: make stopped early")
    if before_directory and not programs:
        raise RuntimeError(
            "make printed no directory: --no-print-directory (in MAKEFLAGS of the environment, for example) "
            "hides it, and fw-context cannot tell where the commands run"
        )
    found.sort(key=lambda item: item[0])
    return [command for _, command in found]


def _directory_change(line: re.Match[str], levels: list[_Level], *, windows: bool) -> list[tuple[int, CompileCommand]]:
    """Apply an ``Entering`` or ``Leaving`` line to *levels*; return the compiles of the make that it ends."""
    printed = line.group("directory")
    if printed is None:
        raise RuntimeError("make printed 'an unknown directory': fw-context cannot tell where its commands run")
    directory = Path(printed)
    if line.group("action") == "Entering":
        if not directory.is_dir():
            raise RuntimeError(
                f"make entered {printed}, which is no directory of this host. A make of MSYS2 or Cygwin "
                "prints paths of its own: use a make for the host."
            )
        levels.append(_Level(directory))
        return []
    if not levels or os.path.normcase(levels[-1].directory) != os.path.normcase(directory):
        raise RuntimeError(
            f"make left {printed} while the log was in {levels[-1].directory if levels else 'no directory'}: "
            "the output of two makes mixed, as in a parallel build"
        )
    return _compiles_of_level(levels.pop(), windows=windows)


def _continues(line: str) -> bool:
    """Say if *line* goes on in the next line: it ends with an odd number of backslashes, as make reads a makefile."""
    count = len(line) - len(line.rstrip("\\"))
    return count % 2 == 1


def _compiles_of_level(level: _Level, *, windows: bool) -> list[tuple[int, CompileCommand]]:
    """Return the compiles of the command lines of one make, each with its position in the log.

    The lines of each pass go with the data base of that pass.  Lines after
    the last data base go with it: make prints its data base when it
    stops, thus such a line is text of make, not a command of a new pass.
    """
    if not level.passes:
        raise RuntimeError(f"make printed no data base for {level.directory}: fw-context cannot tell its shell")
    passes = list(level.passes)
    last_lines, last_database = passes[-1]
    passes[-1] = (last_lines + level.lines, last_database)
    compiles: list[tuple[int, CompileCommand]] = []
    for lines, database in passes:
        shell = _shell_of(database, level.directory, windows=windows)
        for position, line in lines:
            for command in _line_commands(line, level.directory, shell, windows=windows):
                compile_command = _compile_of(command, line, posix=shell.posix, windows=windows)
                if compile_command is None:
                    continue
                if _NOT_UTF8.search(" ".join((str(compile_command.directory), *compile_command.arguments))):
                    raise RuntimeError(
                        f"make printed a compile in {level.directory} with a byte that is not UTF-8: "
                        "fw-context reads the log of make as UTF-8 and cannot tell the path or the argument"
                    )
                compiles.append((position, compile_command))
    return compiles


def _shell_of(database: _Database, directory: Path, *, windows: bool) -> _Shell:
    """Return how the shell of the make in *directory* reads a command, from its data base."""
    if database.one_shell:
        raise RuntimeError(
            f"the Makefile of {directory} sets .ONESHELL: make gives a whole recipe to one shell, and "
            "its log does not show where a recipe ends. fw-context cannot read its commands."
        )
    if "SHELL" not in database.variables:
        raise RuntimeError(f"the data base of make for {directory} holds no SHELL")
    shell, expanded = database.variables["SHELL"]
    if not expanded:
        raise RuntimeError(
            f"SHELL is {shell!r} for the recipes in {directory}: fw-context does not expand a variable of "
            "make, thus it cannot tell the shell. Set SHELL with := or without a reference."
        )
    posix = _is_posix_shell(shell, directory, database, windows=windows)
    for name, value in database.specific:
        # Windows: make goes the slow way for a SHELL that differs, thus
        # each difference counts.  POSIX: the kind of the shell counts.
        if name == "SHELL":
            same = value == shell if windows else _names_posix_shell(value) == posix
        else:
            same = not windows or value == database.variables.get(name, ("", True))[0]
        if not same:
            raise RuntimeError(
                f"a target or a pattern of the Makefile in {directory} sets {name} to {value!r}: the log of "
                "make does not tell the target of a command, thus fw-context cannot tell how its shell reads it"
            )
    flags = database.variables.get(".SHELLFLAGS", ("-c", True))[0]
    separators = database.variables.get("IFS", ("", True))[0]
    fast_path = windows and flags in ("-c", "-ec") and all(char in " \t\n" for char in separators)
    cdpath = posix and bool(database.variables.get("CDPATH", ("", True))[0])
    return _Shell(posix=posix, fast_path=fast_path, cdpath=cdpath)


def _is_posix_shell(shell: str, directory: Path, database: _Database, *, windows: bool) -> bool:
    """Say if make runs the commands with a POSIX shell (True) or with cmd.exe (False).

    POSIX: make runs ``$(SHELL)``; its first word must name a POSIX shell.
    Windows (``find_and_set_default_shell``, ``main.c``): a name ``cmd`` or
    ``cmd.exe`` selects cmd.exe.  Else make looks for the file in the
    directory of the make, then in each directory of ``PATH``.  Without
    it, make runs the commands with cmd.exe (batch mode).  With it, the file
    must be a POSIX shell.

    Raises RuntimeError for a shell that is neither.
    """
    if windows:
        if PureWindowsPath(shell).name.lower() in ("cmd", "cmd.exe"):
            return False
        found = _find_windows_shell(shell, directory, database)
        if found is None:
            return False
        if _program_name(found, windows=True) in POSIX_SHELLS:
            return True
    elif _names_posix_shell(shell):
        return True
    raise RuntimeError(
        f"SHELL is {shell!r} for the recipes in {directory}: fw-context reads the commands of a POSIX shell "
        f"({', '.join(sorted(POSIX_SHELLS))}) or of cmd.exe"
    )


def _names_posix_shell(shell: str) -> bool:
    """Say if the first word of the POSIX value of SHELL names a POSIX shell: make on POSIX runs ``$(SHELL)`` split at spaces."""
    words = shell.split()
    return bool(words) and PurePosixPath(words[0]).name in POSIX_SHELLS


def _find_windows_shell(shell: str, directory: Path, database: _Database) -> str | None:
    """Return the file that GNU make on Windows finds for *shell*, or None."""
    candidate = Path(shell) if PureWindowsPath(shell).is_absolute() else directory / shell
    if candidate.exists():
        return str(candidate)
    for entry in database.variables.get("PATH", ("", True))[0].split(";"):
        if entry and (Path(entry) / shell).exists():
            return str(Path(entry) / shell)
    return None


def _line_commands(line: str, directory: Path, shell: _Shell, *, windows: bool) -> list[Command]:
    """Return the simple commands of one command line of the log, as the shell of its make runs them."""
    commands: list[Command] = []
    for text in recipe_commands(line):
        if shell.fast_path:
            words = make_fast_words(text, posix_shell=shell.posix)
            if words is not None:
                if words:
                    # The C runtime of a MinGW program expands * and ? of
                    # an argument that make gives it.
                    dynamic = not shell.posix
                    commands.append(Command(
                        tuple(Word(word, dynamic=dynamic and any(c in word for c in "*?")) for word in words),
                        directory, conditional=False,
                    ))
                continue
        try:
            if shell.posix:
                commands += posix_commands(text, directory, cdpath=shell.cdpath)
            else:
                commands += cmd_commands(text, directory)
        except Unreadable as exc:
            _check_unreadable(text, exc, windows=windows)
    return commands


def _check_unreadable(text: str, error: Unreadable, *, windows: bool) -> None:
    """Raise RuntimeError when a word of the unreadable line *text* names a compiler.

    make prints the text of ``$(info ...)`` into the same log, and such a
    text need not be a command line.  A line without a compiler holds no
    compile, thus the reader goes on without it.
    """
    pure = PureWindowsPath if windows else PurePosixPath
    for word in _UNREADABLE_WORDS.split(text):
        if word and is_compiler_driver_name(pure(word.lower() if windows else word)):
            raise RuntimeError(
                f"make printed a command that fw-context cannot read ({error}), and it names the compiler "
                f"{word}: {text!r}"
            )


def _compile_of(command: Command, line: str, *, posix: bool, windows: bool) -> CompileCommand | None:
    """Return the compile of *command*, or None when its program is no compiler driver.

    Raises RuntimeError for a compile that the reader cannot tell: the
    program, the directory or a word that the shell computes, or a
    compile under a condition of the shell.
    """
    words = command.words
    directory = command.directory
    position = 0
    while posix and position < len(words) and words[position].assignment:
        position += 1
    while position < len(words) and not words[position].dynamic:
        name = _program_name(words[position].text, windows=windows)
        if name not in _WRAPPER_FLAGS:
            break
        skipped = _skip_wrapper(name, words, position + 1, directory, line)
        if skipped is None:
            return None
        position, directory = skipped
    if position < len(words) and not words[position].dynamic:
        if _program_name(words[position].text, windows=windows) in LAUNCHERS:
            position += 1
    if position >= len(words):
        return None
    program = words[position]
    if program.dynamic:
        raise RuntimeError(
            f"make printed a command whose program the shell computes ({program.text}): fw-context cannot "
            f"tell if it compiles: {line!r}"
        )
    pure = PureWindowsPath if windows else PurePosixPath
    if not is_compiler_driver_name(pure(program.text.lower() if windows else program.text)):
        return None
    if command.conditional:
        raise RuntimeError(
            "make printed a compile that the shell runs only under a condition (after ||, in an if or in a "
            f"loop), thus fw-context cannot tell if the build compiles it: {line!r}"
        )
    if directory is None:
        raise RuntimeError(f"make printed a compile in a directory that the shell computes: {line!r}")
    computed = [word.text for word in words[position:] if word.dynamic]
    if computed:
        raise RuntimeError(
            f"make printed a compile with words that the shell computes ({', '.join(computed)}): {line!r}"
        )
    arguments = tuple(word.text for word in words[position:])
    return CompileCommand(directory, arguments, tuple(_source_positions(arguments, pure)))


def _skip_wrapper(
    name: str, words: tuple[Word, ...], position: int, directory: Path | None, line: str,
) -> tuple[int, Path | None] | None:
    """Skip the options of the wrapper *name* from *position*; return the position of its command and its directory.

    None: the wrapper runs no command (``command -v``).
    """
    flags = _WRAPPER_FLAGS[name]
    valued = _WRAPPER_VALUES.get(name, frozenset())
    while position < len(words):
        word = words[position]
        if word.dynamic:
            raise RuntimeError(f"make printed {name} with a word that the shell computes ({word.text}): {line!r}")
        text = word.text
        if text == "--":
            position += 1
            break
        if not text.startswith("-") or (text == "-" and "-" not in flags):
            break
        if name == "command" and text in ("-v", "-V"):
            return None
        position += 1
        if text in flags or (name == "nice" and re.fullmatch(r"-\d+", text)):
            continue
        if text.startswith("--") and text.split("=", 1)[0] in valued and "=" in text:
            option, value = text.split("=", 1)
        elif not text.startswith("--") and text[:2] in valued and len(text) > 2:
            option, value = text[:2], text[2:]
        elif text in valued and position < len(words) and not words[position].dynamic:
            option, value = text, words[position].text
            position += 1
        else:
            raise RuntimeError(
                f"make printed {name} with the option {text}, which fw-context does not know: it cannot tell "
                f"the command that {name} runs: {line!r}"
            )
        if option in ("-C", "--chdir"):
            directory = None if directory is None else _normal(directory / value)
    if name == "env":
        while position < len(words) and "=" in words[position].text:
            position += 1
    elif name == "timeout":
        position += 1
    return position, directory


def _source_positions(command: tuple[str, ...], pure: type[PurePath]) -> list[int]:
    """Return the position of each source of the compile *command* (``SOURCE_SUFFIXES``; no value of an option)."""
    positions: list[int] = []
    index = 1
    while index < len(command):
        word = command[index]
        if word in DRIVER_TAKES_NEXT:
            index += 2
            continue
        if not word.startswith("-") and pure(word).suffix in SOURCE_SUFFIXES:
            positions.append(index)
        index += 1
    return positions


def _program_name(path: str, *, windows: bool) -> str:
    """Return the name of the program *path* without its directory and ``.exe``; on Windows in lower case."""
    if windows:
        name = PureWindowsPath(path).name.lower()
        return name[:-4] if name.endswith(".exe") else name
    return PurePosixPath(path).name


def _normal(path: Path) -> Path:
    """Return *path* with ``.`` and ``..`` removed by text, as ``cd`` of the shell reads a path."""
    return Path(os.path.normpath(path))


def compile_entries(commands: Iterable[CompileCommand]) -> list[dict[str, object]]:
    """Return the entries of ``compile_commands.json`` for *commands*: one for each source that exists.

    A file that more than one compile names keeps the entry of the last.
    """
    entries: dict[Path, dict[str, object]] = {}
    for command in commands:
        for source in command.sources:
            file = command.arguments[source]
            path = _normal(command.directory / file)
            if not path.is_file():
                continue
            others = {other for other in command.sources if other != source}
            entries[path.resolve()] = {
                "directory": str(command.directory),
                "file": file,
                "arguments": [word for index, word in enumerate(command.arguments) if index not in others],
            }
    return list(entries.values())


def outputs_outside(commands: Iterable[CompileCommand], directory: Path) -> list[Path]:
    """Return each file that *commands* write outside *directory*."""
    inside = os.path.normcase(os.path.normpath(directory))
    outside: list[Path] = []
    for command in commands:
        for output in command.outputs():
            normal = os.path.normcase(os.path.normpath(output))
            try:
                common = os.path.commonpath([inside, normal])
            except ValueError:
                # Two drives of Windows have no common path.
                common = ""
            if common != inside:
                outside.append(output)
    return outside
