"""The reader of the log of ``make -Bnkwp`` (``indexer/builders/_make_log.py``).

compiledb read this log as bash: on Windows a backslash of a path was an
escape, and a compiler with ``.exe`` was no compiler, thus the database of
a Windows build was empty (measured 2026-11-13).  The reader splits each
command by the rules of the shell that make uses, which the data base of
each make gives, and knows a compiler by the name grammar of the driver
query.

The logs here have the form that GNU make 4.4.1 prints with ``-p``
(measured): ``# make: Entering directory '<dir>'``, the commands, the data
base, ``# make: Leaving directory '<dir>'``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from fw_context_mcp.indexer.builders._make_log import (
    CompileCommand,
    compile_entries,
    outputs_outside,
    read_log,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A project with sources in its root, in src/, in sub/ and in asm/."""
    for name in ("main.c", "src/a.c", "src/b.c", "sub/c.c", "asm/start.S", "src/x.cpp"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


def _level(
    directory: Path, lines: list[str], *, shell: str = "/bin/sh", variables: str = "", files: str = "",
    patterns: str = "", depth: int = 0,
) -> str:
    """The log of one make in *directory*: its lines, then its data base, as make -Bnkwp prints them."""
    name = "make" if depth == 0 else f"make[{depth}]"
    return "\n".join([
        f"# {name}: Entering directory '{directory}'",
        *lines,
        "",
        "# Make data base, printed on Fri Nov 13 23:40:08 2026",
        "",
        "# Variables",
        "",
        "# default",
        f"SHELL = {shell}",
        variables,
        "",
        "# Pattern-specific Variable Values",
        patterns,
        "",
        "# Files",
        files,
        "",
        "# Finished Make data base on Fri Nov 13 23:40:08 2026",
        "",
        f"# {name}: Leaving directory '{directory}'",
    ])


def _entries(log: str, *, windows: bool = False) -> list[tuple[str, str, list[str]]]:
    """The entries of *log*: the name of the directory, the file and the arguments of each."""
    return [
        (Path(str(e["directory"])).name, str(e["file"]), cast(list[str], e["arguments"]))
        for e in compile_entries(read_log(log, windows=windows))
    ]


def _files(log: str, *, windows: bool = False) -> list[str]:
    return [file for _, file, _ in _entries(log, windows=windows)]


# ── Compile commands ────────────────────────────────────────────────────


def test_a_compile_command_is_an_entry(tree: Path):
    entries = compile_entries(read_log(_level(tree, ["arm-none-eabi-gcc -mthumb -c main.c -o main.o"]), windows=False))

    assert entries == [{
        "directory": str(tree),
        "file": "main.c",
        "arguments": ["arm-none-eabi-gcc", "-mthumb", "-c", "main.c", "-o", "main.o"],
    }]


@pytest.mark.parametrize("line", [
    "echo gcc -c main.c",
    "arm-none-eabi-ar rcs libx.a a.o",
    "make -C sub",
    "arm-none-eabi-gcc -T app.ld a.o -o app.elf",
    "mkdir -p build",
])
def test_a_line_that_compiles_nothing_is_no_entry(tree: Path, line: str):
    assert _files(_level(tree, [line])) == []


@pytest.mark.parametrize("compiler", [
    "gcc", "g++", "cc", "c++", "clang", "clang++", "arm-none-eabi-gcc", "gcc-12", "riscv32-esp-elf-g++",
])
def test_each_compiler_driver(tree: Path, compiler: str):
    assert _files(_level(tree, [f"{compiler} -c src/x.cpp"])) == ["src/x.cpp"]


def test_each_source_of_a_command_is_an_entry_without_the_others(tree: Path):
    entries = _entries(_level(tree, ["gcc -O2 -c src/a.c src/b.c -I inc"]))

    assert entries == [
        (tree.name, "src/a.c", ["gcc", "-O2", "-c", "src/a.c", "-I", "inc"]),
        (tree.name, "src/b.c", ["gcc", "-O2", "-c", "src/b.c", "-I", "inc"]),
    ]


def test_the_value_of_an_option_is_no_source(tree: Path):
    assert _files(_level(tree, ["gcc -include main.c -c src/a.c"])) == ["src/a.c"]


def test_a_source_that_is_not_there_is_no_entry(tree: Path):
    assert _files(_level(tree, ["gcc -c generated.c"])) == []


def test_a_file_compiled_twice_keeps_its_last_compile(tree: Path):
    entries = _entries(_level(tree, ["gcc -DFIRST -c main.c", "gcc -DSECOND -c main.c"]))

    assert entries == [(tree.name, "main.c", ["gcc", "-DSECOND", "-c", "main.c"])]


# ── Directories ─────────────────────────────────────────────────────────


def test_the_directories_of_a_recursive_make(tree: Path):
    """The data base of a sub-make comes before its Leaving line, and the parent goes on after it."""
    log = _level(tree, [
        "gcc -DROOT -c main.c",
        "make -C sub",
        _level(tree / "sub", ["gcc -c c.c"], depth=1),
        "gcc -DAFTER -c main.c",
    ])

    assert [(d, f) for d, f, _ in _entries(log)] == [(tree.name, "main.c"), ("sub", "c.c")]
    assert _entries(log)[0][2] == ["gcc", "-DAFTER", "-c", "main.c"], "the last compile of the log wins"


def test_cd_changes_the_directory_of_the_commands_after_it_on_its_line(tree: Path):
    log = _level(tree, ["cd sub && gcc -c c.c", "gcc -c main.c"])

    assert [(d, f) for d, f, _ in _entries(log)] == [("sub", "c.c"), (tree.name, "main.c")]


@pytest.mark.parametrize("line", [
    "(cd sub && gcc -c c.c); gcc -c main.c",
    "cd sub | cat; gcc -c main.c",
    "cd sub & gcc -c main.c",
])
def test_a_cd_in_a_subshell_stays_in_it(tree: Path, line: str):
    assert "main.c" in _files(_level(tree, [line]))


def test_a_directory_that_the_shell_computes_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="directory that the shell computes"):
        read_log(_level(tree, ["cd $d && gcc -c c.c"]), windows=False)


