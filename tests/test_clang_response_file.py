"""The flags of a unit go to clang in a response file (``@file``).

Windows limits a command line to 32767 characters.  The flags of one unit
of an ESP32 project were 32798 characters long: CreateProcess failed with
error 206, Python raised FileNotFoundError, and ``clang -dM -E`` of each
such unit was skipped as if clang were missing.  The expanded values of
the macros were then empty on Windows only.  The preprocessor of the
assembly units ran the same command line.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp import utils
from fw_context_mcp.indexer import _clang_cli, asm, macros
from fw_context_mcp.utils import CLANG_POSIX_RESPONSE_FILE, response_file, response_file_text

_HAS_CLANG = shutil.which("clang") is not None

# More than 32767 characters of flags, as the include paths of a large unit.
_LONG_FLAGS = [f"-I{'d' * 200}/include_{index}" for index in range(200)]


@pytest.mark.parametrize(
    ("argument", "line"),
    [
        pytest.param("-DPLAIN=1", "-DPLAIN=1", id="plain"),
        pytest.param("-I/dir with space", r"-I/dir\ with\ space", id="space"),
        pytest.param('-DQ="a b"', r"-DQ=\"a\ b\"", id="double-quotes"),
        pytest.param("-DS='x'", r"-DS=\'x\'", id="single-quotes"),
        pytest.param(r"-IC:\x\y", r"-IC:\\x\\y", id="backslash"),
        pytest.param("-Da\tb", "-Da\\\tb", id="tab"),
        pytest.param("", '""', id="empty"),
    ],
)
def test_each_special_character_gets_a_backslash(argument: str, line: str):
    assert response_file_text([argument]) == line + "\n"


def test_the_response_file_is_removed_after_the_block():
    with response_file(["-DA=1"]) as path:
        assert path.read_bytes() == b"-DA=1\n"
    assert not path.exists()


def test_a_line_break_in_an_argument_stays_one_argument_on_each_host():
    """The text mode of Windows would write \\r\\n, and the escaped \\n would then end the argument."""
    with response_file(["-DA=x\ny"]) as path:
        assert path.read_bytes() == b"-DA=x\\\ny\n"


def test_a_file_that_cannot_be_removed_keeps_the_result(monkeypatch, caplog):
    """A scanner of Windows can hold the new file: the result of the block stays, with a warning."""
    def locked(path):
        raise PermissionError(13, "the file is open in another process")

    monkeypatch.setattr(utils, "remove_file", locked)

    with response_file(["-DA=1"]) as path:
        result = path.read_text(encoding="utf-8")

    assert result == "-DA=1\n"
    assert "stays in the temporary directory" in caplog.text
    path.unlink()


@pytest.mark.skipif(not _HAS_CLANG, reason="the reader of the response file is clang")
def test_clang_reads_each_argument_back(tmp_path: Path):
    """The contract on each host: clang with --rsp-quoting=posix reads the file as written."""
    include = tmp_path / "dir with space"
    include.mkdir()
    (include / "h.h").write_text("#define FROM_HEADER 7\n", encoding="utf-8")
    source = tmp_path / "t.c"
    source.write_text('#include "h.h"\n', encoding="utf-8")

    with response_file(["-dM", "-E", f"-I{include}", '-DQ="a b"', r"-DBS=C:\x\y", str(source)]) as args_file:
        result = subprocess.run(
            ["clang", CLANG_POSIX_RESPONSE_FILE, f"@{args_file}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
        )

    defines = set(result.stdout.splitlines())
    assert {"#define FROM_HEADER 7", '#define Q "a b"', r"#define BS C:\x\y"} <= defines


def test_the_macro_expansion_gives_clang_a_short_command_line(tmp_path: Path, monkeypatch):
    source = tmp_path / "a.c"
    source.write_text("#define A 1\n", encoding="utf-8")
    seen: list[tuple[list[str], str]] = []

    def run(cmd, **kwargs):
        seen.append((cmd, Path(cmd[-1][1:]).read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(cmd, 0, b"#define A 1\n", b"")

    monkeypatch.setattr(_clang_cli.subprocess, "run", run)

    assert macros.resolve_macros_via_preprocessor(_LONG_FLAGS, source, "clang", cwd=tmp_path) == {"A": "1"}
    [(cmd, text)] = seen
    assert cmd[:2] == ["clang", CLANG_POSIX_RESPONSE_FILE]
    assert len(cmd) == 3
    assert text.splitlines()[:2] == ["-dM", "-E"]
    assert f"-I{'d' * 200}/include_199" in text.splitlines()


def test_the_assembly_preprocessor_gives_clang_a_short_command_line(tmp_path: Path, monkeypatch):
    source = tmp_path / "start.S"
    source.write_text("nop\n", encoding="utf-8")
    unit = SimpleNamespace(file=source, directory=tmp_path, clang_args=list(_LONG_FLAGS))
    seen: list[list[str]] = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, b"", b"stop here")

    monkeypatch.setattr(_clang_cli.subprocess, "run", run)
    monkeypatch.setattr(asm, "clang_binary", lambda: "clang")

    assert asm.preprocess(unit) is None
    [cmd] = seen
    assert cmd[:2] == ["clang", CLANG_POSIX_RESPONSE_FILE]
    assert len(cmd) == 3


@pytest.mark.skipif(not _HAS_CLANG, reason="the preprocessor is clang")
def test_a_unit_with_flags_longer_than_a_windows_command_line_gets_its_macros(tmp_path: Path):
    source = tmp_path / "a.c"
    source.write_text("#define TEMP_HYSTERESIS 0.5f\n", encoding="utf-8")
    flags = [*_LONG_FLAGS, "-DFROM_FLAGS=3"]
    assert sum(len(flag) + 1 for flag in flags) > 32767

    expanded = macros.resolve_macros_via_preprocessor(flags, source, cwd=tmp_path)

    assert expanded["TEMP_HYSTERESIS"] == "0.5f"
    assert expanded["FROM_FLAGS"] == "3"


@pytest.mark.skipif(not _HAS_CLANG, reason="the preprocessor is clang")
def test_an_assembly_unit_with_flags_longer_than_a_windows_command_line_is_read(tmp_path: Path):
    source = tmp_path / "start.S"
    source.write_text("#define ANSWER 42\n    .word ANSWER\n", encoding="utf-8")
    unit = SimpleNamespace(file=source, directory=tmp_path, clang_args=list(_LONG_FLAGS))

    result = asm.preprocess(unit)

    assert result is not None
