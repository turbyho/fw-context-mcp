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
    find_macro_refs,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    rebuild_files_fts,
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
from fw_context_mcp.mcp.handlers.callgraph import _resolve_virtual_callers
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


class TestAZeroLimitWouldHintAtAStandstill:
    """Why every paged tool takes one row at least.

    ``page_notice`` reads ``more`` from ``offset + shown < total``, thus a
    page of no rows is always "not the last one" and its hint names the
    offset the reader already gave.  A reader that follows the hint never
    moves.  ``tests/test_search_paging.py`` pins the clamp on each tool
    that could produce such a page.
    """

    def test_the_notice_of_an_empty_page_never_advances(self):
        notice = page_notice(12, 4, 0, hint="search_code('x', offset=4) reads the next page.")
        assert notice["more"] is True
        assert "offset=4" in notice["hint"], (
            "the hint names the offset the reader already stands at"
        )


class TestAggregateMembersMatchExactly:
    """An aggregate reaches its members by a USR PREFIX, and only by that.

    libclang writes a member USR as ``<owner>@<member>``, thus the query
    for a class, struct or enum matches that prefix.  It used to match it
    with ``LIKE owner || '@%'`` and no ESCAPE clause, which is wrong twice:

    * ``_`` is the single-character wildcard of LIKE, and a USR holds one
      whenever the C identifier does — which is most of the time.
    * LIKE is case-insensitive for ASCII, while a USR is case-sensitive.
    """

    @pytest.fixture
    def types_db(self, db):
        """Two structs whose USRs differ only where a wildcard could match."""
        fid = upsert_file(db, CH, "src/types.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/types.cpp", "my_type", "my_type",
                        "c:@S@my_type", 10, kind="struct"),
            # One character apart, at the position of the underscore.
            _symbol_row(fid, "src/types.cpp", "myXtype", "myXtype",
                        "c:@S@myXtype", 20, kind="struct"),
            # Apart only by case.
            _symbol_row(fid, "src/types.cpp", "MY_TYPE", "MY_TYPE",
                        "c:@S@MY_TYPE", 30, kind="struct"),
            _symbol_row(fid, "src/types.cpp", "run", "App::run", "u_app", 40),
        ])
        insert_refs_batch(db, [
            (CH, "c:@S@my_type@FI@field", "src/types.cpp", 100, "u_app", "member", None),
            (CH, "c:@S@myXtype@FI@field", "src/types.cpp", 101, "u_app", "member", None),
            (CH, "c:@S@MY_TYPE@FI@field", "src/types.cpp", 102, "u_app", "member", None),
        ])
        db.commit()
        return db

    def test_an_underscore_is_not_a_wildcard(self, types_db):
        rows = refs_for_symbol(types_db, CH, "c:@S@my_type", "struct")
        lines = sorted(r["from_line"] for r in rows)
        assert lines == [100], (
            f"the members of another struct came back: {lines}"
        )

    def test_the_match_is_case_sensitive(self, types_db):
        rows = refs_for_symbol(types_db, CH, "c:@S@MY_TYPE", "struct")
        lines = sorted(r["from_line"] for r in rows)
        assert lines == [102], f"a struct of another case came back: {lines}"

    def test_the_count_agrees_with_the_page(self, types_db):
        total = count_refs_for_symbol(types_db, CH, "c:@S@my_type", "struct")
        rows = refs_for_symbol(types_db, CH, "c:@S@my_type", "struct")
        assert total == 1, f"the count reached another struct: {total}"
        assert len(rows) == total

    def test_the_owner_itself_is_not_a_member(self, types_db):
        """A reference to the type itself carries the bare USR, no ``@``."""
        insert_refs_batch(types_db, [
            (CH, "c:@S@my_type", "src/types.cpp", 110, "u_app", "ref", None),
        ])
        types_db.commit()
        rows = refs_for_symbol(types_db, CH, "c:@S@my_type", "struct")
        assert sorted(r["from_line"] for r in rows) == [100]


class TestAggregatePageWalk:
    """The aggregate branch groups by the caller, thus so must its order.

    It grouped by ``(caller.qualified_name, from_file, from_line)`` and
    ordered by ``(from_file, from_line)`` alone.  One line can carry
    references from several callers — measured on one firmware index, 88
    lines did, one of them with 8 — and those groups then tie in the order.
    """

    @pytest.fixture
    def shared_line_db(self, db):
        """Six callers referencing members of one struct on ONE line."""
        fid = upsert_file(db, CH, "src/atomic.h", "cpp")
        symbols = [
            _symbol_row(fid, "src/atomic.h", "Holder", "Holder", "c:@S@Holder",
                        5, kind="struct"),
        ]
        for i in range(6):
            symbols.append(
                _symbol_row(fid, "src/atomic.h", f"c{i}", f"Caller::c{i}",
                            f"u_c{i}", 10 + i)
            )
        insert_symbols_batch(db, symbols)
        # Every reference sits on the SAME file and line, thus the order
        # ties unless it also carries the caller.
        insert_refs_batch(db, [
            (CH, f"c:@S@Holder@FI@f{i}", "src/atomic.h", 769, f"u_c{i}", "member", None)
            for i in range(6)
        ])
        db.commit()
        return db

    def test_a_walk_over_tied_rows_neither_repeats_nor_skips(self, shared_line_db):
        total = count_refs_for_symbol(shared_line_db, CH, "c:@S@Holder", "struct")
        assert total == 6, f"the fixture no longer ties six groups: {total}"

        seen: list = []
        for offset in (0, 2, 4):
            page = refs_for_symbol(
                shared_line_db, CH, "c:@S@Holder", "struct", limit=2, offset=offset,
            )
            seen += [r["caller_qname"] for r in page]

        assert len(seen) == 6, f"walked {len(seen)} of {total}"
        assert len(set(seen)) == 6, f"a row came back twice: {sorted(seen)}"


