"""The text of ``clang -dM -E`` is decoded as the content fill decodes a source.

The output holds the values of the macros, thus text of the sources.  It
was decoded in the locale: a byte that is no UTF-8 stopped the run with
UnicodeDecodeError on Linux, and Windows decoded it in its ANSI code page.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fw_context_mcp.indexer.macros import resolve_macros_via_preprocessor

pytestmark = pytest.mark.skipif(shutil.which("clang") is None, reason="the preprocessor is clang")


def test_a_value_with_a_byte_that_is_no_utf8_does_not_stop_the_run(tmp_path: Path):
    source = tmp_path / "a.c"
    source.write_bytes(b'#define GREETING "Gr\xfc\xdfe"\n#define ANSWER 42\n')

    macros = resolve_macros_via_preprocessor([], source, cwd=tmp_path)

    assert macros["ANSWER"] == "42"
    assert macros["GREETING"] == '"Gr��e"'


def test_a_utf8_value_keeps_its_characters(tmp_path: Path):
    source = tmp_path / "a.c"
    source.write_text('#define GREETING "žluťoučký"\n', encoding="utf-8")

    macros = resolve_macros_via_preprocessor([], source, cwd=tmp_path)

    assert macros["GREETING"] == '"žluťoučký"'
