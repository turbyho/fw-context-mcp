"""Every search result must carry the line the caller has to cite.

Measured on one session of 12 calls: the caller read a file-level hit from
``search_content``, had no line number for it, and spent three more calls
deriving one — a number that a second result already held.  ``read_file``
made that worse: its docstring forbids counting the lines of ``content``
(an inactive ``#ifdef`` branch is a blank line, so a raw count is a guess),
and it offered nothing in place of the count.

Two answers close the gap, and these tests pin both:

* ``search_content`` reports ``match_lines`` — the lines of the file that
  hold a query term.
* ``read_file`` numbers its lines on request, and reads a window of the
  file so the caller pays for what it needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# A file whose text is easy to count by eye.  Line 4 holds the constant,
# line 9 the table entry that names it again, and line 12 a comment that
# only a widened query reaches.
_HEADER = """\
#pragma once

enum class CommandType {
    SELF_TEST = 13,
    NUM = 14
};

static const char *const names[] = {
    "SELF_TEST",
};

// Self tester runs from the worker thread
"""

# A second file that the term reaches only through a variant of the token:
# the widened query of ``search_content`` (``SELF_TEST*`` → the tokens
# ``self`` and ``test*``) matches ``Self tester``, while no line here holds
# the term ``self_test`` as the caller wrote it.  This is the measured case
# from one project, where this file held the only caller of the feature.
_SOURCE = """\
#include "command.h"

