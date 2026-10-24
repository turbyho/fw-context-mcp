"""One row of refs is one reference site of one caller, with each analysis that found it.

The key of a row is the target, the file, the line, the column, the kind,
the slot and the caller (``idx_refs_site``).  The earlier key had no column
and no slot: two calls on one line were one row, two slots of one table
that hold one function inside a body were one row, and the repair of the
caller removed a slot in a second run.
"""

from __future__ import annotations

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
)
from fw_context_mcp.indexer.models import Reference, RefOrigin, origin_names
from tests.test_same_name_disambiguation import CH, _symbol_row


@pytest.fixture
def conn(tmp_path: Path):
    db = open_db(tmp_path / "index.db")
    with transaction(db):
        upsert_project(db, "pid", "p", str(tmp_path))
        upsert_build_config(db, CH, "pid", "cc.json")
    yield db
    db.close()


def _rows(conn) -> list[tuple]:
    return [
        tuple(row) for row in conn.execute(
            "SELECT to_usr, from_line, from_col, from_usr, ref_kind, slot_index, origins FROM refs "
            "WHERE config_hash = ? ORDER BY from_line, from_col, ifnull(slot_index, -1), ifnull(from_usr, '')",
            (CH,),
        )
    ]


class TestTheKeyOfASite:
    def test_two_calls_on_one_line_are_two_rows(self, conn):
        insert_refs_batch(conn, [
            (CH, "u_tick", "main.c", 2, "u_twice", "call", None, 5, int(RefOrigin.DIRECT)),
            (CH, "u_tick", "main.c", 2, "u_twice", "call", None, 13, int(RefOrigin.DIRECT)),
        ])
        assert [row[2] for row in _rows(conn)] == [5, 13]

    def test_a_second_analysis_of_one_site_adds_its_origin(self, conn):
        """One site that two analyses find is one row: a second row would count the call twice."""
        site = (CH, "u_cb", "drv.cpp", 8, "u_attach", "indirect", None, 22)
        insert_refs_batch(conn, [(*site, int(RefOrigin.DIRECT))])
        insert_refs_batch(conn, [(*site, int(RefOrigin.TEXT))])
        (row,) = _rows(conn)
        assert origin_names(row[6]) == ["direct", "text"]

    def test_two_slots_of_one_function_inside_a_body_stay_two_rows(self, conn):
        """The earlier key had no slot: ``{ reset, reset }`` in a body lost slot 1."""
        insert_refs_batch(conn, [
            (CH, "u_reset", "vec.c", 4, "u_init", "indirect", 0, 15, int(RefOrigin.DIRECT)),
            (CH, "u_reset", "vec.c", 4, "u_init", "indirect", 1, 15, int(RefOrigin.DIRECT)),
        ])
        assert [row[5] for row in _rows(conn)] == [0, 1]

    def test_two_callers_of_one_macro_site_stay_two_rows(self, conn):
        """A macro that defines two functions on one line gives each a call at one place."""
        insert_refs_batch(conn, [
            (CH, "u_link", "w5100.h", 193, "u_readMR", "call", None, 1, int(RefOrigin.DIRECT)),
            (CH, "u_link", "w5100.h", 193, "u_writeMR", "call", None, 1, int(RefOrigin.DIRECT)),
        ])
        assert [row[3] for row in _rows(conn)] == ["u_readMR", "u_writeMR"]


@pytest.mark.libclang
def test_the_walk_keeps_both_callers_of_a_macro_site(tmp_path: Path):
    """The shape of a register macro: one line of the source defines a reader and a writer."""
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "regs.c"
    src.write_text(
        "int link(void);\n"
        "#define REG(name) int read##name(void) { return link(); } int write##name(void) { return link(); }\n"
        "REG(MR)\n",
        encoding="utf-8",
    )
    unit = CompilationUnit(file=src, directory=tmp_path, language="c", clang_args=["-std=c11"])
    calls = [r for r in extract_all(unit, with_refs=True).references if r.ref_kind == "call"]
    assert sorted(r.from_usr for r in calls) == ["c:@F@readMR", "c:@F@writeMR"]
    assert {(r.from_line, r.from_col) for r in calls} == {(3, 1)}


