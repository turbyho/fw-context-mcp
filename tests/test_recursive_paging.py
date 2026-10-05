"""find_all_callers_recursive and find_callees_recursive page their answer.

The two tools cut the answer at ``limit`` and said nothing about the cut.
A hot function at depth 5 can have hundreds of transitive callers, where
the default page is 50.  They now take an ``offset`` and lead with the
page notice of the other paged tools.

The order used to stop at ``depth, name``.  Two static functions of one
name, or one caller of two same-name targets, tie there, and SQLite may
give tied rows in any order.  The fixture builds exactly those ties, thus
a walk over an order without a unique tie-break repeats or skips a row.
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
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
    walk_recursive,
)
from fw_context_mcp.mcp.handlers.callgraph import _recursive_page
from tests._paging import assert_whole_and_once, notice, walk_pages

CH = "hash-deadbeef"
ROOT = Path("/tmp/project")

# Direct callers of ``hub``: twelve static functions that all carry the
# name ``cb``, one in each file.  They tie on depth AND on name.
DIRECT = 12
# Callers at depth 2: each ``cb`` has one caller of its own name.
INDIRECT = DIRECT


def _symbol(file_id: int, path: str, name: str, qname: str, usr: str, line: int) -> tuple:
    """One row in the column order of ``insert_symbols_batch``."""
    return (
        CH, file_id, path, name, usr, name, qname, "function",
        line, 1, line + 5, 1, f"void {name}(void)", "", None, 0, 0, "",
        0, "", 1, 0.0, "", 0,
    )


def _call(to_usr: str, path: str, line: int, from_usr: str) -> tuple:
    return (CH, to_usr, path, line, from_usr, "call", None)


@pytest.fixture
def db():
    tmpdir = Path(tempfile.mkdtemp())
    conn = open_db(tmpdir / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", str(ROOT))
        upsert_build_config(conn, CH, "proj-001", "/tmp/compile_commands.json")
    hub_file = upsert_file(conn, CH, "src/hub.c", "c")
    symbols = [_symbol(hub_file, "src/hub.c", "hub", "hub", "c:@F@hub", 1)]
    refs = []
    for i in range(DIRECT):
        path = f"src/cb{i:02d}.c"
        fid = upsert_file(conn, CH, path, "c")
        cb_usr = f"c:cb{i:02d}.c@F@cb"
        top_usr = f"c:@F@top{i:02d}"
        symbols.append(_symbol(fid, path, "cb", "cb", cb_usr, 10))
        symbols.append(_symbol(fid, path, f"top{i:02d}", f"top{i:02d}", top_usr, 20))
        refs.append(_call("c:@F@hub", path, 11, cb_usr))
        refs.append(_call(cb_usr, path, 21, top_usr))
    insert_symbols_batch(conn, symbols)
    insert_refs_batch(conn, refs)
    conn.commit()
    yield conn
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _key(row: dict) -> tuple:
    return (row["file_path"] if "file_path" in row else row["file"], row["name"], row["depth"])


def _walk_db(db, name: str, direction: str, page: int) -> tuple[list[tuple], int]:
    """Walk the pages of ``walk_recursive``; the total must not change between pages."""
    seen: list[tuple] = []
    offset = 0
    totals: set[int] = set()
    while True:
        rows, total = walk_recursive(db, CH, name, direction=direction, limit=page, offset=offset)
        totals.add(total)
        if not rows:
            break
        seen += [_key(r) for r in rows]
        offset += len(rows)
    assert len(totals) == 1, f"the total changed between pages: {totals}"
    return seen, totals.pop()


class TestWalkRecursive:
    @pytest.mark.parametrize("page", [1, 5, 7, 50])
    def test_the_callers_walk_whole_and_once(self, db, page):
        seen, total = _walk_db(db, "hub", "callers", page)

        assert total == DIRECT + INDIRECT
        assert_whole_and_once(seen, total)

    def test_the_order_ends_at_the_key_of_the_answer(self):
        """The tie-break, held as an invariant because no test can force it.

        The twelve ``cb`` rows tie on depth and on name.  Measured, SQLite
        gives them in ``usr`` order anyway: the GROUP BY of ``dedup`` hands
        them over sorted, and the sort keeps that order.  Neither a walk
        nor ``PRAGMA reverse_unordered_selects`` made the order without the
        tie-break fail, thus a change of the query plan is the only way to
        the fault.  What holds either way: the order must END at
        ``usr, target``, the key of one row of the answer.
        """
        from fw_context_mcp.indexer.db._callgraph import _WALK_ORDER

        assert _WALK_ORDER.endswith("d.usr, d.target"), _WALK_ORDER

    def test_the_depth_leads_the_order(self, db):
        rows, _ = walk_recursive(db, CH, "hub", direction="callers", limit=100)
        assert [r["depth"] for r in rows] == [1] * DIRECT + [2] * INDIRECT

    def test_the_callees_walk_whole_and_once(self, db):
        seen, total = _walk_db(db, "top00", "callees", 1)

        assert total == 2  # cb, then hub
        assert_whole_and_once(seen, total)

    def test_a_page_after_the_end_still_knows_the_total(self, db):
        rows, total = walk_recursive(db, CH, "hub", direction="callers", limit=5, offset=500)
        assert rows == []
        assert total == DIRECT + INDIRECT

    def test_the_max_depth_bounds_the_total(self, db):
        _, total = walk_recursive(db, CH, "hub", direction="callers", max_depth=1, limit=5)
        assert total == DIRECT


class TestRecursivePage:
    def _page(self, db, offset: int, limit: int = 5, max_depth: int = 5) -> list[dict]:
        return _recursive_page(
            db, CH, ROOT, "find_all_callers_recursive", "callers",
            "hub", max_depth, limit, offset,
        )

    def test_the_handler_walks_whole_and_once(self, db):
        seen, total = walk_pages(lambda offset: self._page(db, offset), _key)
        assert total == DIRECT + INDIRECT
        assert_whole_and_once(seen, total)

    def test_the_notice_leads_and_the_hint_names_the_next_call(self, db):
        rows = self._page(db, 0)
        assert notice(rows) is rows[0]
        assert rows[0]["hint"] == "find_all_callers_recursive('hub', offset=5) reads the next page."

    def test_a_depth_that_is_not_the_default_goes_into_the_hint(self, db):
        rows = self._page(db, 0, limit=3, max_depth=1)
        assert rows[0]["hint"] == (
            "find_all_callers_recursive('hub', max_depth=1, offset=3) reads the next page."
        )

    def test_the_hint_default_is_the_default_of_the_tools(self):
        """The hint leaves out the default depth; the defaults must agree."""
        import inspect

        from fw_context_mcp.mcp.handlers import callgraph

        for tool in (callgraph.find_all_callers_recursive, callgraph.find_callees_recursive):
            default = inspect.signature(tool).parameters["max_depth"].default
            assert default == callgraph._DEFAULT_RECURSIVE_DEPTH, tool.__name__

    def test_a_page_after_the_end_names_the_total(self, db):
        assert self._page(db, 500) == [
            {"info": f"No caller at offset 500; the answer holds {DIRECT + INDIRECT}."}
        ]

    def test_a_symbol_with_no_caller_says_so(self, db):
        rows = _recursive_page(
            db, CH, ROOT, "find_all_callers_recursive", "callers", "top00", 5, 5, 0,
        )
        assert rows == [{"info": "No callers found for 'top00'."}]

    def test_an_ambiguous_name_keeps_its_warning_first_and_out_of_the_count(self, db):
        """Twelve symbols are named ``cb``: the warning leads, the notice follows it."""
        rows = _recursive_page(
            db, CH, ROOT, "find_all_callers_recursive", "callers", "cb", 5, 5, 0,
        )
        assert set(rows[0]) == {"warning"}
        assert notice(rows) is rows[1]
        assert rows[1]["shown"] == len(rows) - 2 == 5
        assert rows[1]["total"] == DIRECT  # each top<i> calls one cb

    def test_the_rows_carry_an_absolute_file(self, db):
        rows = self._page(db, 0)
        assert all(r["file"].startswith(str(ROOT)) for r in rows[1:])
