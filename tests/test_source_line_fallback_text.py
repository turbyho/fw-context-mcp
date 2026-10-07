"""The source-line fallback of the reference pass scans the text that the parse read.

``_run_source_line_fallback`` read the main file of the unit from the disk
after the parse.  A file saved during the parse gave the scan lines that no
extent of the parse describes, and a file that the build removed after the
parse raised ``FileNotFoundError`` out of ``extract_all``: the except clause
named no ``OSError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOURCE = """\
static int helper(int v) { return v + 1; }
int caller(int v) { return helper(v); }
"""


@pytest.mark.libclang
def test_a_source_gone_after_the_parse_does_not_stop_the_extraction(tmp_path: Path, monkeypatch):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "unit.c"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c", clang_args=["-std=gnu11"])
    read_text = Path.read_text

    def gone(self: Path, *args, **kwargs) -> str:
        if self.resolve() == src.resolve():
            raise FileNotFoundError(str(self))
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", gone)
    result = extract_all(unit, with_refs=True)

    assert any(r.ref_kind == "call" for r in result.references)
