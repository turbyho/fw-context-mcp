"""The slot an indirect reference carries when it comes from an init list.

A table of function addresses is a vector table whether an assembler wrote
it or a generator emitted C.  To read the C form, the indexer must know
WHICH element of the array holds each address — the reference alone only
says which line.  These tests pin the four conditions under which a
position is an array index, and pin the absence of a slot everywhere else,
because a wrong slot number in front of a reader is worse than none.
"""
import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.compile_commands import CompilationUnit
from fw_context_mcp.indexer.db import insert_refs_batch, open_db

pytestmark = pytest.mark.libclang


def _slots(tmp_path, code, language="c", std="c11"):
    """Return {function name: {slot, ...}} for the indirect refs in *code*."""
    from fw_context_mcp.indexer.symbols import extract_all

    suffix = ".c" if language == "c" else ".cpp"
    src = tmp_path / f"table{suffix}"
    src.write_text(code, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language=language,
                           clang_args=[f"-std={std}"])
    result = extract_all(unit, with_refs=True)
    found: dict[str, set] = {}
    for ref in result.references:
        if ref.ref_kind != "indirect":
            continue
        found.setdefault(ref.to_usr.split("@")[-1], set()).add(ref.slot_index)
    return found


def test_flat_array_numbers_its_elements(tmp_path):
    """The ARM vector table shape: the position in the list is the slot."""
    slots = _slots(tmp_path, """
void reset(void); void nmi(void); void hard(void);
void (*const vectors[])(void) = { reset, nmi, hard };
""")
    assert slots == {"reset": {0}, "nmi": {1}, "hard": {2}}


def test_one_element_per_line_still_counts_by_position(tmp_path):
    """A generated table puts one entry per line; the line is not the slot."""
    slots = _slots(tmp_path, """
void reset(void); void nmi(void); void hard(void);
void (*const vectors[])(void) = {
    reset,
    nmi,
    hard,
};
""")
    assert slots == {"reset": {0}, "nmi": {1}, "hard": {2}}


def test_a_hole_keeps_the_following_positions_right(tmp_path):
    """An unused slot written as 0 still occupies its index.

    This is the reason the count comes from the element position and not
    from a running tally of the addresses found.
    """
    slots = _slots(tmp_path, """
void reset(void); void hard(void);
void (*const vectors[])(void) = { reset, 0, 0, hard };
""")
    assert slots == {"reset": {0}, "hard": {3}}


def test_array_of_structs_reports_the_outer_index(tmp_path):
    """The Zephyr _sw_isr_table shape: a slot is a struct, not an address.

    The slot of ``isr_b`` is 1 because it is the second ENTRY — not because
    ``fn`` is the second field of the entry.  Both numbers are 1 here by
    coincidence, so ``isr_a`` at slot 0 is what proves the difference: its
    field position is also 1, and it must not be reported.
    """
    slots = _slots(tmp_path, """
void isr_a(void); void isr_b(void);
struct entry { const void *arg; void (*fn)(void); };
struct entry table[] = { { 0, isr_a }, { 0, isr_b } };
""")
    assert slots == {"isr_a": {0}, "isr_b": {1}}


def test_a_designated_initializer_reports_no_slot(tmp_path):
    """``[11] = svc`` puts the source order out of step with the array."""
    slots = _slots(tmp_path, """
void svc(void); void hard(void);
void (*const vectors[16])(void) = { [11] = svc, [3] = hard };
""")
    assert slots == {"svc": {None}, "hard": {None}}


def test_one_designator_suppresses_the_whole_list(tmp_path):
    """The elements after a designator continue from the index it named.

    The leading ``reset`` looks positional on its own, but ``[8] = hard``
    proves the list is designated, so no element of it can be counted.
    """
    slots = _slots(tmp_path, """
void reset(void); void hard(void);
void (*const vectors[16])(void) = { reset, [8] = hard };
""")
    assert slots == {"reset": {None}, "hard": {None}}


def test_a_float_element_is_not_read_as_a_designator(tmp_path):
    """``.5`` is one token, so the leading dot does not mean ``.field =``."""
    slots = _slots(tmp_path, """
void reset(void);
struct mixed { double scale; void (*fn)(void); };
struct mixed table[] = { { .5, reset } };
""")
    assert slots == {"reset": {0}}


def test_a_struct_that_is_not_an_array_reports_no_slot(tmp_path):
    """A field position is not a slot, so a plain struct gives nothing."""
    slots = _slots(tmp_path, """
void isr_a(void);
struct entry { const void *arg; void (*fn)(void); };
struct entry one = { 0, isr_a };
""")
    assert slots == {"isr_a": {None}}


def test_a_two_dimensional_table_reports_no_slot(tmp_path):
    """A row and a column do not reduce to one slot number."""
    slots = _slots(tmp_path, """
void h00(void); void h01(void); void h10(void); void h11(void);
void (*fsm[2][2])(void) = { { h00, h01 }, { h10, h11 } };
""")
    assert slots == {"h00": {None}, "h01": {None},
                     "h10": {None}, "h11": {None}}


