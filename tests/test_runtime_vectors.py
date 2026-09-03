"""A vector table the running program fills, not the image.

A CMSIS target can move its table into RAM and install each handler with
``NVIC_SetVector``.  The image then holds an alias of the trap loop for
every such slot, so the static table says "nothing services this
interrupt" about interrupts that are serviced.

Measured on the Mbed project, which sets ``CMSIS_VECTAB_VIRTUAL``: 14
calls, 12 distinct slots, all of them reported ``unhandled`` before this
existed.  Among them the tick source (IRQ 9 -> slot 25 ->
``us_ticker_irq_handler``), the low-power tick (17 -> 33), the console
(2 -> 18) and USB (39 -> 55).

These tests drive the whole path — parse, store, query — because the
number a reader sees is the IRQ of the argument plus
``NVIC_USER_IRQ_OFFSET`` of the macro table, and those two meet only in
the query.
"""
import pytest

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.indexer.compile_commands import CompilationUnit
from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project
from fw_context_mcp.indexer.db._callgraph import (
    _runtime_registrations,
    _user_irq_offset,
)
from fw_context_mcp.indexer.nvic import RUNTIME_VECTOR_REF_KIND
from fw_context_mcp.indexer.ops import store_symbols_for_unit

pytestmark = pytest.mark.libclang

CONFIG = "testhash"

# The shape of the real thing: an enum of interrupts, the offset as a
# macro, a handler, and the SDK function that writes the RAM table.
_PRELUDE = """
typedef enum {
    Reset_IRQn = -15,
    UARTE0_UART0_IRQn = 2,
    TIMER1_IRQn = 9,
    RTC1_IRQn = 17
} IRQn_Type;
#define NVIC_USER_IRQ_OFFSET 16
void NVIC_SetVector(IRQn_Type IRQn, unsigned int vector);
void us_ticker_irq_handler(void);
void RTC1_IRQHandler(void);
"""


def _index(tmp_path, code, filename="us_ticker.c"):
    """Store *code* through the indexer, and return the connection."""
    src = tmp_path / filename
    src.write_text(_PRELUDE + code, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c",
                           clang_args=["-std=c11"])
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, CONFIG, "pid", "cc.json")
    store_symbols_for_unit(conn, unit, CONFIG, tmp_path, index_refs=True)
    conn.commit()
    return conn


def _refs(conn):
    """The runtime_vector rows, as (irq, handler name) pairs."""
    return [
        (row["slot_index"], row["name"])
        for row in conn.execute(
            """SELECT r.slot_index, s.name FROM refs r
               JOIN symbols s ON s.usr = r.to_usr
                             AND s.config_hash = r.config_hash
               WHERE r.config_hash = ? AND r.ref_kind = ?
               ORDER BY r.slot_index""",
            (CONFIG, RUNTIME_VECTOR_REF_KIND),
        ).fetchall()
    ]


class TestWhatTheIndexerRecords:
    """The pass over the call, and what it refuses to guess at."""

    def test_a_registration_becomes_a_reference(self, tmp_path):
        """The Mbed shape: the handler address cast to an integer."""
        conn = _index(tmp_path, """
void us_ticker_init(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            assert _refs(conn) == [(9, "us_ticker_irq_handler")]
        finally:
            conn.close()

    def test_the_irq_number_is_stored_unshifted(self, tmp_path):
        """The offset belongs to the reader, so the data holds the IRQ.

        Storing slot 25 here would write a target constant into the
        index; storing 9 keeps what the argument said.
        """
        conn = _index(tmp_path, """
void init(void) {
    NVIC_SetVector(RTC1_IRQn, (unsigned int) RTC1_IRQHandler);
}
""")
        try:
            assert _refs(conn) == [(17, "RTC1_IRQHandler")]
        finally:
            conn.close()

    def test_two_call_sites_are_two_references(self, tmp_path):
        """Two drivers register the same slot, and both places count.

        Measured: the I2C/SPI slots are registered from ``spi_api.c`` and
        again from ``i2c_api.c``.
        """
        conn = _index(tmp_path, """
void first(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
void second(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            assert _refs(conn) == [
                (9, "us_ticker_irq_handler"), (9, "us_ticker_irq_handler"),
            ]
        finally:
            conn.close()

    def test_a_computed_irq_records_nothing(self, tmp_path):
        """A loop over interrupts names no slot, and a guess would lie."""
        conn = _index(tmp_path, """
void init(void) {
    for (int i = 0; i < 4; i++)
        NVIC_SetVector((IRQn_Type) i, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            assert _refs(conn) == []
        finally:
            conn.close()

    def test_an_address_that_is_not_a_function_records_nothing(self, tmp_path):
        """A number in the second argument names no handler."""
        conn = _index(tmp_path, """
void init(void) {
    NVIC_SetVector(TIMER1_IRQn, 0x20000100);
}
""")
        try:
            assert _refs(conn) == []
        finally:
            conn.close()

    def test_another_function_with_the_same_arguments_is_not_a_vector(
        self, tmp_path,
    ):
        """Only the CMSIS names write the table.

        A callback registration has exactly the same shape, and treating
        it as a vector would put a handler in a slot nothing wrote.
        """
        conn = _index(tmp_path, """
void register_callback(IRQn_Type which, unsigned int fn);
void init(void) {
    register_callback(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            assert _refs(conn) == []
        finally:
            conn.close()


class TestWhatTheReaderMakesOfIt:
    """The IRQ number becomes a slot, or nothing at all."""

    def test_the_offset_comes_from_the_macro_table(self, tmp_path):
        conn = _index(tmp_path, """
void init(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            assert _user_irq_offset(conn, CONFIG) == 16
            # IRQ 9 plus the offset is the slot of the table.
            found = _runtime_registrations(conn, CONFIG)
            assert set(found) == {25}
            assert found[25][0]["name"] == "us_ticker_irq_handler"
            # The call site, so a reader can see WHEN the slot is filled.
            assert found[25][0]["at"].endswith(":14")
        finally:
            conn.close()

    def test_without_the_offset_no_slot_is_reported(self, tmp_path):
        """An index that never saw the macro must not assume 16.

        The registration is still in ``refs``; what cannot be done is
        turning an IRQ number into a slot number.
        """
        conn = _index(tmp_path, """
void init(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            conn.execute(
                "DELETE FROM macros WHERE name = 'NVIC_USER_IRQ_OFFSET'")
            conn.commit()
            assert _user_irq_offset(conn, CONFIG) is None
            assert _runtime_registrations(conn, CONFIG) == {}
        finally:
            conn.close()

    def test_two_call_sites_become_one_slot_with_two_entries(self, tmp_path):
        conn = _index(tmp_path, """
void first(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
void second(void) {
    NVIC_SetVector(TIMER1_IRQn, (unsigned int) us_ticker_irq_handler);
}
""")
        try:
            found = _runtime_registrations(conn, CONFIG)
            assert set(found) == {25}
            assert len(found[25]) == 2
            assert {entry["at"].rsplit(":", 1)[1] for entry in found[25]} == {
                "14", "17",
            }
        finally:
            conn.close()
