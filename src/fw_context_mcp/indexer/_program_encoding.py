"""The text encoding of the output of a GCC driver query, and its decode.

A GCC driver query (``gcc -E -v``) gives the names of directories as
bytes, and the index needs each name as this process names the file.

- POSIX: the bytes of the file name.  ``os.fsdecode`` gives the name.
- Windows: the active code page of ``cc1``, the process that writes the
  list of directories.  Measured on Windows 11 with a path that holds
  "měření ť":

  - Arm GNU Toolchain 14.2 writes UTF-8 (its manifest declares the code
    page UTF-8).
  - Arm GNU Toolchain 12.2 writes the ANSI code page (cp1252 there).  A
    character that the code page does not hold became a similar one ("ě"
    gave "e"), and GCC could not open the file at all.
  - PlatformIO installs older GCC versions (7, 10).

  The query therefore asks the compiler itself: it adds the directory
  ``<python.exe>\\fwctx-é-probe`` (``echo_flags``), which cannot exist,
  because its parent is a file.  The driver writes it in its options and
  in the command line of ``cc1``, and ``cc1`` writes ``ignoring
  nonexistent directory "...fwctx-é-probe"``, each in its code page.  The
  bytes of "é" give the code page: ``C3 A9`` is UTF-8, else it is the ANSI
  code page (measured: ``E9`` from GCC 12.2, ``C3 A9`` from GCC 14.2 and
  clang 23).  Each line that holds the name counts, not only the English
  message: a GCC with translated messages (``LANG=cs``) writes the message
  in another language.
  WHY the echo and not the manifest of the executable: the path of the
  compiler can be a shim (scoop), a wrapper (ccache) or a batch file, and
  the text comes from ``cc1`` behind it.  The echo comes from the process
  that writes the list.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

__all__ = ["EchoMissingError", "echo_flags", "gcc_decoder"]

#: The name of the directory that ``cc1`` echoes.  "é" is in the ANSI code
#: page of most systems; where it is not, ``cc1`` gives another character,
#: and that also is no UTF-8 of "é".
_ECHO_NAME = "fwctx-é-probe"
#: The ASCII parts of the name: they find each echo, also where "é" is
#: another character.
_ECHO_START = b"fwctx-"
_ECHO_END = b"-probe"


class EchoMissingError(ValueError):
    """The query output holds no echo of the probe directory, thus its encoding is not known."""


def echo_flags() -> list[str]:
    """Return the flags that make ``cc1`` echo the probe directory, for a ``-E -v`` query.

    Empty on POSIX: the output there is the bytes of the file names.
    """
    if sys.platform != "win32":
        return []
    return ["-I", os.path.join(sys.executable, _ECHO_NAME)]


def gcc_decoder(verbose_stderr: bytes) -> Callable[[bytes], str]:
    """Return the decode of the text of the GCC driver query whose ``-E -v`` stderr is *verbose_stderr*.

    The decode raises ``UnicodeDecodeError`` for bytes that are not text of
    that encoding: the caller must not use a name that it cannot read.

    Raises:
        EchoMissingError: On Windows, when *verbose_stderr* holds no echo
            of the probe directory of ``echo_flags``, or when its echoes
            give two code pages (the driver and ``cc1`` do not agree).
    """
    if sys.platform != "win32":
        return os.fsdecode
    import locale

    utf8_echo = _ECHO_NAME.encode("utf-8")
    echoes = [line for line in verbose_stderr.splitlines() if _ECHO_START in line and _ECHO_END in line]
    if not echoes:
        raise EchoMissingError(f"the compiler did not echo the directory {_ECHO_NAME}")
    in_utf8 = {utf8_echo in line for line in echoes}
    if in_utf8 == {True}:
        return lambda data: data.decode("utf-8")
    if in_utf8 == {False}:
        # ``locale.getencoding`` gives the ANSI code page also when this
        # process runs in the UTF-8 mode of Python.
        ansi = locale.getencoding()
        return lambda data: data.decode(ansi)
    raise EchoMissingError(f"the compiler echoed the directory {_ECHO_NAME} in two code pages")
