"""``utils.use_utf8_stdio``: text output into a pipe is UTF-8, and stderr keeps its error handler.

On Windows a pipe or a file got the ANSI code page (cp1252), and a ``print``
of ``✓`` raised UnicodeEncodeError.  The child process of each test gets
``PYTHONIOENCODING=cp1252``, the condition of Windows, thus the test shows
the repair on each system: ``_reconfigure_utf8`` is the part that
``use_utf8_stdio`` runs on Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first


def _run(script: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", script], capture_output=True, check=False, env=env,
    )


def test_without_the_repair_cp1252_cannot_write_the_text() -> None:
    """The condition is real: the child cannot write the text without the repair."""
    result = _run("print('\\u2713')")
    assert result.returncode != 0
    assert b"UnicodeEncodeError" in result.stderr


def test_a_pipe_gets_utf8() -> None:
    result = _run(
        "import sys\n"
        "from fw_context_mcp.utils import _reconfigure_utf8\n"
        "_reconfigure_utf8([sys.stderr, sys.stdout])\n"
        "print('\\u2713 \\u017dlu\\u0165')\n"
        "print('\\u2014', file=sys.stderr)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").splitlines() == ["✓ Žluť"]
    assert result.stderr.decode("utf-8").splitlines() == ["—"]


def test_stderr_keeps_backslashreplace() -> None:
    """A lone surrogate in a traceback prints as an escape, and does not raise."""
    result = _run(
        "import sys\n"
        "from fw_context_mcp.utils import _reconfigure_utf8\n"
        "_reconfigure_utf8([sys.stderr, sys.stdout])\n"
        "print('a\\udcffb', file=sys.stderr)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.decode("utf-8").splitlines() == ["a\\udcffb"]