def test_cdpath_makes_a_relative_cd_unknown(tree: Path):
    """POSIX cd looks a relative name up in CDPATH first."""
    log = _level(tree, ["cd sub && gcc -c c.c"], variables="# environment\nCDPATH = /opt")

    with pytest.raises(RuntimeError, match="directory that the shell computes"):
        read_log(log, windows=False)
    assert _files(_level(tree, ["cd ./sub && gcc -c c.c"], variables="# environment\nCDPATH = /opt")) == ["c.c"]


def test_env_chdir_runs_the_command_in_its_directory(tree: Path):
    assert _entries(_level(tree, ["env -C sub gcc -c c.c"]))[0][:2] == ("sub", "c.c")


# ── The words of a command ──────────────────────────────────────────────


@pytest.mark.parametrize("separator", ["&&", "||", ";", "|", "&"])
def test_each_command_of_a_line(tree: Path, separator: str):
    assert _files(_level(tree, [f"gcc -c main.c {separator} echo done"])) == ["main.c"]


def test_a_separator_in_quotes_is_no_separator(tree: Path):
    entries = _entries(_level(tree, ["gcc '-DTEXT=a && b; c' -c main.c"]))

    assert entries[0][2] == ["gcc", "-DTEXT=a && b; c", "-c", "main.c"]


@pytest.mark.parametrize("line", [
    "gcc -c main.c 2>&1 | tee build.log",
    "gcc -c main.c > /dev/null 2>&1",
    "gcc -c main.c >&2",
    "gcc -c main.c 2> errors.txt",
])
def test_a_redirection_is_no_argument(tree: Path, line: str):
    assert _entries(_level(tree, [line]))[0][2] == ["gcc", "-c", "main.c"]