def test_a_typedef_cannot_hide_the_second_dimension(tmp_path):
    """The element type is read canonically, so ``row_t`` is still an array."""
    slots = _slots(tmp_path, """
void a(void); void b(void); void c(void); void d(void);
typedef void (*row_t[2])(void);
row_t grid[2] = { { a, b }, { c, d } };
""")
    assert slots == {"a": {None}, "b": {None}, "c": {None}, "d": {None}}


def test_the_same_target_in_two_slots_keeps_both(tmp_path):
    """``{ reset, reset }`` is two facts, and they share a line.

    Without the slot in the identity of the reference the second element
    would read as a repeat of the first and be dropped.
    """
    slots = _slots(tmp_path, """
void reset(void);
void (*const vectors[])(void) = { reset, reset };
""")
    assert slots == {"reset": {0, 1}}


def test_a_nested_list_does_not_add_a_second_slotless_row(tmp_path):
    """One reference, one row.

    The enclosing array list and the struct list inside it both reach
    ``isr_a``.  The enclosing one knows the slot; the inner one does not.
    Recording both would leave a reader two rows for one address.
    """
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "table.c"
    src.write_text("""
void isr_a(void);
struct entry { const void *arg; void (*fn)(void); };
struct entry table[] = { { 0, isr_a } };
""", encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                           clang_args=["-std=c11"])
    result = extract_all(unit, with_refs=True)
    rows = [r for r in result.references
            if r.ref_kind == "indirect" and r.to_usr.endswith("isr_a")]
    assert len(rows) == 1, [(r.from_line, r.slot_index) for r in rows]
    assert rows[0].slot_index == 0


def test_the_outermost_list_is_found_by_its_parent_not_by_a_kind(tmp_path):
    """A list whose parent is a FIELD_DECL is outermost, not nested.

    The rule asks only whether a parent EXISTS, because the parent of an
    outermost list is a VAR_DECL in C and can be a FIELD_DECL in C++.  A
    test against VAR_DECL alone would pass in C and quietly refuse every
    C++ class member.

    This exercises the rule directly rather than through ``extract_all``,
    because the reference pass does not descend into a C++ in-class member
    initializer at all — no indirect reference is produced there, with or
    without this change.  The rule is still pinned so that a later fix to
    that pass inherits the right slot instead of a refusal.
    """
    from clang import cindex as cx

    from fw_context_mcp.indexer.symbols import _init_list_gives_slots

    src = tmp_path / "field.cpp"
    src.write_text("""
void reset(); void nmi();
struct Field { void (*table[2])() = { reset, nmi }; };
struct Pair { void (*a)(); void (*b)(); };
Pair pairs[] = { { reset, nmi } };
""", encoding="utf-8")
    tu = cx.Index.create().parse(str(src), args=["-std=c++17"])

    lists = []

    def collect(cursor):
        if cursor.kind == cx.CursorKind.INIT_LIST_EXPR:
            parent = cursor.semantic_parent
            lists.append((
                parent.kind.name if parent else None,
                _init_list_gives_slots(cursor),
            ))
        for child in cursor.get_children():
            collect(child)

    collect(tu.cursor)

    # The FIELD_DECL list and the VAR_DECL list both count; the struct list
    # nested inside `pairs` reports no parent and does not.
    assert lists == [
        ("FIELD_DECL", True),
        ("VAR_DECL", True),
        (None, False),
    ]


def test_an_assignment_outside_a_list_reports_no_slot(tmp_path):
    """A plain assignment has no position, and must not gain a false one."""
    slots = _slots(tmp_path, """
void handler(void);
void (*cb)(void);
void install(void) { cb = handler; }
""")
    assert slots == {"handler": {None}}


def test_the_slot_reaches_the_database(tmp_path):
    """A slot is only useful if it survives the write.

    ``ops`` builds the row tuple for every C reference; this pins that the
    last column carries the slot instead of a constant None.
    """
    from fw_context_mcp.indexer.models import Reference

    db_path = tmp_path / "index.db"
    conn = open_db(db_path)
    try:
        insert_refs_batch(conn, [
            ("hash", r.to_usr, r.from_file, r.from_line, r.from_usr,
             r.ref_kind, r.slot_index)
            for r in (
                Reference("c:@F@reset", "v.c", 3, None, "indirect", 0),
                Reference("c:@F@nmi", "v.c", 3, None, "indirect", 1),
                Reference("c:@F@other", "v.c", 9, None, "indirect", None),
            )
        ])
        conn.commit()
        stored = dict(conn.execute(
            "SELECT to_usr, slot_index FROM refs ORDER BY to_usr"
        ).fetchall())
    finally:
        conn.close()

    assert stored == {"c:@F@reset": 0, "c:@F@nmi": 1, "c:@F@other": None}
