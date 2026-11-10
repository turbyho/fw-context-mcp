"""The output of pip in a fix of ``fw-context doctor`` is UTF-8.

pip writes the ANSI code page to a pipe on Windows, and a character
outside it stopped pip with UnicodeEncodeError (measured on Windows 11 with
a Python child): the fix then reported the crash and not the answer of pip.
"""

from __future__ import annotations

import pytest

from fw_context_mcp.deps._fixes import _pip

# The test runs pip of this environment: an environment of uv can have none.
pytest.importorskip("pip")


def test_a_name_with_a_character_outside_the_ansi_code_page_reaches_the_message(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)  # pip, not uv

    ok, output = _pip(["show", "fwctx-ťest-none"])

    assert not ok
    assert "fwctx-ťest-none" in output
