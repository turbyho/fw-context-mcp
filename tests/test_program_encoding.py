"""The encoding of the text of a GCC driver query: the echo of ``cc1`` on Windows.

Measured on Windows 11: Arm GNU Toolchain 14.2 and clang 23 echo the
probe directory ``fwctx-é-probe`` with "é" as ``C3 A9`` (UTF-8), and Arm
GNU Toolchain 12.2 as ``E9`` (cp1252, its ANSI code page).  The Windows
branch runs on each system here: it reads bytes only.
"""

from __future__ import annotations

import locale
import os
import sys

import pytest

from fw_context_mcp.indexer._program_encoding import EchoMissingError, echo_flags, gcc_decoder


@pytest.fixture
def windows(monkeypatch):
    """The Windows branch, with cp1252 as the ANSI code page."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")


def _echo(name: bytes) -> bytes:
    return b'Target: arm-none-eabi\r\nignoring nonexistent directory "C:\\py\\python.exe\\' + name + b'"\r\n'


def test_the_probe_directory_cannot_exist(windows):
    """Its parent is the Python executable, a file: cc1 always ignores it, thus it is never a system directory."""
    flag, directory = echo_flags()

    assert flag == "-I"
    assert os.path.dirname(directory) == sys.executable


def test_an_echo_in_utf8_gives_utf8(windows):
    """GCC 14.2 and clang 23."""
    decode = gcc_decoder(_echo("fwctx-é-probe".encode()))

    assert decode(" C:\\Users\\měření ť\\inc\n".encode()) == " C:\\Users\\měření ť\\inc\n"


@pytest.mark.parametrize("echo", [b"fwctx-\xe9-probe", b"fwctx-e-probe"], ids=["cp1252", "best fit"])
def test_an_echo_that_is_no_utf8_gives_the_ansi_code_page(windows, echo: bytes):
    """GCC 12.2 writes "é" in cp1252; a code page without "é" gives a similar character."""
    decode = gcc_decoder(_echo(echo))

    assert decode(" C:\\Users\\míš\\inc\n".encode("cp1252")) == " C:\\Users\\míš\\inc\n"


def test_no_echo_is_an_error(windows):
    """Without the echo the encoding is not known: the query must not use a name it cannot read."""
    with pytest.raises(EchoMissingError):
        gcc_decoder(b"Target: arm-none-eabi\r\n")


def test_a_translated_message_still_gives_the_code_page(windows):
    """A GCC with translated messages (LANG=cs) writes no English "ignoring nonexistent directory"."""
    stderr = (
        b"COLLECT_GCC_OPTIONS='-E' '-v' '-I' 'C:\\py\\python.exe\\fwctx-\xc3\xa9-probe'\r\n"
        b'ignoruje se neexistuj\xc3\xadc\xc3\xad adres\xc3\xa1\xc5\x99 "C:\\py\\python.exe\\fwctx-\xc3\xa9-probe"\r\n'
    )

    assert gcc_decoder(stderr)("ř".encode()) == "ř"


def test_echoes_in_two_code_pages_are_an_error(windows):
    """The driver and cc1 do not agree: no code page is known."""
    stderr = _echo("fwctx-é-probe".encode()) + _echo(b"fwctx-\xe9-probe")

    with pytest.raises(EchoMissingError):
        gcc_decoder(stderr)


def test_posix_needs_no_echo(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert echo_flags() == []
    assert gcc_decoder(b"") is os.fsdecode
