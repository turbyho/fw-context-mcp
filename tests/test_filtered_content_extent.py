"""The ifdef-filtered content must span the whole file, not stop at the last token.

``files.content`` backs ``read_file`` and ``search_content``.  It is built
from the set of lines that libclang tokenization reports as active, and the
loop that assembles it used to stop at ``max(active_lines)``.  Conditional
preprocessor directives carry no token, thus a file whose last line is
``#endif`` — every include guard — lost its tail: the stored text ended
early and ``read_file`` reported a line count smaller than the file.

These tests pin the extent to the file, not to the last token.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang


def _fill_content(root: Path, db_path: Path, files: dict[str, str], main: str) -> dict[str, str]:
    """Index *files* through the real content-fill pass and return stored text.

    *files* maps a project-relative path to its text; *main* names the entry
    in it that compile_commands.json points at.  The return maps the same
    relative paths to the ``files.content`` the pass stored, so a test can
    compare stored text against the text it wrote to disk.
    """
    from fw_context_mcp.indexer.compile_commands import parse as parse_cc
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )
    from fw_context_mcp.indexer.ops import _build_filtered_file_content

    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    cc = root / "compile_commands.json"
    cc.write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(root / main),
                    "arguments": ["cc", "-c", str(root / main), "-I", str(root / "src")],
                }
            ]
        ),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            upsert_build_config(conn, "ch", "pid", str(cc))
            # The rows must exist before the content pass: `remaining` counts
            # files with empty content, and a zero count takes the fast path
            # that returns before the fill loop this test exercises.
            for rel in files:
                lang = "cpp" if Path(rel).suffix in {".cpp", ".hpp", ".cc"} else "c"
                upsert_file(conn, "ch", rel, lang, mtime=1.0)

        unit = next(iter(parse_cc(cc)))
        with transaction(conn):
            _build_filtered_file_content(conn, unit, "ch", root)

        return {
            row["path"]: row["content"]
            for row in conn.execute(
                "SELECT path, content FROM files WHERE config_hash='ch'"
            ).fetchall()
        }
    finally:
        conn.close()


class TestTrailingDirectivesSurvive:
    """A file whose tail holds only directives must keep its full length."""

    def test_include_guard_endif_is_not_cut(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        header = (
            "#ifndef API_H_\n"          # 1
            "#define API_H_\n"          # 2
            "\n"                        # 3
            "int kept(void);\n"         # 4
            "\n"                        # 5
            "#endif // API_H_\n"        # 6 — last token-free line
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/api.h": header,
                "src/main.c": '#include "api.h"\nint main(void) { return kept(); }\n',
            },
            main="src/main.c",
        )

        content = stored["src/api.h"]
        assert content.splitlines().__len__() == len(header.splitlines()), (
            "the stored text must be as long as the file — read_file reports "
            "len(content.splitlines()) as `lines`, and a short answer there "
            "tells the caller the file ends before it does"
        )
        assert "kept" in content, "an active declaration must survive the filter"

    def test_line_numbers_still_align_after_the_last_token(self, tmp_path: Path) -> None:
        """Padding the tail must not shift the lines that carry code."""
        root = tmp_path / "proj"
        header = (
            "#ifndef CFG_H_\n"          # 1
            "#define CFG_H_\n"          # 2
            "int marker_line_3(void);\n"  # 3
            "#endif\n"                  # 4
            "\n"                        # 5
        )
        stored = _fill_content(
            root,
            tmp_path / "index.db",
            {
                "src/cfg.h": header,
                "src/main.c": '#include "cfg.h"\nint main(void) { return marker_line_3(); }\n',
            },
            main="src/main.c",
        )

        lines = stored["src/cfg.h"].splitlines()
        assert len(lines) == 5
        assert "marker_line_3" in lines[2], (
            "line 3 must still be line 3 — the tail is padded with blank "
            "lines, never inserted before existing content"
        )
