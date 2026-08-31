"""Reading an array of function addresses back out of the index.

The assembly reader finds a vector table written as address words.  A build
that generates its table in C writes the same thing as an array
initializer, and until now nobody joined "line 11 of isr_tables.c" to
"slot 0".  These tests drive the whole path — parse, store, query — because
the join that ties an element to its array happens in SQL and a test
against the extraction alone would not touch it.
"""
import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.compile_commands import CompilationUnit
from fw_context_mcp.indexer.db import (
    get_function_address_arrays,
    insert_refs_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_project,
)
from fw_context_mcp.indexer.ops import store_symbols_for_unit

pytestmark = pytest.mark.libclang

CONFIG = "testhash"


def _open(tmp_path, *hashes):
    """Open a fresh index that holds one project and the given builds."""
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        for config_hash in hashes:
            upsert_build_config(conn, config_hash, "pid", "cc.json")
    return conn


def _index(tmp_path, code, filename="isr_tables.c"):
    """Store *code* the way the indexer does, and return the connection.

    This goes through ``store_symbols_for_unit`` rather than building rows
    by hand, so the row that ``ops`` writes for each reference — the one
    that has to carry the slot — is part of what the test covers.
    """
    src = tmp_path / filename
    src.write_text(code, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                           clang_args=["-std=c11"])

    conn = _open(tmp_path, CONFIG)
    store_symbols_for_unit(conn, unit, CONFIG, tmp_path, index_refs=True)
    conn.commit()
    return conn


def _slots(conn):
    """Return {table name: {slot: target name}} for the recognised arrays."""
    out: dict[str, dict] = {}
    for row in get_function_address_arrays(conn, CONFIG):
        out.setdefault(row["table_name"], {})[row["slot"]] = row["name"]
    return out


def test_a_generated_table_reads_back_slot_by_slot(tmp_path):
    """The Zephyr shape: addresses cast to an integer array.

    ``_irq_vector_table`` is ``const uintptr_t[]``, not an array of function
    pointers, and the entries are casts.  This is the shape that the tool
    could not read at all, so it is the one that must work.
    """
    conn = _index(tmp_path, """
typedef unsigned long uintptr_t;
void _isr_wrapper(void); void z_arm_exc_spurious(void);
const uintptr_t _irq_vector_table[3] = {
    ((uintptr_t)&z_arm_exc_spurious),
    ((uintptr_t)&_isr_wrapper),
    ((uintptr_t)&_isr_wrapper),
};
""")
    try:
        assert _slots(conn) == {"_irq_vector_table": {
            0: "z_arm_exc_spurious",
            1: "_isr_wrapper",
            2: "_isr_wrapper",
        }}
    finally:
        conn.close()


def test_a_table_of_function_pointers_reads_back_too(tmp_path):
    """The plain shape, without a cast, is the same construct."""
    conn = _index(tmp_path, """
void reset(void); void nmi(void); void hard(void);
void (*const vectors[])(void) = { reset, nmi, hard };
""")
    try:
        assert _slots(conn) == {"vectors": {0: "reset", 1: "nmi", 2: "hard"}}
    finally:
        conn.close()


def test_each_array_keeps_its_own_slot_numbers(tmp_path):
    """Two tables in one file both start at slot 0.

    Without the array on every row a reader would see two slot 0 entries
    and no way to tell which table each belongs to.
    """
    conn = _index(tmp_path, """
void isr_a(void); void isr_b(void);
void step_x(void); void step_y(void);
void (*const vectors[])(void) = {
    isr_a,
    isr_b,
};
void (*const fsm_steps[])(void) = {
    step_x,
    step_y,
};
""")
    try:
        assert _slots(conn) == {
            "vectors": {0: "isr_a", 1: "isr_b"},
            "fsm_steps": {0: "step_x", 1: "step_y"},
        }
    finally:
        conn.close()