@pytest.mark.libclang
def test_one_reference_in_nested_calls_is_one_row(tmp_path: Path):
    """``reg(wrap(f))``: the walk claims ``f`` for ``reg`` and for ``wrap``.

    The row of a reference is keyed on the place of the expression that
    names the target, thus the two claims are one row.  With the place of
    the enclosing call they were two rows, and ``find_callers`` counted the
    reference twice.
    """
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "nested.c"
    src.write_text(
        "typedef void (*cb_t)(void);\n"
        "void f(void) {}\n"
        "cb_t wrap(cb_t c) { return c; }\n"
        "void reg(cb_t c) { (void)c; }\n"
        "void g(void) { reg(wrap(f)); }\n"
        "void h(void) { reg(f); reg(f); }\n",
        encoding="utf-8",
    )
    unit = CompilationUnit(file=src, directory=tmp_path, language="c", clang_args=["-std=c11"])
    sites = sorted(
        (r.from_usr, r.from_line, r.from_col)
        for r in extract_all(unit, with_refs=True).references
        if r.ref_kind == "indirect" and r.to_usr == "c:@F@f"
    )
    assert sites == [("c:@F@g", 5, 25), ("c:@F@h", 6, 20), ("c:@F@h", 6, 28)]


class TestTheRepairOfTheCaller:
    @pytest.fixture
    def gaps(self, conn):
        fid = upsert_file(conn, CH, "vec.c", "c")
        insert_symbols_batch(conn, [_symbol_row(fid, "vec.c", "init", "init", "u_init", 2, 10, kind="function")])
        # Two slots of one function in one body, both with no caller: the gap
        # of the walk that the repair fills.
        insert_refs_batch(conn, [
            (CH, "u_reset", "vec.c", 4, None, "indirect", 0, 15, int(RefOrigin.DIRECT)),
            (CH, "u_reset", "vec.c", 4, None, "indirect", 1, 15, int(RefOrigin.DIRECT)),
        ])
        conn.commit()
        return conn

    def test_each_slot_gets_the_caller(self, gaps):
        from fw_context_mcp.indexer._postprocess import _step_repair_from_usr

        _step_repair_from_usr(gaps, {"config_hash": CH})
        assert [(row[3], row[5]) for row in _rows(gaps)] == [("u_init", 0), ("u_init", 1)]

    def test_a_second_run_keeps_both_slots(self, gaps):
        """The earlier twin match ignored the slot and deleted the gap row of slot 1."""
        from fw_context_mcp.indexer._postprocess import _step_repair_from_usr

        _step_repair_from_usr(gaps, {"config_hash": CH})
        # The next index run writes the rows of the walk again, with the gap.
        insert_refs_batch(gaps, [
            (CH, "u_reset", "vec.c", 4, None, "indirect", 0, 15, int(RefOrigin.DIRECT)),
            (CH, "u_reset", "vec.c", 4, None, "indirect", 1, 15, int(RefOrigin.DIRECT)),
        ])
        _step_repair_from_usr(gaps, {"config_hash": CH})
        assert [(row[3], row[5]) for row in _rows(gaps)] == [("u_init", 0), ("u_init", 1)]


def test_the_migration_keeps_the_slots_of_an_older_index(tmp_path: Path):
    """The earlier migration grouped NULL callers as equal and kept one slot of a table."""
    import sqlite3

    db = tmp_path / "index.db"
    conn = open_db(db, skip_integrity_check=True)
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, CH, "pid", "cc.json")
    insert_refs_batch(conn, [
        (CH, "u_reset", "vec.c", 4, None, "indirect", 0, 0, 0),
        (CH, "u_reset", "vec.c", 4, None, "indirect", 1, 0, 0),
    ])
    conn.commit()
    conn.close()
    raw = sqlite3.connect(db)
    raw.execute("DROP INDEX idx_refs_site")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    migrated = open_db(db, skip_integrity_check=True)
    try:
        assert [row[5] for row in _rows(migrated)] == [0, 1]
    finally:
        migrated.close()


