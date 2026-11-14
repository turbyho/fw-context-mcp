"""The shell of a recipe line of make (``indexer/builders/_recipe_shell.py``).

The rules come from ``construct_command_argv_internal`` in ``job.c`` of
GNU make 4.4.1 (the fast path of make on Windows, the batch file of
cmd.exe), from XCU 2 of POSIX (the shell), and from cmd.exe.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.builders._recipe_shell import (
    Unreadable,
    cmd_commands,
    make_fast_words,
    posix_commands,
    recipe_commands,
)


def _words(commands) -> list[list[str]]:
    return [[word.text for word in command.words] for command in commands]


# ── The lines of a recipe line ──────────────────────────────────────────


def test_a_recipe_line_splits_at_a_newline_without_a_backslash():
    assert recipe_commands("a\nb \\\nc\nd") == ["a", "b \\\nc", "d"]


# ── The fast path of make on Windows ────────────────────────────────────


@pytest.mark.parametrize(("line", "words"), [
    ("gcc -c main.c", ["gcc", "-c", "main.c"]),
    ("  gcc   -c\tmain.c  ", ["gcc", "-c", "main.c"]),
    ("gcc '-DA=1 2' -c main.c", ["gcc", "-DA=1 2", "-c", "main.c"]),
    ('gcc "-DA=1 2" -c main.c', ["gcc", "-DA=1 2", "-c", "main.c"]),
    ('gcc "-DSTR=\\"x\\"" -c main.c', ["gcc", '-DSTR="x"', "-c", "main.c"]),
    ("gcc -IC:\\sdk\\inc -c main.c", ["gcc", "-IC:\\sdk\\inc", "-c", "main.c"]),
    ("gcc -DA=\\\\x -c main.c", ["gcc", "-DA=\\x", "-c", "main.c"]),
    ('gcc "" -c', ["gcc", "", "-c"]),
    ("gcc -c \\\n   main.c", ["gcc", "-c", "main.c"]),
    ("gcc a\\ b", ["gcc", "a\\", "b"]),
    ("GCC -DX=1 -c main.c", ["GCC", "-DX=1", "-c", "main.c"]),
    ("", []),
])
def test_make_splits_a_line_for_cmd_itself(line: str, words: list[str]):
    assert make_fast_words(line, posix_shell=False) == words


@pytest.mark.parametrize("line", [
    "gcc -c main.c && echo",
    "gcc -c main.c > log",
    "gcc -c main.c | tee",
    "cd sub",
    "CD sub",
    "echo x",
    "A=1 gcc -c main.c",
    'gcc "-DX=*" -c main.c',
    "gcc '-DA -c main.c",
])
def test_make_gives_a_line_to_cmd(line: str):
    assert make_fast_words(line, posix_shell=False) is None


@pytest.mark.parametrize(("line", "words"), [
    ("gcc -c main.c", ["gcc", "-c", "main.c"]),
    ("gcc '-DA=1 2' -c main.c", ["gcc", "-DA=1 2", "-c", "main.c"]),
    ("gcc -IC:\\sdk\\inc", ["gcc", "-IC:\\sdk\\inc"]),
    ("gcc a\\ b", ["gcc", "a b"]),
    ("gcc -DA=1", ["gcc", "-DA=1"]),
])
def test_make_splits_a_line_for_sh_itself(line: str, words: list[str]):
    assert make_fast_words(line, posix_shell=True) == words


@pytest.mark.parametrize("line", [
    "gcc -c main.c; echo",
    'gcc "-DA=1" -c main.c',
    "gcc $X",
    "A=1 gcc -c main.c",
    "cd sub",
    "test -f x",
])
def test_make_gives_a_line_to_sh(line: str):
    assert make_fast_words(line, posix_shell=True) is None


# ── POSIX shell ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("line", "words"), [
    ("gcc -c main.c", [["gcc", "-c", "main.c"]]),
    ("gcc 'a b' \"c d\" e\\ f", [["gcc", "a b", "c d", "e f"]]),
    ("gcc \"a\\\"b\" 'c\\d'", [["gcc", 'a"b', "c\\d"]]),
    ("a && b || c; d & e | f", [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]),
    ("gcc -c x.c 2>&1 >out <in 3>>log", [["gcc", "-c", "x.c"]]),
    ("gcc -c x.c # a comment with it's quote", [["gcc", "-c", "x.c"]]),
    ("gcc a#b", [["gcc", "a#b"]]),
    ("gcc \\\n  -c x.c", [["gcc", "-c", "x.c"]]),
    ("(a; b) && { c; }", [["a"], ["b"], ["c"]]),
    ("if a; then b; elif c; then d; else e; fi", [["a"], ["b"], ["c"], ["d"], ["e"]]),
    ("for f in x y; do a; done", [["a"]]),
    ("while a; do b; done; until c; do d; done", [["a"], ["b"], ["c"], ["d"]]),
    ("! a | b", [["a"], ["b"]]),
    ("A=1 B='x y' gcc", [["A=1", "B=x y", "gcc"]]),
    ("echo if then fi", [["echo", "if", "then", "fi"]]),
    ("'if' x", [["if", "x"]]),
    ("", []),
])
def test_the_commands_of_a_posix_line(tmp_path: Path, line: str, words: list[list[str]]):
    assert _words(posix_commands(line, tmp_path, cdpath=False)) == words


@pytest.mark.parametrize(("line", "conditional"), [
    ("a && b", [False, False]),
    ("a || b", [False, True]),
    ("a || b && c", [False, True, False]),
    ("if a; then b; else c; fi", [False, True, True]),
    ("while a; do b; done", [False, True]),
    ("for x in y; do a; done", [True]),
    ("(a || b)", [False, True]),
])
def test_a_command_that_a_choice_of_the_shell_selects_is_conditional(tmp_path: Path, line: str, conditional):
    assert [command.conditional for command in posix_commands(line, tmp_path, cdpath=False)] == conditional


@pytest.mark.parametrize(("word", "dynamic"), [
    ("plain", False),
    ("'$x'", False),
    ("$x", True),
    ("${x}", True),
    ('"$x"', True),
    ("$(cmd)", True),
    ("`cmd`", True),
    ("*.c", True),
    ("'*.c'", False),
    ("a?b", True),
    ("[ab]", True),
    ("~/x", True),
    ("a~b", False),
    ("a{b,c}", True),
    ("a{1..3}", True),
    ("{}", False),
    ("a$", False),
    ("$'x'", True),
])
def test_a_word_that_the_shell_computes(tmp_path: Path, word: str, dynamic: bool):
    (command,) = posix_commands(f"echo {word}", tmp_path, cdpath=False)

    assert command.words[1].dynamic is dynamic


def test_an_assignment_needs_a_name_without_a_quote(tmp_path: Path):
    (command,) = posix_commands("A=1 'B=2' C='3' gcc", tmp_path, cdpath=False)

    assert [word.assignment for word in command.words] == [True, False, True, False]


def test_cd_and_its_scope(tmp_path: Path):
    commands = posix_commands("cd a && x; (cd b; y); z; cd ../c | w; v", tmp_path, cdpath=False)

    assert [(command.words[0].text, command.directory) for command in commands] == [
        ("x", tmp_path / "a"), ("y", tmp_path / "a" / "b"), ("z", tmp_path / "a"), ("w", tmp_path / "a"),
        ("v", tmp_path / "a"),
    ]


@pytest.mark.parametrize("line", ["cd $d; x", "cd; x", "cd -; x", "pushd a; x", "test -d a || cd a; x", "cd a b; x"])
def test_a_cd_that_the_reader_cannot_tell(tmp_path: Path, line: str):
    assert posix_commands(line, tmp_path, cdpath=False)[-1].directory is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows removes .. from a path by text before it follows a link (measured on Windows 11)",
)
def test_cd_physical_follows_a_link(tmp_path: Path):
    (tmp_path / "real" / "deep").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real" / "deep")

    logical = posix_commands("cd link/.. ; x", tmp_path, cdpath=False)[0].directory
    physical = posix_commands("cd -P link/.. ; x", tmp_path, cdpath=False)[0].directory

    assert logical == tmp_path
    assert physical == (tmp_path / "real").resolve()


@pytest.mark.parametrize("line", [
    "echo 'x",
    'echo "x',
    "echo $(x",
    "echo `x",
    "case x in a) b;; esac",
    "f() { x; }",
    "cat <<EOF",
    "then x",
    "if a; then b",
    "( a",
    "a )",
    "a && ",
    "> ",
])
def test_a_line_out_of_the_grammar(tmp_path: Path, line: str):
    with pytest.raises(Unreadable):
        posix_commands(line, tmp_path, cdpath=False)


# ── cmd.exe ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("line", "words"), [
    ("gcc -c main.c", [["gcc", "-c", "main.c"]]),
    ('gcc "-DA=1 2" -c', [["gcc", "-DA=1 2", "-c"]]),
    ("gcc '-DA=1 2'", [["gcc", "'-DA=1", "2'"]]),
    ("a & b && c || d | e", [["a"], ["b"], ["c"], ["d"], ["e"]]),
    ("gcc -c x.c 2>&1 >out.txt <in.txt 2> err", [["gcc", "-c", "x.c"]]),
    ("gcc -c x.c >> \"log file\"", [["gcc", "-c", "x.c"]]),
    ("echo a^&b", [["echo", "a&b"]]),
    ('echo "a&b"', [["echo", "a&b"]]),
    ("@gcc -c x.c", [["gcc", "-c", "x.c"]]),
    ("(a & b) && c", [["a"], ["b"], ["c"]]),
    ("rem a & b", []),
    (":: a & b", []),
    ("a & rem b", [["a"]]),
    ("gcc -c \\\nx.c", [["gcc", "-c", "x.c"]]),
])
def test_the_commands_of_a_cmd_line(tmp_path: Path, line: str, words: list[list[str]]):
    assert _words(cmd_commands(line, tmp_path)) == words


def test_cmd_conditions_and_a_percent(tmp_path: Path):
    commands = cmd_commands("a || b & c %X%", tmp_path)

    assert [command.conditional for command in commands] == [False, True, False]
    assert [word.dynamic for word in commands[2].words] == [False, True]


@pytest.mark.parametrize("line", ["if exist x gcc -c x.c", "for %f in (*.c) do gcc", "start gcc", "(a", "a & (b"])
def test_a_cmd_line_out_of_the_grammar(tmp_path: Path, line: str):
    with pytest.raises(Unreadable):
        cmd_commands(line, tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="the drives of cmd.exe need the paths of Windows")
@pytest.mark.parametrize(("line", "expected"), [
    ("cd sub & x", "sub"),
    ("cd sub\\deep & x", "sub\\deep"),
    ('cd "my dir" & x', "my dir"),
    ("cd.. & x", ".."),
    ("cd /d sub & x", "sub"),
    ("(cd sub) & x", "sub"),
    ("cd sub | more & x", ""),
])
def test_cd_of_cmd(tmp_path: Path, line: str, expected: str):
    import os

    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "my dir").mkdir()

    assert cmd_commands(line, tmp_path)[-1].directory == Path(os.path.normpath(tmp_path / expected))


@pytest.mark.skipif(sys.platform != "win32", reason="the drives of cmd.exe need the paths of Windows")
def test_cd_of_cmd_to_an_other_drive(tmp_path: Path):
    from pathlib import PureWindowsPath

    other = "Z:" if PureWindowsPath(tmp_path).drive.upper() != "Z:" else "Y:"

    assert cmd_commands(f"cd {other}\\x & a", tmp_path)[-1].directory == tmp_path, "cd without /d keeps the drive"
    assert cmd_commands(f"cd /d {other}\\x & a", tmp_path)[-1].directory == Path(f"{other}\\x")
    assert cmd_commands(f"cd {other}x & a", tmp_path)[-1].directory is None