def test_a_hole_leaves_a_gap_in_the_slot_numbers(tmp_path):
    """An unused entry has no address, so its slot is simply absent."""
    conn = _index(tmp_path, """
void reset(void); void hard(void);
void (*const vectors[])(void) = { reset, 0, 0, hard };
""")
    try:
        assert _slots(conn) == {"vectors": {0: "reset", 3: "hard"}}
    finally:
        conn.close()


def test_a_designated_table_is_not_reported(tmp_path):
    """No slot was recorded for it, so the reader has nothing to report.

    This is the deliberate consequence of refusing to guess: the elements
    are known, their positions are not, and a table with invented positions
    would be worse than one the tool stays quiet about.
    """
    conn = _index(tmp_path, """
void svc(void); void hard(void);
void (*const vectors[16])(void) = { [11] = svc, [3] = hard };
""")
    try:
        assert _slots(conn) == {}
    finally:
        conn.close()


def test_an_assignment_is_not_a_table(tmp_path):
    """An indirect reference without a slot must not become slot 0."""
    conn = _index(tmp_path, """
void handler(void);
void (*cb)(void);
void install(void) { cb = handler; }
""")
    try:
        assert _slots(conn) == {}
    finally:
        conn.close()


def test_two_arrays_on_one_line_report_neither(tmp_path):
    """When a line holds two arrays, ownership is not knowable from it.

    The range join asks which array's extent covers the line of the
    element.  Two single-line arrays on the same line both answer, and
    naming the wrong one is as wrong as naming the wrong slot — so the
    elements they disagree about are dropped.
    """
    conn = _index(tmp_path, """
void a(void); void b(void);
void (*const t1[])(void) = { a }; void (*const t2[])(void) = { b };
""")
    try:
        assert _slots(conn) == {}
    finally:
        conn.close()


def test_an_index_without_slots_reports_nothing(tmp_path):
    """An index written before slots were recorded must answer empty.

    Reading it is not an error — every C reference in it carries NULL, and
    the reader has to say "no array recognised" rather than fail.
    """
    from fw_context_mcp.indexer.models import Reference

    conn = _open(tmp_path, CONFIG)
    try:
        insert_refs_batch(conn, [
            (CONFIG, r.to_usr, r.from_file, r.from_line, r.from_usr,
             r.ref_kind, r.slot_index)
            for r in (
                Reference("c:@F@reset", "v.c", 3, None, "indirect", None),
                Reference("c:@F@nmi", "v.c", 4, None, "indirect", None),
            )
        ])
        conn.commit()
        assert get_function_address_arrays(conn, CONFIG) == []
    finally:
        conn.close()


def test_another_build_does_not_leak_its_table(tmp_path):
    """Slots are read for one configuration only.

    Five images of one Zephyr project each hold their own
    ``_irq_vector_table`` of a different length, so a reader that ignored
    config_hash would merge tables of different boards.
    """
    conn = _index(tmp_path, """
void isr_a(void); void isr_b(void);
void (*const vectors[])(void) = { isr_a, isr_b };
""")
    try:
        with transaction(conn):
            upsert_build_config(conn, "otherhash", "pid", "cc.json")
        assert get_function_address_arrays(conn, "otherhash") == []
        assert len(get_function_address_arrays(conn, CONFIG)) == 2
    finally:
        conn.close()


def test_every_row_names_the_target_and_the_position(tmp_path):
    """One row must carry both halves a reader needs.

    The target says what runs; the array and the line say where the
    address sits.  A row missing either is not usable on its own.
    """
    conn = _index(tmp_path, """
void reset(void);
void (*const vectors[])(void) = { reset };
""")
    try:
        rows = get_function_address_arrays(conn, CONFIG)
        assert len(rows) == 1
        row = rows[0]
        assert row["slot"] == 0
        assert row["name"] == "reset"
        assert row["kind"] == "function"
        assert row["table_name"] == "vectors"
        assert row["table_usr"].endswith("vectors")
        assert row["file"].endswith("isr_tables.c")
        assert row["table_file"].endswith("isr_tables.c")
        assert row["line"] == 2
        assert row["table_line"] == 3
    finally:
        conn.close()