class TestTheClaimsOfOneUnit:
    def _ref(self, **changes) -> Reference:
        fields = {
            "to_usr": "u_cb", "from_file": "drv.cpp", "from_line": 8, "from_usr": "u_attach",
            "ref_kind": "indirect", "slot_index": None, "from_col": 22, "origins": RefOrigin.DIRECT,
        }
        fields.update(changes)
        return Reference(**fields)

    def test_a_text_reference_joins_an_earlier_site_of_its_line(self):
        """The scan of the source text has no column: it joins a site of the line that is known."""
        from fw_context_mcp.indexer.symbols import _claim_ref, _claim_text_ref

        refs: list[Reference] = []
        seen: dict = {}
        _claim_ref(refs, seen, self._ref())
        assert not _claim_text_ref(refs, seen, self._ref(from_col=0, origins=RefOrigin.TEXT))
        assert len(refs) == 1 and origin_names(refs[0].origins) == ["direct", "text"]

    def test_a_text_reference_of_another_caller_is_a_row_of_its_own(self):
        from fw_context_mcp.indexer.symbols import _claim_ref, _claim_text_ref

        refs: list[Reference] = []
        seen: dict = {}
        _claim_ref(refs, seen, self._ref())
        assert _claim_text_ref(refs, seen, self._ref(from_col=0, from_usr="u_other", origins=RefOrigin.TEXT))
        assert len(refs) == 2

    def test_a_nested_list_joins_the_slot_of_its_element(self):
        """The enclosing list gives the column of the element, the nested list the column of the function."""
        from fw_context_mcp.indexer.symbols import _claim_ref

        refs: list[Reference] = []
        seen: dict = {}
        _claim_ref(refs, seen, self._ref(slot_index=0, from_col=26))
        assert not _claim_ref(refs, seen, self._ref(slot_index=None, from_col=31))
        assert [r.slot_index for r in refs] == [0]


class TestTheReaders:
    @pytest.fixture
    def calls(self, conn):
        fid = upsert_file(conn, CH, "main.c", "c")
        insert_symbols_batch(conn, [
            _symbol_row(fid, "main.c", "tick", "tick", "u_tick", 1, 1, kind="function"),
            _symbol_row(fid, "main.c", "twice", "twice", "u_twice", 2, 3, kind="function"),
        ])
        # Written out of the order of the source: the reader gives that order.
        insert_refs_batch(conn, [
            (CH, "u_tick", "main.c", 2, "u_twice", "call", None, 13, int(RefOrigin.DIRECT | RefOrigin.TEXT)),
            (CH, "u_tick", "main.c", 2, "u_twice", "call", None, 5, int(RefOrigin.DIRECT)),
        ])
        conn.commit()
        return conn

    def test_two_calls_on_one_line_come_in_the_order_of_the_columns(self, calls):
        from fw_context_mcp.indexer.db._refs import refs_for_symbol

        rows = refs_for_symbol(calls, CH, "u_tick", "function", ref_kind="call")
        assert [(r["from_line"], r["from_col"]) for r in rows] == [(2, 5), (2, 13)]
        assert [origin_names(r["origins"]) for r in rows] == [["direct"], ["direct", "text"]]

    def test_a_class_row_joins_the_origins_of_its_members(self, conn):
        """A class answers at member granularity: one row per caller and line, with each origin."""
        from fw_context_mcp.indexer.db._refs import refs_for_symbol

        fid = upsert_file(conn, CH, "dev.cpp", "cpp")
        insert_symbols_batch(conn, [
            _symbol_row(fid, "dev.cpp", "Dev", "Dev", "c:@S@Dev", 1, 9, kind="class"),
            _symbol_row(fid, "dev.cpp", "run", "run", "u_run", 10, 20, kind="function"),
        ])
        insert_refs_batch(conn, [
            (CH, "c:@S@Dev@F@a#", "dev.cpp", 12, "u_run", "call", None, 9, int(RefOrigin.DIRECT)),
            (CH, "c:@S@Dev@F@b#", "dev.cpp", 12, "u_run", "call", None, 4, int(RefOrigin.TEXT)),
        ])
        conn.commit()
        (row,) = refs_for_symbol(conn, CH, "c:@S@Dev", "class", ref_kind="call")
        assert row["from_col"] == 4
        assert origin_names(row["origins"]) == ["direct", "text"]
