"""``utils.use_utf8_stdio``: text output into a pipe is UTF-8 on each system.

On Windows a pipe or a file got the ANSI code page (cp1252), and a ``print``
of ``✓`` raised UnicodeEncodeError.  The test runs on each system: on
Linux and macOS the locale is UTF-8 already.
"""

from __future__ import annotations

import subprocess
import sys

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first

SCRIPT = (
    "from fw_context_mcp.utils import use_utf8_stdio\n"
    "use_utf8_stdio()\n"
    "import sys\n"
    "print('\\u2713 \\u017dlu\\u0165')\n"
    "print('\\u2014', file=sys.stderr)\n"
)


def test_a_pipe_gets_utf8() -> None:
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", SCRIPT], capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").splitlines() == ["✓ Žluť"]
    assert result.stderr.decode("utf-8").splitlines() == ["—"]
