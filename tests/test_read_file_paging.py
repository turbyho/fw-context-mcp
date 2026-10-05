"""read_file without end_line gives one page of at most 2000 lines.

The tool gave the whole file when the caller set no range, and a vendor
header of 10 000 to 20 000 lines then filled the context in one answer.
Without ``end_line`` the answer is now one page, and ``page`` gives the
size of the file and the call for the next page.  An explicit range stays
whole, and a file that fits into one page comes back as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._paging import make_project, with_project

LINES = 4500


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = make_project(tmp_path, lambda conn, ids: None, files=2)
    # The index holds no content for these files, thus read_file reads the
    # disk; the paging is the same on both paths.
    (root / "src" / "f0.c").write_text(
        "".join(f"int line_{n};\n" for n in range(1, LINES + 1)), encoding="utf-8",
    )
    return root


def _read(project: Path, **kw) -> dict:
    from fw_context_mcp.mcp.handlers.source import read_file

    return read_file("src/f0.c", project_root=str(project), **kw)


def test_the_pages_walk_the_whole_file_once(project):
    start = 0
    seen: list[str] = []
    while True:
        result = _read(project, start_line=start)
        page = result["page"]
        assert page["total"] == LINES
        assert page["offset"] == max(start, 1) - 1
        lines = result["content"].splitlines()
        assert page["shown"] == len(lines) <= 2000
        seen += lines
        if not page["more"]:
            break
        start = int(page["hint"].split("start_line=")[1].split(")")[0])
    assert seen == [f"int line_{n};" for n in range(1, LINES + 1)]


def test_the_hint_names_the_next_start_line(project):
    assert _read(project)["page"]["hint"] == (
        with_project("read_file('src/f0.c', start_line=2001) reads the next page.", project)
    )
    assert _read(project, line_numbers=True)["page"]["hint"] == (
        with_project("read_file('src/f0.c', line_numbers=True, start_line=2001) reads the next page.", project)
    )


def test_a_page_says_where_it_stops(project):
    result = _read(project)
    assert (result["start_line"], result["end_line"]) == (1, 2000)
    assert result["lines"] == LINES


def test_an_explicit_range_stays_whole(project):
    result = _read(project, start_line=1, end_line=3000)
    assert len(result["content"].splitlines()) == 3000
    assert "page" not in result


def test_a_file_that_fits_comes_back_as_it_was(project):
    """f1.c has 400 lines: one page, the text with its closing newline."""
    from fw_context_mcp.mcp.handlers.source import read_file

    result = read_file("src/f1.c", project_root=str(project))
    assert result["content"] == (project / "src" / "f1.c").read_text(encoding="utf-8")
    assert result["page"] == {"total": 400, "offset": 0, "shown": 400, "more": False}
    assert "start_line" not in result