def test_a_launcher_and_assignments_before_the_compiler(tree: Path):
    entries = _entries(_level(tree, ["CCACHE_DIR=/tmp/c ccache arm-none-eabi-gcc -c main.c"]))

    assert entries[0][2] == ["arm-none-eabi-gcc", "-c", "main.c"]


@pytest.mark.parametrize("prefix", [
    "env", "env -i PATH=/usr/bin", "env -u LANG", "env --unset=LANG A=1",
    "exec", "command", "command -p",
    "nice", "nice -n 5", "nice -5", "nice --adjustment=5",
    "nohup", "time", "time -p", "timeout 60", "timeout -s KILL 60", "timeout --kill-after=5 60",
    "env nice -n 2 ccache",
])
def test_a_wrapper_before_the_compiler(tree: Path, prefix: str):
    assert _entries(_level(tree, [f"{prefix} gcc -c main.c"]))[0][2] == ["gcc", "-c", "main.c"]


def test_command_v_runs_nothing(tree: Path):
    assert _files(_level(tree, ["command -v gcc main.c"])) == []


def test_an_option_of_a_wrapper_that_the_reader_does_not_know_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="does not know"):
        read_log(_level(tree, ["env -S 'gcc -c' main.c"]), windows=False)


def test_a_line_that_goes_on_after_a_backslash(tree: Path):
    entries = _entries(_level(tree, ["gcc -DA=1 \\", "  -c main.c \\", "-o main.o"]))

    assert entries[0][2] == ["gcc", "-DA=1", "-c", "main.c", "-o", "main.o"]


def test_a_quoted_backslash_and_an_escaped_quote(tree: Path):
    entries = _entries(_level(tree, ['gcc -DSTR=\\"x\\" "-DPATH=a\\\\b" -c main.c']))

    assert entries[0][2] == ["gcc", '-DSTR="x"', "-DPATH=a\\b", "-c", "main.c"]


@pytest.mark.parametrize("line", [
    "gcc -DX=$HOME -c main.c",
    "gcc $(cat flags) -c main.c",
    "gcc `cat flags` -c main.c",
    "gcc -c *.c",
    "gcc -DX=a{b,c} -c main.c",
])
def test_a_compile_with_a_word_that_the_shell_computes_is_an_error(tree: Path, line: str):
    with pytest.raises(RuntimeError, match="words that the shell computes"):
        read_log(_level(tree, [line]), windows=False)


def test_a_program_that_the_shell_computes_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="program the shell computes"):
        read_log(_level(tree, ["$CC -c main.c"]), windows=False)


# ── The flow of a line ──────────────────────────────────────────────────


@pytest.mark.parametrize("line", [
    "mkdir -p build && gcc -c main.c",
    "if gcc -c main.c; then echo ok; fi",
    "{ gcc -c main.c; }",
    "! gcc -c main.c",
    "gcc -c main.c || exit 1",
])
def test_a_compile_that_runs_when_each_command_succeeds(tree: Path, line: str):
    assert _files(_level(tree, [line])) == ["main.c"]


@pytest.mark.parametrize("line", [
    "test -f main.o || gcc -c main.c",
    "if test -f x; then gcc -c main.c; fi",
    "if test -f x; then true; else gcc -c main.c; fi",
    "for f in a b; do gcc -c main.c; done",
    "while false; do gcc -c main.c; done",
])
def test_a_compile_under_a_condition_of_the_shell_is_an_error(tree: Path, line: str):
    with pytest.raises(RuntimeError, match="only under a condition"):
        read_log(_level(tree, [line]), windows=False)


# ── Lines that are no commands ──────────────────────────────────────────


@pytest.mark.parametrize("line", [
    "# don't panic: a comment of the shell",
    "Don't panic: the text of $(info)",
    "make: Nothing to be done for `all'.",
    "make[1]: 'all' is up to date.",
    "case x in a) echo a;; esac",
])
def test_a_line_that_is_no_compile(tree: Path, line: str):
    assert _files(_level(tree, [line, "gcc -c main.c"])) == ["main.c"]


