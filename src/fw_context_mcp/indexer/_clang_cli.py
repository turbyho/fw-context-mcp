"""Run the clang command (not libclang) with the flags of a unit.

Two passes run clang as a program: the expansion of the macros
(``clang -dM -E``, macros.py) and the preprocessor of the assembly units
(``clang -E -C``, asm.py).  Both give it the flags of a unit, and both go
through :func:`run_clang`, thus they find the same binary and pass the
flags the same way.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from ..utils import CLANG_POSIX_RESPONSE_FILE, response_file

#: The places where the installer of LLVM for Windows puts clang.  The
#: installer does not always put its bin directory on PATH.
_WINDOWS_LLVM = (
    r"C:\Program Files\LLVM\bin\clang.exe",
    r"C:\Program Files (x86)\LLVM\bin\clang.exe",
)


@lru_cache(maxsize=1)
def clang_binary() -> str:
    """Return the clang command: from PATH, else from the install place of LLVM on Windows.

    Without either, the result is ``"clang"``, and the start of the
    program raises FileNotFoundError, which each caller reports.
    """
    found = shutil.which("clang")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in _WINDOWS_LLVM:
            if Path(candidate).exists():
                return candidate
    return "clang"


def run_clang(
    binary: str, arguments: Sequence[str], *, timeout: float, cwd: str | Path | None
) -> subprocess.CompletedProcess[bytes]:
    """Run *binary* with *arguments* in a response file, and return the result in bytes.

    WHY a response file: the flags of one unit can be longer than the
    command line of Windows (32767 characters).  CreateProcess then fails
    with error 206, and Python raises FileNotFoundError for it.  Measured:
    32798 characters for one unit of an ESP32 project, and the expansion of
    each such unit was skipped as if clang were missing.  The option
    ``--rsp-quoting=posix`` makes each host read the file the same way.
    """
    with response_file(arguments) as args_file:
        return subprocess.run(
            [binary, CLANG_POSIX_RESPONSE_FILE, f"@{args_file}"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, cwd=cwd, check=False,
        )
