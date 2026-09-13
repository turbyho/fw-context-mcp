"""A paged answer says where it sits, and two pages never overlap or skip.

A tool that cuts its answer at ``limit`` used to leave the reader guessing:
a result of exactly ``limit`` rows could be the whole truth or the first
slice of hundreds.  Every paged tool now leads with a notice that carries
``total``, ``offset``, ``shown`` and ``more``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    refs_for_symbol,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.indexer.db._refs import (
    count_refs_for_symbol,
    find_refs_with_candidates,
)
from fw_context_mcp.mcp.shared.paging import clamp_offset, page_notice

CH = "hash-deadbeef"


def _symbol_row(file_id, file_path, name, qualified_name, usr, line, kind="method"):
    return (
        CH, file_id, file_path, name, usr, name, qualified_name, kind,
        line, 1, line + 5, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
    )


@pytest.fixture
def db():
    """One method with twelve call sites, and a second of the same name."""
    tmpdir = Path(tempfile.mkdtemp())
    conn = open_db(tmpdir / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", "/tmp/test")
        upsert_build_config(conn, CH, "proj-001", "/tmp/compile_commands.json")
    fid = upsert_file(conn, CH, "src/main.cpp", "cpp")
    insert_symbols_batch(conn, [
        _symbol_row(fid, "src/main.cpp", "probe", "ClassA::probe", "u_a", 10),
        _symbol_row(fid, "src/main.cpp", "probe", "ClassB::probe", "u_b", 30),
        _symbol_row(fid, "src/main.cpp", "caller", "Caller::run", "u_run", 50),
    ])
    insert_refs_batch(conn, [
        (CH, "u_a", "src/main.cpp", 100 + i, "u_run", "call", None)
        for i in range(12)
    ] + [
        (CH, "u_b", "src/main.cpp", 200 + i, "u_run", "call", None)
        for i in range(3)
    ])
    yield conn
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestPageNotice:
    """The row that removes the guess."""

    def test_it_reports_where_the_page_sits(self):
        notice = page_notice(137, 20, 20, hint="pass offset=40")
        assert notice["total"] == 137
        assert notice["offset"] == 20
        assert notice["shown"] == 20
        assert notice["more"] is True
        assert notice["hint"] == "pass offset=40"

    def test_the_last_page_says_so_and_gives_no_hint(self):
        """An instruction that leads nowhere costs a call to find that out."""
        notice = page_notice(40, 20, 20, hint="pass offset=40")
        assert notice["more"] is False
        assert "hint" not in notice

    def test_a_single_page_still_carries_the_count(self):
        """Always present: an absent notice would have to mean 'no more'."""
        notice = page_notice(3, 0, 3)
        assert notice == {"total": 3, "offset": 0, "shown": 3, "more": False}

    def test_a_missing_or_negative_offset_is_zero(self):
        assert clamp_offset(None) == 0
        assert clamp_offset(-5) == 0
        assert clamp_offset(0) == 0
        assert clamp_offset(7) == 7


class TestRefsPageWalk:
    """Two pages of one walk must not overlap and must not skip."""

    def _walk(self, db, name, page):
        seen: list = []
        offset = 0
        while True:
            rows, _c, total = find_refs_with_candidates(
                db, CH, name, ref_kind=["call"], limit=page, offset=offset
            )
            if not rows:
                break
            seen += [(r["from_file"], r["from_line"]) for r in rows]
            offset += len(rows)
            if offset >= total:
                break
        return seen, total

    def test_one_symbol_walks_whole_and_once(self, db):
        seen, total = self._walk(db, "ClassA::probe", page=5)
        assert total == 12
        assert len(seen) == 12, f"walked {len(seen)} of {total}"
        assert len(set(seen)) == 12, "a row came back twice"

    def test_an_ambiguous_name_walks_every_symbol(self, db):
        """The merge interleaves, thus the offset must skip the MERGED list.

        Giving the offset to each symbol query would walk each one past its
        own rows, and the interleave would step over the rows between.
        """
        seen, total = self._walk(db, "probe", page=4)
        assert total == 15, f"total should cover both symbols, got {total}"
        assert len(seen) == 15, f"walked {len(seen)} of {total}"
        assert len(set(seen)) == 15, "a row came back twice"
        # Both symbols really are represented.
        assert any(line >= 200 for _f, line in seen), "ClassB never showed"

    def test_the_first_page_holds_both_symbols(self, db):
        """Interleaving, not concatenation: ClassB must not wait for page 4."""
        rows, _c, _total = find_refs_with_candidates(
            db, CH, "probe", ref_kind=["call"], limit=4, offset=0
        )
        targets = {r["target_qualified_name"] for r in rows}
        assert targets == {"ClassA::probe", "ClassB::probe"}, f"got: {targets}"

    def test_an_offset_past_the_end_gives_nothing(self, db):
        rows, _c, total = find_refs_with_candidates(
            db, CH, "ClassA::probe", ref_kind=["call"], limit=5, offset=99
        )
        assert rows == []
        assert total == 12, "the count must still describe the whole answer"


class TestCountMatchesThePage:
    """The count and the page must run on the same conditions."""

    def test_the_count_ignores_the_page_bound(self, db):
        total = count_refs_for_symbol(db, CH, "u_a", "method", ref_kind=["call"])
        page = refs_for_symbol(db, CH, "u_a", "method", ref_kind=["call"], limit=3)
        assert total == 12
        assert len(page) == 3

    def test_the_kind_filter_reaches_the_count(self, db):
        """A count over a different filter would describe a different answer."""
        insert_refs_batch(db, [(CH, "u_a", "src/main.cpp", 300, "u_run", "member", None)])
        db.commit()
        calls = count_refs_for_symbol(db, CH, "u_a", "method", ref_kind=["call"])
        every = count_refs_for_symbol(db, CH, "u_a", "method", ref_kind=None)
        assert calls == 12
        assert every == 13, "the unfiltered count must see the member row"