@pytest.mark.parametrize("line", [
    "gcc -DNAME='x -c main.c",
    "case x in a) gcc -c main.c;; esac",
    "gcc -c main.c <<EOF",
])
def test_an_unreadable_line_that_names_a_compiler_is_an_error(tree: Path, line: str):
    with pytest.raises(RuntimeError, match="cannot read"):
        read_log(_level(tree, [line]), windows=False)


# ── The shell of a make ─────────────────────────────────────────────────


@pytest.mark.parametrize("shell", ["/bin/bash", "/usr/bin/dash", "bash", "/bin/sh -e"])
def test_a_posix_shell(tree: Path, shell: str):
    assert _files(_level(tree, ["gcc -c main.c"], shell=shell)) == ["main.c"]


def test_a_shell_that_is_no_posix_shell_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="POSIX shell"):
        read_log(_level(tree, ["gcc -c main.c"], shell="/usr/bin/python3"), windows=False)


def test_a_shell_with_a_reference_is_an_error(tree: Path):
    """make prints a recursive variable as it is: its value is unknown."""
    with pytest.raises(RuntimeError, match="does not expand"):
        read_log(_level(tree, ["gcc -c main.c"], shell="$(BASH)"), windows=False)


def test_a_simple_variable_gets_its_dollars_back(tree: Path):
    log = _level(tree, ["gcc -c main.c"]).replace("SHELL = /bin/sh", "SHELL := /opt/x$$/bash")

    assert _files(log) == ["main.c"]


def test_oneshell_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="ONESHELL"):
        read_log(_level(tree, ["gcc -c main.c"], files=".ONESHELL:"), windows=False)


def test_a_target_with_a_shell_of_an_other_kind_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="target or a pattern"):
        read_log(_level(tree, ["gcc -c main.c"], files="t: SHELL = /usr/bin/python3"), windows=False)
    with pytest.raises(RuntimeError, match="target or a pattern"):
        read_log(_level(tree, ["gcc -c main.c"], patterns="%.o :\n# makefile\n# SHELL := /usr/bin/python3"),
                 windows=False)


def test_a_target_with_a_posix_shell_of_its_own(tree: Path):
    assert _files(_level(tree, ["gcc -c main.c"], files="t: SHELL = /bin/bash")) == ["main.c"]


# ── The shell of a make on Windows ──────────────────────────────────────


def test_windows_without_sh_make_splits_a_simple_line_itself(tree: Path):
    """make finds no sh.exe: batch mode.  A line without | & < > runs without cmd.exe, and make reads ' as a quote."""
    log = _level(tree, ["gcc '-DA=1 2' -c main.c"], shell="sh.exe",
                 variables="# environment\nPATH = " + str(tree / "nothing"))

    assert _entries(log, windows=True)[0][2] == ["gcc", "-DA=1 2", "-c", "main.c"]


def test_windows_without_sh_reads_a_line_for_cmd(tree: Path):
    """A line with && goes to cmd.exe: a single quote is no quote there, and a redirection is no argument."""
    log = _level(tree, ["gcc '-DA=1 2' -c main.c 2>&1 && echo x"], shell="sh.exe")

    assert _entries(log, windows=True)[0][2] == ["gcc", "'-DA=1", "2'", "-c", "main.c"]


def test_windows_with_sh_on_path_reads_by_the_rules_of_sh(tree: Path):
    tools = tree / "tools"
    tools.mkdir()
    (tools / "sh.exe").write_bytes(b"")
    log = _level(tree, ["gcc '-DA=1 2' -c main.c && echo x"], shell="sh.exe",
                 variables=f"# environment\nPATH = {tree / 'nothing'};{tools}")

    assert _entries(log, windows=True)[0][2] == ["gcc", "-DA=1 2", "-c", "main.c"]


