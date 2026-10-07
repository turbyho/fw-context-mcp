"""The content fill reads each file through the handle that the parse gave.

Two reads of the disk stayed in the content fill after the parse:

* ``tu.get_file(<resolved path>)`` looked a header up by a path that the
  parse did not use.  For a header reached through a symlink and saved by a
  rename after the parse, libclang answered with a null handle, and the
  file kept no content.
* ``_token_lines_of_file`` took the size of the tokenized range from the
  disk.  A file cut after the parse lost the comment lines of its tail.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang

MAIN = '#include "h.h"\nint main(void) { return 0; }\n'


def _fill_after(root: Path, header_dir: str, include_dir: str, header: str, change: Callable[[Path], None]) -> str:
    """Parse the unit, apply *change* to the header, run the content fill, return the stored header text."""
    from fw_context_mcp.indexer.compile_commands import parse as parse_cc
    from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_file, upsert_project
    from fw_context_mcp.indexer.ops import _build_filtered_file_content
    from fw_context_mcp.indexer.symbols import extract_all

    (root / header_dir).mkdir(parents=True, exist_ok=True)
    header_path = root / header_dir / "h.h"
    header_path.write_text(header, encoding="utf-8")
    (root / "main.c").write_text(MAIN, encoding="utf-8")
    cc = root / "compile_commands.json"
    cc.write_text(json.dumps([{
        "directory": str(root),
        "file": str(root / "main.c"),
        "arguments": ["cc", "-c", str(root / "main.c"), "-I", str(root / include_dir)],
    }]), encoding="utf-8")

    unit = next(iter(parse_cc(cc)))
    tu = extract_all(unit, return_tu=True).tu
    change(header_path)

    conn = open_db(root / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            upsert_build_config(conn, "ch", "pid", str(cc))
            # Rows with empty content, or the pass takes its fast path and
            # fills nothing (see test_filtered_content_extent).
            for rel in ("main.c", f"{header_dir}/h.h"):
                upsert_file(conn, "ch", rel, "c", mtime=1.0)
        with transaction(conn):
            _build_filtered_file_content(conn, unit, "ch", root, existing_tu=tu)
        row = conn.execute(
            "SELECT content FROM files WHERE config_hash='ch' AND path=?", (f"{header_dir}/h.h",),
        ).fetchone()
    finally:
        conn.close()
    return row["content"] if row else ""


def _rename_save(path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(path.read_text(encoding="utf-8") + "int h_saved(void);\n", encoding="utf-8")
    os.replace(tmp, path)


def test_a_header_reached_through_a_symlink_keeps_the_text_of_the_parse(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "inc").symlink_to("real", target_is_directory=True)
    header = "/* header comment */\nint h_parsed(void);\n"

    content = _fill_after(root, "real", "inc", header, _rename_save)

    assert content == header


def test_a_file_that_the_parse_did_not_load_is_read_from_the_disk(tmp_path: Path) -> None:
    """``TranslationUnit.get_file`` raised AssertionError for a missing file.

    The constructor of ``File`` asserts a non-null pointer, thus the
    lookup never gave the null handle that the readers expected, and the
    promised read of the disk never came.  A file on the disk that the
    parse did not load gets a handle, and no text of the parse.
    """
    from fw_context_mcp.indexer._parsed_text import file_of_parse, parsed_bytes
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.ops import _cached_read_lines, _clear_body_cache
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "main.c"
    src.write_text(MAIN.replace('#include "h.h"\n', ""), encoding="utf-8")
    other = tmp_path / "other.c"
    other.write_text("int other(void) { return 1; }\n", encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c", clang_args=["-std=gnu11"])
    tu = extract_all(unit, return_tu=True).tu

    # libclang knows a file on the disk by its name, but holds no text of it.
    assert parsed_bytes(tu, file_of_parse(tu, str(other))) is None
    assert file_of_parse(tu, str(tmp_path / "missing.h")) is None
    _clear_body_cache()
    assert _cached_read_lines(str(other), tu) == ["int other(void) { return 1; }\n"]
    assert _cached_read_lines(str(tmp_path / "missing.h"), tu) is None


def test_a_header_cut_after_the_parse_keeps_its_comment_lines(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    header = "int h_decl(void);\n/* tail comment 1 */\n/* tail comment 2 */\n"

    def cut(path: Path) -> None:
        path.write_text("int h_decl(void);\n", encoding="utf-8")

    content = _fill_after(root, "src", "src", header, cut)

    assert content == header
