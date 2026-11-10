"""The output of a build is UTF-8, and a Python tool of the build writes UTF-8 on Windows.

``run_in_process_group`` decoded the output with ``text=True``: in the
locale, thus the ANSI code page of Windows, and strict.  A byte that was no
UTF-8 stopped the read on Linux with UnicodeDecodeError, and on Windows the
decode in a reader thread of ``communicate`` left an IndexError and no
output.  A Python tool of the build (pio, west, idf.py) wrote the ANSI code
page to the pipe, and stopped on a character that the code page does not
hold (measured on Windows 11).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from fw_context_mcp import utils


def _writes(data: bytes) -> list[str]:
    """A command that writes the bytes *data* to stdout."""
    return [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({data!r})"]


def test_a_byte_that_is_no_utf8_is_replaced_by_default(tmp_path: Path):
    result = utils.run_in_process_group(
        _writes(b"ok \xff end"), cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=True,
    )

    assert result.stdout == "ok \ufffd end"


def test_utf8_output_keeps_its_characters(tmp_path: Path):
    """Windows decoded it in the ANSI code page: "ž" became two other characters."""
    result = utils.run_in_process_group(
        _writes("C:\\Users\\Jiří\\měření".encode()), cwd=tmp_path, env=dict(os.environ), timeout=60.0,
        capture_output=True,
    )

    assert result.stdout == "C:\\Users\\Jiří\\měření"


def test_a_line_end_of_windows_is_a_newline(tmp_path: Path):
    """As ``text=True`` gave it: the callers read lines that end with "\\n"."""
    result = utils.run_in_process_group(
        _writes(b"one\r\ntwo\r\n"), cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=True,
    )

    assert result.stdout == "one\ntwo\n"


def test_strict_output_that_is_no_utf8_is_an_error(tmp_path: Path):
    """A caller that parses the output must not read a name with U+FFFD in it."""
    with pytest.raises(UnicodeDecodeError):
        utils.run_in_process_group(
            _writes(b"\xff"), cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=True,
            errors="strict",
        )


def test_strict_decodes_stderr_with_replacement(tmp_path: Path):
    """stderr holds messages for a person: a warning of the tool must not stop a parse of its stdout."""
    command = [sys.executable, "-c", "import sys; sys.stdout.write('[]'); sys.stderr.buffer.write(b'warn \\xff')"]

    result = utils.run_in_process_group(
        command, cwd=tmp_path, env=dict(os.environ), timeout=60.0, capture_output=True, errors="strict",
    )

    assert (result.stdout, result.stderr) == ("[]", "warn \ufffd")


def test_run_build_command_names_the_command_of_output_that_is_no_utf8(tmp_path: Path):
    with pytest.raises(RuntimeError, match="not UTF-8: probe"):
        utils.run_build_command(_writes(b"\xff"), cwd=tmp_path, description="probe", errors="strict")


def test_the_output_of_a_timeout_is_text(tmp_path: Path):
    """The output so far can end in the middle of a character, thus it is decoded with replacement."""
    command = [
        sys.executable, "-c",
        "import sys, time; sys.stdout.buffer.write(b'half \\xc5'); sys.stdout.flush(); time.sleep(60)",
    ]

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        utils.run_in_process_group(command, cwd=tmp_path, env=dict(os.environ), timeout=3.0, capture_output=True)

    assert caught.value.output == "half \ufffd"


@pytest.mark.parametrize(("inherited", "expected"), [(None, "utf-8"), ("cp850", "cp850")])
def test_a_python_tool_of_a_windows_build_writes_utf8(monkeypatch, inherited: str | None, expected: str):
    """The environment of the user wins: fw-context sets only a value that is not there."""
    monkeypatch.setattr(sys, "platform", "win32")
    if inherited is None:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    else:
        monkeypatch.setenv("PYTHONIOENCODING", inherited)

    assert utils.build_env()["PYTHONIOENCODING"] == expected


def test_posix_builds_get_no_python_encoding(monkeypatch):
    """Linux and macOS write UTF-8 already: their environment does not change."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    assert "PYTHONIOENCODING" not in utils.build_env()


@pytest.mark.skipif(sys.platform != "win32", reason="the ANSI code page of Windows")
def test_a_python_tool_of_a_windows_build_writes_a_character_outside_the_code_page(tmp_path: Path):
    """Measured: without PYTHONIOENCODING the tool stopped with UnicodeEncodeError and wrote nothing."""
    result = utils.run_in_process_group(
        [sys.executable, "-c", "print('ť \u2013 \u03b1')"], cwd=tmp_path, env=utils.build_env(), timeout=60.0,
        capture_output=True,
    )

    assert (result.returncode, result.stdout.strip()) == (0, "ť \u2013 \u03b1")