def test_windows_fast_path_keeps_a_backslash_of_a_path(tree: Path):
    """make runs a line without a special character itself; a backslash before a letter stays."""
    log = _level(tree, ["gcc -Isrc\\inc -c main.c"], shell="sh.exe")

    assert _entries(log, windows=True)[0][2] == ["gcc", "-Isrc\\inc", "-c", "main.c"]


def test_windows_cmd_keeps_a_percent_word_out(tree: Path):
    with pytest.raises(RuntimeError, match="words that the shell computes"):
        read_log(_level(tree, ["gcc -D%X% -c main.c && echo"], shell="cmd.exe"), windows=True)


def test_windows_a_shell_that_is_neither_is_an_error(tree: Path):
    (tree / "pwsh.exe").write_bytes(b"")
    with pytest.raises(RuntimeError, match="POSIX shell"):
        read_log(_level(tree, ["gcc -c main.c"], shell="pwsh.exe"), windows=True)


# ── A log that the reader cannot use ────────────────────────────────────


def test_a_make_without_a_data_base_is_an_error(tree: Path):
    log = f"# make: Entering directory '{tree}'\ngcc -c main.c\n# make: Leaving directory '{tree}'\n"

    with pytest.raises(RuntimeError, match="no data base"):
        read_log(log, windows=False)


def test_a_sub_make_without_its_directory_is_an_error(tree: Path):
    """--no-print-directory: the data base of the sub-make lands in the level of its parent."""
    sub = _level(tree / "sub", ["gcc -c c.c"], depth=1)
    hidden = "\n".join(line for line in sub.splitlines() if "directory" not in line)

    with pytest.raises(RuntimeError, match="no-print-directory"):
        read_log(_level(tree, ["make --no-print-directory -C sub", hidden]), windows=False)


def _restart(tree: Path, first: list[str], second: list[str]) -> str:
    """The log of a make 4 that restarts: the first pass and its data base, then the next pass with MAKE_RESTARTS = 1.

    Measured with GNU make 4.4.1 and ``include $(SRCS:.c=.d)`` with a rule
    for ``%.d``: one Entering line, two data bases, one Leaving line.
    """
    first_pass = _level(tree, first).splitlines()[:-1]
    next_pass = _level(tree, second, variables="MAKE_RESTARTS = 1").splitlines()[1:]
    return "\n".join(first_pass + next_pass)


def test_a_make_that_restarts_gives_the_compiles_of_each_pass(tree: Path):
    """The first pass remakes an included makefile, the next pass makes the goals.

    src/a.c keeps the place of its first compile and the arguments of its last.
    """
    log = _restart(tree, ["gcc -MM src/a.c > src/a.d"], ["gcc -c main.c", "gcc -c src/a.c"])

    assert _entries(log) == [
        (tree.name, "src/a.c", ["gcc", "-c", "src/a.c"]),
        (tree.name, "main.c", ["gcc", "-c", "main.c"]),
    ]


def test_a_make_381_that_restarts_prints_its_directory_again(tree: Path):
    """GNU make 3.81 prints its data base and Leaving before it runs itself again (main.c:1966-1971 of 3.81)."""
    log = "\n".join([
        _level(tree, ["gcc -MM main.c > main.d"]),
        _level(tree, ["gcc -c main.c"], variables="MAKE_RESTARTS = 1"),
    ])

    assert _entries(log) == [(tree.name, "main.c", ["gcc", "-c", "main.c"])]


def test_a_second_data_base_without_a_restart_is_an_error(tree: Path):
    """Two data bases in one level, and the second is not a restart: a sub-make hid its directory."""
    log = "\n".join(_level(tree, ["gcc -c main.c"]).splitlines()[:-1] + _level(tree, ["gcc -c src/a.c"]).splitlines()[1:])

    with pytest.raises(RuntimeError, match="no-print-directory"):
        read_log(log, windows=False)


def test_a_byte_that_is_not_utf8_in_the_data_base_is_no_error(tree: Path):
    """The value of a variable that the reader does not use (decoded with surrogateescape: \\udce8 is the byte 0xE8)."""
    log = _level(tree, ["gcc -c main.c"], variables="# environment\nNAME = Kol\udce8")

    assert _files(log) == ["main.c"]


