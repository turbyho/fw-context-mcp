"""Which slots of a table hold no name, and why that cuts both ways.

An array can be longer than the number of slots the index can name.
Measured on the Zephyr image of zbox-ecb-fw-v5: ``_sw_isr_table`` is
declared ``[290]``, 284 slots name ``z_irq_spurious``, and the 6 with no
name — 89, 198, 219, 228, 269, 270 — are the interrupts the firmware
actually services, because ``gen_isr_tables.py`` wrote a resolved address
there instead of a symbol.

So a hole means opposite things in the two real tables, and these tests pin
that the reader is told where to look without being told what it means.
"""
import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.db import (
    get_table_coverage,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.indexer.db._callgraph import _declared_length
from fw_context_mcp.utils import format_number_ranges as _as_ranges

CONFIG = "ch"


def _db(tmp_path):
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, CONFIG, "pid", "cc.json")
    return conn


def _table(conn, name, signature, filled, table_file="isr_tables.c"):
    """One array with *signature*, naming a function in each *filled* slot."""
    with transaction(conn):
        fid = upsert_file(conn, CONFIG, table_file, "c")
        insert_symbols_batch(conn, [
            (CONFIG, fid, table_file, name, f"c:@{name}", name, name,
             "varglobal", 10, 1, 10 + len(filled) + 2, 1, signature, "",
             None, 0, 0, "", 0, "", 1, 0.0, "", 0),
            *[
                (CONFIG, fid, "handlers.c", f"h{slot}", f"c:@F@h{slot}",
                 f"h{slot}", f"h{slot}", "function", 100 + index, 1,
                 102 + index, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0)
                for index, slot in enumerate(filled)
            ],
        ])
        insert_refs_batch(conn, [
            (CONFIG, f"c:@F@h{slot}", table_file, 11 + slot, None,
             "indirect", slot)
            for slot in filled
        ])


def test_the_zephyr_shape_names_the_slots_in_use(tmp_path):
    """The measured case, in miniature.

    A table of 8 with 6 named leaves 2 unnamed, and on this shape those two
    are the vectors that ARE serviced — the build resolved their addresses.
    """
    conn = _db(tmp_path)
    try:
        _table(conn, "_sw_isr_table", "const struct _isr_table_entry[8] _sw_isr_table",
               filled=[0, 1, 2, 3, 5, 7])

        coverage = get_table_coverage(conn, CONFIG)
        assert len(coverage) == 1
        assert coverage[0]["table_name"] == "_sw_isr_table"
        assert coverage[0]["declared"] == 8
        assert coverage[0]["named"] == 6
        assert coverage[0]["missing"] == "4, 6"
    finally:
        conn.close()


def test_a_fully_named_table_is_not_reported(tmp_path):
    """Nothing to add about a table the index read completely."""
    conn = _db(tmp_path)
    try:
        _table(conn, "vectors", "void (*const[3])(void) vectors",
               filled=[0, 1, 2])

        assert get_table_coverage(conn, CONFIG) == []
    finally:
        conn.close()


def test_a_length_that_cannot_be_read_is_not_guessed(tmp_path):
    """Two bracketed numbers make the count ambiguous.

    Reporting the wrong length would turn into a wrong list of slots, which
    is worse than no coverage row at all.
    """
    conn = _db(tmp_path)
    try:
        _table(conn, "grid", "void (*const[2][4])(void) grid", filled=[0, 1])

        assert get_table_coverage(conn, CONFIG) == []
    finally:
        conn.close()


def test_an_empty_index_answers_empty(tmp_path):
    conn = _db(tmp_path)
    try:
        assert get_table_coverage(conn, CONFIG) == []
    finally:
        conn.close()


def test_each_table_is_measured_on_its_own(tmp_path):
    """Two tables in one build do not pool their slots."""
    conn = _db(tmp_path)
    try:
        _table(conn, "table_a", "void (*const[4])(void) table_a",
               filled=[0, 1], table_file="a.c")
        _table(conn, "table_b", "void (*const[3])(void) table_b",
               filled=[0], table_file="b.c")

        coverage = {c["table_name"]: (c["declared"], c["named"], c["missing"])
                    for c in get_table_coverage(conn, CONFIG)}
        assert coverage == {
            "table_a": (4, 2, "2-3"),
            "table_b": (3, 1, "1-2"),
        }
    finally:
        conn.close()


class TestDeclaredLength:
    """The count comes from the resolved type, never from the source text."""

    @pytest.mark.parametrize(("signature", "expected"), [
        ("const struct _isr_table_entry[290] _sw_isr_table", 290),
        ("const uintptr_t[290] _irq_vector_table", 290),
        ("void (*const[2])(void) vectors", 2),
        ("const uintptr_t[48] _irq_vector_table", 48),
    ])
    def test_a_real_signature(self, signature, expected):
        """Every one of these was read out of a real index."""
        assert _declared_length(signature) == expected

    @pytest.mark.parametrize("signature", [
        "void (*const[2][4])(void) grid",          # two dimensions
        "void (*const[2])(int[3]) with_array_arg",  # a parameter is an array
        "int scalar",                               # no array at all
        "",
    ])
    def test_an_ambiguous_or_absent_count(self, signature):
        assert _declared_length(signature) is None


class TestRanges:
    """284 missing slots must stay readable."""

    @pytest.mark.parametrize(("numbers", "expected"), [
        ([4, 6], "4, 6"),
        ([0, 1, 2], "0-2"),
        ([89, 198, 219, 228, 269, 270], "89, 198, 219, 228, 269-270"),
        ([7], "7"),
        ([0, 1, 3, 4, 5, 9], "0-1, 3-5, 9"),
    ])
    def test_folding(self, numbers, expected):
        assert _as_ranges(numbers) == expected

    def test_a_long_run_stays_short(self):
        """The measured worst case: a table of 290 with 6 named."""
        named = {89, 198, 219, 228, 269, 270}
        missing = sorted(set(range(290)) - named)
        folded = _as_ranges(missing)
        assert folded == "0-88, 90-197, 199-218, 220-227, 229-268, 271-289"
        assert len(folded) < 80, "must fit on a line"