class TestMacroRefsRead:
    """The macro fallback of find_callers reads EVERY match, then filters."""

    @pytest.fixture
    def macro_db(self, db):
        """Six files that hold the macro, ordered so that rank ties."""
        for i in range(6):
            fid = upsert_file(db, CH, f"src/use{i}.c", "c")
            db.execute(
                "UPDATE files SET content = ? WHERE id = ?",
                (f"int x{i} = GAP_LIMIT;\n", fid),
            )
        db.commit()
        rebuild_files_fts(db)
        db.commit()
        return db

    def test_it_reads_past_the_old_hard_cap(self, macro_db):
        """``limit=None`` means every match, not a page of them.

        The caller filters the comment matches out in Python, thus a SQL
        LIMIT would cut a set the answer has not been filtered out of yet
        and neither the page nor its count could be the truth.
        """
        rows = find_macro_refs(macro_db, CH, "GAP_LIMIT", limit=None)
        assert len(rows) == 6, f"got: {[r['file_path'] for r in rows]}"

    def test_a_limit_still_bounds_the_read(self, macro_db):
        rows = find_macro_refs(macro_db, CH, "GAP_LIMIT", limit=2)
        assert len(rows) == 2

    def test_the_order_is_stable_across_calls(self, macro_db):
        """``rank`` ties over six alike files; ``f.path`` settles the order."""
        first = [r["file_path"] for r in find_macro_refs(macro_db, CH, "GAP_LIMIT", limit=None)]
        again = [r["file_path"] for r in find_macro_refs(macro_db, CH, "GAP_LIMIT", limit=None)]
        assert first == again
        assert first == sorted(first), f"rank tied, so path must order: {first}"


class TestVirtualCallersPaging:
    """The virtual-dispatch fallback pages like every other answer."""

    @pytest.fixture
    def virtual_db(self, db):
        """A base method with two overrides; one override has no caller.

        ``find_callers`` on the caller-less override falls back to the
        peers, and that answer used to come back unordered, unbounded by
        any offset and with no page notice.
        """
        fid = upsert_file(db, CH, "src/dl.cpp", "cpp")
        insert_symbols_batch(db, [
            _symbol_row(fid, "src/dl.cpp", "stop", "Base::stop", "u_base", 10),
            _symbol_row(fid, "src/dl.cpp", "stop", "Sd::stop", "u_sd", 20),
            _symbol_row(fid, "src/dl.cpp", "stop", "Flash::stop", "u_flash", 30),
            _symbol_row(fid, "src/dl.cpp", "run", "App::run", "u_app", 40, kind="function"),
        ])
        db.executemany(
            "INSERT INTO overrides(config_hash, derived_usr, base_usr) VALUES (?, ?, ?)",
            [(CH, "u_sd", "u_base"), (CH, "u_flash", "u_base")],
        )
        # Seven call sites on the peer override, none on u_flash itself.
        insert_refs_batch(db, [
            (CH, "u_sd", "src/dl.cpp", 400 + i, "u_app", "call", None)
            for i in range(7)
        ])
        db.commit()
        return db

    def test_it_reports_the_total_beside_the_page(self, virtual_db):
        rows, total = _resolve_virtual_callers(
            virtual_db, CH, "u_flash", Path("/tmp/test"), ref_kind=["call"], limit=3,
        )
        assert total == 7, "the count must describe the whole answer"
        assert len(rows) == 3

    def test_two_pages_neither_overlap_nor_skip(self, virtual_db):
        """The query had no ORDER BY at all, thus a walk over it was a guess."""
        seen: list = []
        for offset in (0, 3, 6):
            rows, total = _resolve_virtual_callers(
                virtual_db, CH, "u_flash", Path("/tmp/test"),
                ref_kind=["call"], limit=3, offset=offset,
            )
            seen += [(r["file"], r["line"]) for r in rows]
        assert len(seen) == 7, f"walked {len(seen)} rows"
        assert len(set(seen)) == 7, "a row came back twice"

    def test_an_offset_past_the_end_keeps_the_count(self, virtual_db):
        rows, total = _resolve_virtual_callers(
            virtual_db, CH, "u_flash", Path("/tmp/test"),
            ref_kind=["call"], limit=3, offset=99,
        )
        assert rows == []
        assert total == 7

    def test_a_symbol_with_no_overrides_still_answers_none(self, virtual_db):
        """None means "not a virtual method", and it must stay distinct."""
        assert _resolve_virtual_callers(
            virtual_db, CH, "u_app", Path("/tmp/test"), ref_kind=["call"],
        ) is None
