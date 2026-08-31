"""Two sources of vector slots, read as one table.

A build can hold its table as address words in assembly, as an array of
function addresses in C, or as both — Zephyr on ARM puts the 16 exception
vectors in assembly and generates the 290 external IRQs into C.  The tool
used to see only the first, which is why it reported 11 slots there.

These tests cover the merge: that both sources arrive, that each row says
where it came from, that slot numbers stay inside their own table, and that
a dispatcher is told apart from a handler and from an unused stub.
"""
import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.db import (
    get_vector_table,
    insert_indirect_call_sites_batch,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

CONFIG = "ch"


def _symbol_row(config, file_id, path, name, usr, kind="function",
                line=10, end_line=12, is_weak=0):
    """One symbols row in the 24-column shape the batch insert expects."""
    return (config, file_id, path, name, usr, name, name, kind, line, 1,
            end_line, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", is_weak)


def _db(tmp_path):
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, CONFIG, "pid", "cc.json")
    return conn


def _assembly_table(conn, slots, table_file="startup.S"):
    """Write an assembly-style table: one 'vector' ref for each slot.

    Each target is stored as a strong assembly definition, which is what
    the assembly reader produces for a handler written in assembly.
    """
    with transaction(conn):
        fid = upsert_file(conn, CONFIG, table_file, "asm")
        insert_symbols_batch(conn, [
            _symbol_row(CONFIG, fid, table_file, name, f"asm:{name}",
                        line=70 + index)
            for index, name in enumerate(dict.fromkeys(slots.values()))
        ])
        insert_refs_batch(conn, [
            (CONFIG, f"asm:{name}", table_file, 40 + slot, None,
             "vector", slot)
            for slot, name in slots.items()
        ])


def _c_table(conn, table_name, slots, table_file="isr_tables.c",
             first_line=11):
    """Write a C-style table: an array symbol plus one 'indirect' ref each.

    The array covers the lines its elements sit on, which is how the reader
    finds the owning table.
    """
    with transaction(conn):
        fid = upsert_file(conn, CONFIG, table_file, "c")
        insert_symbols_batch(conn, [
            _symbol_row(CONFIG, fid, table_file, table_name,
                        f"c:@{table_name}", kind="varglobal",
                        line=first_line - 1,
                        end_line=first_line + len(slots)),
            *[
                _symbol_row(CONFIG, fid, "handlers.c", name, f"c:@F@{name}",
                            line=100 + index)
                for index, name in enumerate(dict.fromkeys(slots.values()))
            ],
        ])
        insert_refs_batch(conn, [
            (CONFIG, f"c:@F@{name}", table_file, first_line + slot, None,
             "indirect", slot)
            for slot, name in slots.items()
        ])


def _calls_through_pointer(conn, name):
    """Record that *name* holds an indirect call site."""
    with transaction(conn):
        insert_indirect_call_sites_batch(conn, [
            (CONFIG, "handlers.c", 101, f"c:@F@{name}", "handler()",
             "c:@F@slot", "handler", "void (*)(void)"),
        ])


def test_both_sources_arrive(tmp_path):
    """The C table used to be invisible; now it is read alongside assembly."""
    conn = _db(tmp_path)
    try:
        _assembly_table(conn, {1: "Reset_Handler", 15: "SysTick_Handler"})
        _c_table(conn, "_irq_vector_table", {0: "uart_isr", 1: "spi_isr"})

        rows = get_vector_table(conn, CONFIG)
        assert [(r["source"], r["slot"], r["name"]) for r in rows] == [
            ("assembly", 1, "Reset_Handler"),
            ("assembly", 15, "SysTick_Handler"),
            ("c", 0, "uart_isr"),
            ("c", 1, "spi_isr"),
        ]
    finally:
        conn.close()


def test_slot_numbers_are_not_joined_into_one_run(tmp_path):
    """Slot 15 of assembly and slot 0 of C stay as they are.

    Renumbering would need the length of the assembly table, and the index
    holds only its occupied slots — so any offset derived from it would be
    silently wrong on a build that leaves its last exception slot unused.
    """
    conn = _db(tmp_path)
    try:
        _assembly_table(conn, {15: "SysTick_Handler"})
        _c_table(conn, "_irq_vector_table", {0: "uart_isr"})

        slots = {(r["source"], r["slot"]) for r in get_vector_table(conn, CONFIG)}
        assert slots == {("assembly", 15), ("c", 0)}
    finally:
        conn.close()


def test_two_c_tables_keep_their_own_numbering(tmp_path):
    """Both tables start at zero, so the row has to name its table."""
    conn = _db(tmp_path)
    try:
        _c_table(conn, "_irq_vector_table", {0: "uart_isr"},
                 table_file="isr_tables.c", first_line=11)
        _c_table(conn, "fsm_steps", {0: "step_x"},
                 table_file="fsm.c", first_line=5)

        rows = get_vector_table(conn, CONFIG)
        assert {(r["table_name"], r["slot"], r["name"]) for r in rows} == {
            ("_irq_vector_table", 0, "uart_isr"),
            ("fsm_steps", 0, "step_x"),
        }
    finally:
        conn.close()


def test_a_repeated_target_that_calls_a_pointer_is_a_dispatcher(tmp_path):
    """The Zephyr shape: one wrapper fills every external IRQ slot.

    It cannot be servicing any one of them, and it calls through a pointer
    to decide where to go, so a reader must follow it further.
    """
    conn = _db(tmp_path)
    try:
        _c_table(conn, "_irq_vector_table",
                 {0: "_isr_wrapper", 1: "_isr_wrapper", 2: "_isr_wrapper"})
        _calls_through_pointer(conn, "_isr_wrapper")

        rows = get_vector_table(conn, CONFIG)
        assert {r["status"] for r in rows} == {"dispatcher"}
    finally:
        conn.close()


def test_a_repeated_target_that_calls_nothing_is_not_a_dispatcher(tmp_path):
    """``z_irq_spurious`` fills 43 slots of a real table and dispatches nothing.

    Counting slots alone would call it a dispatcher and send a reader
    looking for a jump table that does not exist.
    """
    conn = _db(tmp_path)
    try:
        _c_table(conn, "_sw_isr_table",
                 {0: "z_irq_spurious", 1: "z_irq_spurious"})

        rows = get_vector_table(conn, CONFIG)
        assert {r["status"] for r in rows} == {"c"}
    finally:
        conn.close()


def test_a_single_slot_handler_that_calls_a_callback_is_not_a_dispatcher(tmp_path):
    """``POWER_CLOCK_IRQHandler`` holds one slot and calls 6 callbacks.

    It does service its interrupt, so the pointer call alone must not
    relabel it — measured on zbox-ecb-fw, where three such handlers would
    otherwise change status.
    """
    conn = _db(tmp_path)
    try:
        _c_table(conn, "_irq_vector_table",
                 {0: "POWER_CLOCK_IRQHandler", 1: "RADIO_IRQHandler"})
        _calls_through_pointer(conn, "POWER_CLOCK_IRQHandler")

        rows = get_vector_table(conn, CONFIG)
        assert {(r["name"], r["status"]) for r in rows} == {
            ("POWER_CLOCK_IRQHandler", "c"),
            ("RADIO_IRQHandler", "c"),
        }
    finally:
        conn.close()


def test_the_same_target_in_two_tables_is_counted_per_table(tmp_path):
    """Filling one slot of each of two tables is not filling two slots.

    The count has to be per table, or a handler shared between an interrupt
    table and an unrelated one would read as a dispatcher.
    """
    conn = _db(tmp_path)
    try:
        _c_table(conn, "table_a", {0: "shared_isr"},
                 table_file="a.c", first_line=11)
        _c_table(conn, "table_b", {0: "shared_isr"},
                 table_file="b.c", first_line=11)
        _calls_through_pointer(conn, "shared_isr")

        rows = get_vector_table(conn, CONFIG)
        assert {r["status"] for r in rows} == {"c"}
    finally:
        conn.close()


def test_an_unhandled_stub_is_not_relabelled_as_a_dispatcher(tmp_path):
    """When both answers fit, ``unhandled`` is the one a reader needs.

    This case is built to force the collision: one target fills two slots
    of the table, calls through a pointer, AND is an alias of another
    symbol.  Both rules then apply, and the order in the ladder decides.
    ``unhandled`` has to win — "nothing services this interrupt" is the
    fact a reader cannot afford to have hidden behind "look further".

    A real CMSIS table does not reach this: each unserviced vector has its
    own name, so each fills one slot, and ``Default_Handler`` is an
    infinite loop that calls nothing.  The ordering is therefore a
    deliberate guard rather than an observed case, and it is pinned here so
    it cannot be reordered by accident.
    """
    conn = _db(tmp_path)
    try:
        _assembly_table(conn, {2: "Stub_Handler", 3: "Stub_Handler"})
        with transaction(conn):
            fid = upsert_file(conn, CONFIG, "startup.S", "asm")
            insert_symbols_batch(conn, [
                _symbol_row(CONFIG, fid, "startup.S", "Default_Handler",
                            "asm:Default_Handler", line=90),
            ])
            insert_refs_batch(conn, [
                (CONFIG, "asm:Default_Handler", "startup.S", 70,
                 "asm:Stub_Handler", "alias", None),
            ])
            insert_indirect_call_sites_batch(conn, [
                (CONFIG, "startup.S", 91, "asm:Stub_Handler", "p()",
                 "c:@p", "p", "void (*)(void)"),
            ])

        rows = get_vector_table(conn, CONFIG)
        assert {r["status"] for r in rows} == {"unhandled"}
        assert all(r["aliases"]["name"] == "Default_Handler" for r in rows)
    finally:
        conn.close()


def test_unhandled_only_still_filters(tmp_path):
    """The existing filter must not start letting C rows through."""
    conn = _db(tmp_path)
    try:
        _assembly_table(conn, {1: "Reset_Handler"})
        _c_table(conn, "_irq_vector_table", {0: "uart_isr"})

        assert get_vector_table(conn, CONFIG, unhandled_only=True) == []
    finally:
        conn.close()


def test_an_assembly_only_build_is_unchanged(tmp_path):
    """A table wholly in assembly gains ``source`` and nothing else."""
    conn = _db(tmp_path)
    try:
        _assembly_table(conn, {1: "Reset_Handler", 2: "NMI_Handler"})

        rows = get_vector_table(conn, CONFIG)
        assert [(r["slot"], r["name"], r["status"], r["source"]) for r in rows] == [
            (1, "Reset_Handler", "assembly", "assembly"),
            (2, "NMI_Handler", "assembly", "assembly"),
        ]
        assert "table_name" not in rows[0]
    finally:
        conn.close()


@pytest.mark.parametrize("unhandled_only", [False, True])
def test_an_empty_index_answers_empty(tmp_path, unhandled_only):
    """No table at all is not an error."""
    conn = _db(tmp_path)
    try:
        assert get_vector_table(conn, CONFIG, unhandled_only) == []
    finally:
        conn.close()


class TestADeclarationIsNotTheDefinition:
    """A table in C names what the compiler saw, not where the code is.

    Measured on the RISC-V image of zbox-ecb-fw-v5: all 571 slots pointed at
    ``_isr_wrapper`` in ``sw_isr_table.h:29`` with ``is_definition = 0``,
    while the definition is ``arch/riscv/core/isr.S:137``.  Every row then
    named a header with nothing to read, and the status came out ``"c"`` —
    asserting that code outside assembly services the interrupt, which is
    false when the definition is assembly.
    """

    @staticmethod
    def _riscv_shape(conn, definition_files=("isr.S",)):
        """Write the shape: a C declaration in a header, plus definitions.

        More than one *definition_files* entry makes the name ambiguous,
        which is the case the rule has to refuse.
        """
        with transaction(conn):
            header = upsert_file(conn, CONFIG, "sw_isr_table.h", "c")
            insert_symbols_batch(conn, [
                # The C declaration the initializer resolves to.
                (CONFIG, header, "sw_isr_table.h", "_isr_wrapper",
                 "c:@F@_isr_wrapper", "_isr_wrapper", "_isr_wrapper",
                 "function", 29, 1, 29, 0, "", "", None, 0, 0, "", 0, "",
                 1, 0.0, "", 0),
            ])
            for index, name in enumerate(definition_files):
                fid = upsert_file(conn, CONFIG, name, "asm")
                insert_symbols_batch(conn, [
                    _symbol_row(CONFIG, fid, name, "_isr_wrapper",
                                f"asm:{index}:_isr_wrapper", line=137 + index,
                                end_line=200 + index),
                ])
            table = upsert_file(conn, CONFIG, "isr_tables.c", "c")
            insert_symbols_batch(conn, [
                _symbol_row(CONFIG, table, "isr_tables.c",
                            "_irq_vector_table", "c:@_irq_vector_table",
                            kind="varglobal", line=10, end_line=13),
            ])
            insert_refs_batch(conn, [
                (CONFIG, "c:@F@_isr_wrapper", "isr_tables.c", 11 + slot,
                 None, "indirect", slot)
                for slot in (0, 1)
            ])

    def test_the_row_moves_onto_the_definition(self, tmp_path):
        """file and line must name code, and the status must be assembly."""
        conn = _db(tmp_path)
        try:
            self._riscv_shape(conn)

            rows = get_vector_table(conn, CONFIG)
            assert [(r["slot"], r["file"], r["line"], r["status"]) for r in rows] == [
                (0, "isr.S", 137, "assembly"),
                (1, "isr.S", 137, "assembly"),
            ]
        finally:
            conn.close()

    def test_two_definitions_of_one_name_leave_the_row_alone(self, tmp_path):
        """Two static functions may share a name; guessing between them is worse.

        The row keeps the declaration, which is at least not a claim about
        the wrong file.
        """
        conn = _db(tmp_path)
        try:
            self._riscv_shape(conn, definition_files=("isr.S", "other.S"))

            rows = get_vector_table(conn, CONFIG)
            assert {r["file"] for r in rows} == {"sw_isr_table.h"}
            assert {r["status"] for r in rows} == {"c"}
        finally:
            conn.close()

    def test_a_declaration_with_no_definition_stays_put(self, tmp_path):
        """Nothing to move onto is not an error."""
        conn = _db(tmp_path)
        try:
            self._riscv_shape(conn, definition_files=())

            rows = get_vector_table(conn, CONFIG)
            assert {r["file"] for r in rows} == {"sw_isr_table.h"}
        finally:
            conn.close()

    def test_a_row_that_already_names_a_definition_is_untouched(self, tmp_path):
        """The common case must not be disturbed by the repair path."""
        conn = _db(tmp_path)
        try:
            _c_table(conn, "_irq_vector_table", {0: "uart_isr"})

            rows = get_vector_table(conn, CONFIG)
            assert [(r["slot"], r["name"], r["status"]) for r in rows] == [
                (0, "uart_isr", "c"),
            ]
            assert rows[0]["file"] == "handlers.c"
        finally:
            conn.close()

    def test_no_row_carries_the_internal_flag(self, tmp_path):
        """Both sources must return the same keys.

        is_definition is how the C reader repairs a row; a reader of the
        tool should not have to know that only C rows have it.
        """
        conn = _db(tmp_path)
        try:
            _assembly_table(conn, {1: "Reset_Handler"})
            _c_table(conn, "_irq_vector_table", {0: "uart_isr"})

            keys = [set(row) for row in get_vector_table(conn, CONFIG)]
            assert all("is_definition" not in row for row in keys)
        finally:
            conn.close()


class TestTheLimitIsVisible:
    """Cutting a table short must never look like a whole table.

    A generated table measures 301 rows with the assembly exceptions, so
    the cut is reachable with ordinary settings and a reader counting
    serviced interrupts would otherwise be wrong with no way to notice.
    """

    @staticmethod
    def _rows(count):
        return [{"slot": index, "name": f"h{index}"} for index in range(count)]

    def test_nothing_is_added_when_everything_fits(self):
        from fw_context_mcp.mcp.handlers.callgraph import _slots_within_limit

        rows = self._rows(5)
        assert _slots_within_limit(rows, 5) == rows
        assert _slots_within_limit(rows, 9) == rows

    def test_the_cut_is_reported_with_both_counts(self):
        from fw_context_mcp.mcp.handlers.callgraph import _slots_within_limit

        out = _slots_within_limit(self._rows(301), 300)
        assert len(out) == 301
        assert out[:300] == self._rows(301)[:300]
        assert "1 of 301 slots are not shown" in out[-1]["truncated"]

    def test_a_limit_of_zero_still_says_what_was_hidden(self):
        from fw_context_mcp.mcp.handlers.callgraph import _slots_within_limit

        out = _slots_within_limit(self._rows(7), 0)
        assert out == [{"truncated": (
            "7 of 7 slots are not shown. "
            "Raise limit (max 1000), or narrow with unhandled_only."
        )}]

    def test_the_default_limit_holds_a_measured_table(self):
        """290 generated entries plus a 92-slot assembly table must fit."""
        import inspect

        from fw_context_mcp.mcp.handlers.callgraph import get_vector_table

        default = inspect.signature(get_vector_table).parameters["limit"].default
        assert default >= 290 + 92