void worker_loop(void)
{
    // Periodic process + Self tester
    sensor_periodic_process();
}
"""


@pytest.fixture
def indexed_project(tmp_path: Path) -> Path:
    """A project ``resolve_db_context`` can find, holding one indexed header.

    Built the way ``test_stale_body_detection`` builds one: the config
    points ``db_dir`` at *tmp_path*, where ``_resolve_context`` computes
    ``db_dir / project_id / index.db``.
    """
    from fw_context_mcp.indexer.db import (
        insert_symbols_batch,
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )
    from fw_context_mcp.utils import compute_source_hash

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    project_id = "proj-001"
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{project_id}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )
    files = {"src/command.h": ("c", _HEADER), "src/worker.cpp": ("cpp", _SOURCE)}
    for rel, (_lang, text) in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    # One definition whose body holds the call pattern from the docs.  It is
    # the query that used to come back empty, thus it belongs in the index
    # the tests search.
    body = (
        "void worker_loop(void)\n"
        "{\n"
        "    // Periodic process + Self tester\n"
        "    _timeout.attach(callback(&sensor_periodic_process), 1s);\n"
        "}\n"
    )

    db_path = tmp_path / project_id / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, project_id, root.name, str(root))
            upsert_build_config(conn, "ch", project_id, str(root / "compile_commands.json"))
            file_ids = {}
            for rel, (lang, text) in files.items():
                on_disk = root / rel
                file_ids[rel] = upsert_file(
                    conn, "ch", rel, lang,
                    mtime=on_disk.stat().st_mtime,
                    source_hash=compute_source_hash(on_disk),
                )
                conn.execute(
                    "UPDATE files SET content=? WHERE config_hash='ch' AND path=?",
                    (text, rel),
                )
            insert_symbols_batch(conn, [
                (
                    "ch", file_ids["src/worker.cpp"], "src/worker.cpp",
                    "worker loop",                    # name_tokens
                    "c:@F@worker_loop", "worker_loop", "worker_loop", "function",
                    3, 0, 7, 1,                       # line, col, end_line, is_definition
                    "void worker_loop(void)", "", None, 0, 0, "", 0, "", 1, 0.0,
                    body, 0,
                ),
                # A definition whose BODY never says "sensor" — only the
                # summary a model wrote does.  search_bodies must not answer
                # with it; search_code may.
                (
                    "ch", file_ids["src/worker.cpp"], "src/worker.cpp",
                    "sensor periodic process",
                    "c:@F@sensor_periodic_process", "sensor_periodic_process",
                    "sensor_periodic_process", "function",
                    9, 0, 12, 1,
                    "void sensor_periodic_process(void)", "", None, 0, 0, "", 0, "", 1, 0.0,
                    "void sensor_periodic_process(void)\n{\n    tick();\n}\n", 0,
                ),
            ])
            # The word `journal` appears in this summary and in no body,
            # thus a query for it separates the two sources cleanly.
            conn.execute(
                "UPDATE symbols SET summary = ? WHERE name = 'sensor_periodic_process'",
                ("Runs the self test on a timer and writes a journal entry.",),
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return root


def _by_file(rows: list[dict]) -> dict[str, dict]:
    """Index the results by file name, dropping any warning element."""
    return {
        Path(r["file"]).name: r
        for r in rows
        if "warning" not in r and "error" not in r
    }


class TestSearchContentMatchLines:
    def test_the_lines_of_the_matches_come_back(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.search import search_content

        rows = _by_file(search_content("SELF_TEST", project_root=str(indexed_project)))

        assert "command.h" in rows, "the header holds the term"
        assert rows["command.h"]["match_lines"] == [4, 9], (
            "the constant and the table entry, as line numbers of the file"
        )

    def test_the_field_is_absent_when_only_a_token_variant_matched(
        self, indexed_project: Path,
    ):
        """FTS5 reaches further than the substring test, and says so by omission.

        The widened query matches ``Self tester`` in the second file, and no
        line there holds ``self_test``.  Reporting a line would be a guess;
        the caller reads ``_match_snippet`` instead.
        """
        from fw_context_mcp.mcp.handlers.search import search_content

        rows = _by_file(search_content("SELF_TEST", project_root=str(indexed_project)))

        assert "worker.cpp" in rows, (
            "the wider query of search_content reaches the comment"
        )
        assert "match_lines" not in rows["worker.cpp"]
        assert rows["worker.cpp"]["_match_snippet"]

    def test_a_query_fts5_refuses_says_so(self, indexed_project: Path):
        """An empty list must keep its one meaning: no such code."""
        from fw_context_mcp.mcp.handlers.search import search_content

        rows = search_content('unbalanced "quote', project_root=str(indexed_project))

        assert rows and "warning" in rows[0], rows
        assert "FTS5 rejected" in rows[0]["warning"]
        assert rows[0]["hint"]


class TestSearchBodiesReachesACallPattern:
    """The pattern every copy of the docs names must find the code.

    ``.attach(`` is not legal FTS5 as a bare term.  The engine rejected it,
    the handler caught the error and answered with ``[]``, and the caller
    read that as "this code does not exist".  The tokenizer still drops the
    brackets — no FTS5 query can match punctuation — but the repaired query
    now finds the definitions that hold the word.
    """

    def test_the_documented_pattern_answers(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = _by_file(search_bodies(".attach(", project_root=str(indexed_project)))

        assert "worker.cpp" in rows
        assert rows["worker.cpp"]["name"] == "worker_loop"
        assert rows["worker.cpp"]["match_lines"] == [6], (
            "the line of the call inside the body, not the line of the definition"
        )

    def test_a_repaired_query_says_which_one_ran(self, indexed_project: Path):
        """The repair drops the punctuation, thus the answer is wider."""
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = _by_file(search_bodies(".attach(", project_root=str(indexed_project)))

        assert rows["worker.cpp"]["_fallback"] == "sanitized"
        assert rows["worker.cpp"]["_query_used"] == '".attach("'

    def test_a_query_fts5_refuses_says_so(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = search_bodies('unbalanced "quote', project_root=str(indexed_project))

        assert rows and "warning" in rows[0], rows
        assert "FTS5 rejected" in rows[0]["warning"]
        assert rows[0]["hint"]


class TestSearchBodiesSearchesTheBody:
    """The tool must match the definition text, and nothing else.

    ``symbols_fts`` indexes ten columns — the body among them, but also
    name, signature, docstring and the three that hold ``llm_analysis``.
    A bare MATCH searched all of them, thus the tool answered with
    definitions whose body never held the query.  Measured on one project,
    ``sensor`` gave 36 results of which 22 matched only through a summary
    that a model wrote — text the instructions call untrusted.  Those
    results even carried a snippet with no match in it, because the
    snippet comes from the body column.
    """

    def test_a_match_in_the_model_summary_is_not_a_match(self, indexed_project: Path):
        """`journal` is in the summary of one symbol and in no body."""
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = search_bodies("journal", project_root=str(indexed_project))

        assert [r for r in rows if "warning" not in r] == []

    def test_search_code_is_the_tool_that_reaches_it(self, indexed_project: Path):
        """The row is in the index — only this tool is allowed to find it."""
        from fw_context_mcp.mcp.handlers.search import search_code

        rows = search_code("journal", project_root=str(indexed_project))

        assert "sensor_periodic_process" in {r.get("name") for r in rows}

    def test_a_column_filter_written_by_the_caller_wins(self, indexed_project: Path):
        """Scoping must not intersect the column the caller named to nothing."""
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = search_bodies("summary : journal", project_root=str(indexed_project))

        assert "sensor_periodic_process" in {r.get("name") for r in rows}


class TestFtsSyntaxSurvivesTheRepair:
    """A query FTS5 accepts must reach it byte for byte.

    The repair exists for a code pattern the parser refuses.  It must never
    touch a query the parser takes, or it changes the question: quoting the
    brackets of ``NEAR(a b)`` turns an operator into two words, and quoting
    ``^attach`` turns "at the start of the column" into "anywhere".
    Measured before the order was fixed: 6 results → 0, and 23 → 106.
    """

    def _names(self, rows: list[dict]) -> set[str]:
        return {r["name"] for r in rows if "warning" not in r and "error" not in r}

    def test_the_near_operator_still_works(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.search import search_bodies

        # `attach` and `sensor` sit one token apart in the body (the
        # `callback` between them), thus distance 1 matches and 0
        # (adjacent) does not — the distance still does its work.
        near = search_bodies("NEAR(sensor attach, 1)", project_root=str(indexed_project))
        adjacent = search_bodies("NEAR(sensor attach, 0)", project_root=str(indexed_project))

        assert self._names(near) == {"worker_loop"}
        assert self._names(adjacent) == set()

    def test_an_accepted_query_is_never_marked_repaired(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.search import search_bodies

        rows = search_bodies("attach", project_root=str(indexed_project))

        assert rows
        assert all("_fallback" not in r for r in rows), rows


class TestReadFileAnchors:
    def test_the_default_call_is_unchanged(self, indexed_project: Path):
        """The bare text must stay byte-for-byte what the index stored."""
        from fw_context_mcp.mcp.handlers.source import read_file

        result = read_file(file_path="src/command.h", project_root=str(indexed_project))

        assert result["content"] == _HEADER
        assert result["lines"] == 12
        assert "start_line" not in result

    def test_the_numbers_let_the_caller_cite_a_line(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.source import read_file

        result = read_file(
            file_path="src/command.h", project_root=str(indexed_project), line_numbers=True,
        )
        lines = result["content"].splitlines()

        assert lines[3] == "   4      SELF_TEST = 13,"
        assert result["lines"] == 12

    def test_a_window_costs_only_what_it_holds(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.source import read_file

        result = read_file(
            file_path="src/command.h", project_root=str(indexed_project),
            start_line=3, end_line=5,
        )

        assert result["content"] == "enum class CommandType {\n    SELF_TEST = 13,\n    NUM = 14"
        assert (result["start_line"], result["end_line"]) == (3, 5)
        assert result["lines"] == 12, "`lines` stays the length of the whole file"

    def test_the_end_is_clamped_to_the_file(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.source import read_file

        result = read_file(
            file_path="src/command.h", project_root=str(indexed_project),
            start_line=11, end_line=9999,
        )

        assert result["end_line"] == 12
        assert result["content"].splitlines()[-1].startswith("// Self tester")

    def test_one_bound_is_enough(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.source import read_file

        head = read_file(
            file_path="src/command.h", project_root=str(indexed_project), end_line=2,
        )
        tail = read_file(
            file_path="src/command.h", project_root=str(indexed_project), start_line=12,
        )

        assert (head["start_line"], head["end_line"]) == (1, 2)
        assert (tail["start_line"], tail["end_line"]) == (12, 12)

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"start_line": -1}, "negative"),
            ({"start_line": 9, "end_line": 3}, "before"),
            ({"start_line": 400}, "past the end"),
        ],
    )
    def test_a_range_that_makes_no_sense_is_an_error(
        self, indexed_project: Path, kwargs: dict, expected: str,
    ):
        """Silence would hand back a window the caller never asked for."""
        from fw_context_mcp.mcp.handlers.source import read_file

        result = read_file(
            file_path="src/command.h", project_root=str(indexed_project), **kwargs,
        )

        assert expected in result["error"]
        assert "content" not in result