def test_a_compile_with_a_byte_that_is_not_utf8_is_an_error(tree: Path):
    with pytest.raises(RuntimeError, match="not UTF-8"):
        read_log(_level(tree, ["gcc -DNAME=Kol\udce8 -c main.c"]), windows=False)


def test_mixed_output_of_two_makes_is_an_error(tree: Path):
    log = _level(tree, ["gcc -c main.c"]).replace(f"Leaving directory '{tree}'", f"Leaving directory '{tree / 'sub'}'")

    with pytest.raises(RuntimeError, match="mixed"):
        read_log(log, windows=False)


def test_a_log_that_stops_early_is_an_error(tree: Path):
    log = "\n".join(_level(tree, ["gcc -c main.c"]).splitlines()[:-1])

    with pytest.raises(RuntimeError, match="stops"):
        read_log(log, windows=False)


def test_a_directory_of_an_other_host_is_an_error(tree: Path):
    """A make of MSYS2 prints /c/Users/...: no path of the host."""
    with pytest.raises(RuntimeError, match="MSYS2"):
        read_log(_level(tree / "no-such-directory", ["gcc -c main.c"]), windows=False)


def test_the_output_that_make_381_prints_before_its_directory(tree: Path):
    """GNU make 3.81 (macOS) prints its version and the text of $(info) before its first Entering line (measured)."""
    before = (
        "# GNU Make 3.81\n# Copyright (C) 2006  Free Software Foundation, Inc.\n\n"
        "# This program built for i386-apple-darwin11.3.0\n"
        "Don't panic: the text of $(info)\n"
        "gcc -c main.c\n"
    )
    log = before + _level(tree, ["gcc -DIN_LEVEL -c main.c"]).replace(f"'{tree}'", f"`{tree}'")

    assert _entries(log) == [(tree.name, "main.c", ["gcc", "-DIN_LEVEL", "-c", "main.c"])]


def test_a_log_without_a_directory_is_an_error(tree: Path):
    """--no-print-directory in MAKEFLAGS of the environment: make prints no directory."""
    with pytest.raises(RuntimeError, match="no-print-directory"):
        read_log("gcc -c main.c\n", windows=False)


# ── The files that a compile writes ─────────────────────────────────────


@pytest.mark.parametrize(("arguments", "outputs"), [
    (["gcc", "-c", "a.c", "-o", "out/a.o"], ["out/a.o"]),
    (["gcc", "-c", "a.c", "-oout/a.o"], ["out/a.o"]),
    (["gcc", "-c", "a.c", "--output=out/a.o"], ["out/a.o"]),
    (["gcc", "-c", "src/a.c", "src/b.c"], ["a.o", "b.o"]),
    (["gcc", "-S", "src/a.c"], ["a.s"]),
    (["gcc", "a.o", "b.o"], ["a.out"]),
    (["gcc", "-E", "a.c"], []),
    (["gcc", "-MM", "a.c"], []),
    (["gcc", "-fsyntax-only", "a.c"], []),
    (["gcc", "--version"], []),
    (["gcc", "-I", "inc", "-c", "a.c", "-o", "-"], []),
])
def test_the_outputs_of_a_compile(tmp_path: Path, arguments: list[str], outputs: list[str]):
    sources = tuple(index for index, word in enumerate(arguments) if word.endswith(".c"))
    command = CompileCommand(tmp_path, tuple(arguments), sources)

    assert command.outputs() == [tmp_path / output for output in outputs]


def test_the_outputs_outside_a_directory(tmp_path: Path):
    inside = CompileCommand(tmp_path, ("gcc", "-c", "a.c", "-o", "out/a.o"), (2,))
    outside = CompileCommand(tmp_path, ("gcc", "-c", "a.c", "-o", "build/a.o"), (2,))

    assert outputs_outside([inside, outside], tmp_path / "out") == [tmp_path / "build" / "a.o"]
