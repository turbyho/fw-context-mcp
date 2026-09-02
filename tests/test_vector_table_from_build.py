"""Naming the slots the index could not, from what the build recorded.

A generated table holds a resolved address in every slot that is in use, so
those slots carry no name for the index to read — and they are the
interrupts the firmware actually services.  ``get_vector_table`` now fills
them from the build's own record of the registrations.

Measured on the real index, which is what these tests reproduce in
miniature:

    nrf52840-dev/app   _sw_isr_table 43/48   slot 0 -> nrfx_isr,
                       2 -> uarte_nrfx_isr_int, 6 -> gpio_nrfx_gpiote_...,
                       17 -> rtc_nrf_isr, 41 -> nrfx_isr
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
from fw_context_mcp.mcp.handlers.callgraph import _from_the_build
from tests.test_intlist import _elf, _intlist

CONFIG = "ch"


def _symbol(config, file_id, path, name, usr, kind="function", line=10):
    return (config, file_id, path, name, usr, name, name, kind, line, 1,
            line + 2, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0)


def _index(tmp_path, *, declared=8, named=(0, 1, 2), handlers=(),
           registrations=(), config_text=None, cc_in_db=True):
    """Build an index whose table has gaps, plus a build that names them.

    *named* are the slots the index could read; the rest of *declared* are
    the gaps.  *registrations* are (irq, handler) or (irq, handler,
    argument) the build recorded — the argument being the symbol passed to
    the handler, which for a shim is the real worker.
    """
    build = tmp_path / "build"
    zephyr = build / "zephyr"
    zephyr.mkdir(parents=True)

    # One address per distinct symbol the build refers to, handler or
    # argument alike, so the reader has to resolve both from the symtab.
    wanted: list[str] = []
    for entry in registrations:
        for name in entry[1:]:
            if name and name not in wanted:
                wanted.append(name)
    addresses = {name: 0x1001 + index * 2 for index, name in enumerate(wanted)}

    entries = [
        (
            entry[0],
            addresses[entry[1]],
            addresses[entry[2]] if len(entry) > 2 and entry[2] else 0,
        )
        for entry in registrations
    ]
    symbols = [(name, address, 2) for name, address in addresses.items()]
    (zephyr / "zephyr_pre0.elf").write_bytes(
        _elf(_intlist(declared, 0, entries), symbols))
    if config_text is not None:
        (zephyr / ".config").write_text(config_text, encoding="utf-8")

    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(
            conn, CONFIG, "pid",
            str(build / "compile_commands.json") if cc_in_db else "",
        )
        fid = upsert_file(conn, CONFIG, "isr_tables.c", "c")
        insert_symbols_batch(conn, [
            (CONFIG, fid, "isr_tables.c", "_sw_isr_table", "c:@_sw_isr_table",
             "_sw_isr_table", "_sw_isr_table", "varglobal", 10, 1, 40, 1,
             f"const struct entry[{declared}] _sw_isr_table", "", None,
             0, 0, "", 0, "", 1, 0.0, "", 0),
            *[_symbol(CONFIG, fid, "spurious.c", f"stub{slot}",
                      f"c:@F@stub{slot}", line=50 + slot) for slot in named],
            *[_symbol(CONFIG, fid, path, name, usr, line=line)
              for name, usr, path, line in handlers],
        ])
        insert_refs_batch(conn, [
            (CONFIG, f"c:@F@stub{slot}", "isr_tables.c", 11 + slot, None,
             "indirect", slot)
            for slot in named
        ])
    conn.commit()
    return conn


def _rows(conn):
    return _from_the_build(conn, CONFIG, get_table_coverage(conn, CONFIG))


def test_a_gap_the_build_can_name_becomes_a_row(tmp_path):
    """The whole point: a slot with no name in the index gets one."""
    conn = _index(
        tmp_path,
        registrations=[(5, "uarte_nrfx_isr_int")],
        handlers=[("uarte_nrfx_isr_int", "c:@F@uarte_nrfx_isr_int",
                   "uart_nrfx_uarte.c", 484)],
    )
    try:
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["slot"] == 5
        assert rows[0]["name"] == "uarte_nrfx_isr_int"
        assert rows[0]["source"] == "build"
        assert rows[0]["table_name"] == "_sw_isr_table"
        assert (rows[0]["file"], rows[0]["line"]) == ("uart_nrfx_uarte.c", 484)
    finally:
        conn.close()


def test_a_slot_the_index_already_named_is_not_duplicated(tmp_path):
    """Two sources agreeing is not a reason to print the slot twice.

    And if they disagreed, a second row would hide it rather than show it.
    """
    conn = _index(
        tmp_path,
        named=(0, 1, 2),
        registrations=[(1, "handler"), (5, "handler")],
        handlers=[("handler", "c:@F@handler", "drv.c", 7)],
    )
    try:
        assert [row["slot"] for row in _rows(conn)] == [5]
    finally:
        conn.close()


def test_the_argument_is_reported_when_the_build_named_one(tmp_path):
    """``nrfx_isr`` is a shim, and its argument is the real handler.

    Measured: slot 0 reaches ``nrfx_isr`` with the argument
    ``nrfx_power_clock_irq_handler``.  Without the argument the answer is
    true and useless.
    """
    conn = _index(
        tmp_path,
        registrations=[
            (4, "nrfx_isr", "nrfx_power_clock_irq_handler"),
            (5, "nrfx_isr", "nrfx_qspi_irq_handler"),
        ],
        handlers=[("nrfx_isr", "c:@F@nrfx_isr", "nrfx_glue.c", 11)],
    )
    try:
        rows = _rows(conn)
        # One shim, two interrupts, and the argument is the only thing that
        # tells them apart.
        assert [(r["slot"], r["name"], r["argument"]) for r in rows] == [
            (4, "nrfx_isr", "nrfx_power_clock_irq_handler"),
            (5, "nrfx_isr", "nrfx_qspi_irq_handler"),
        ]
    finally:
        conn.close()


def test_no_argument_key_when_the_build_named_none(tmp_path):
    """``rtc_nrf_isr`` was measured with a plain value as its argument."""
    conn = _index(
        tmp_path,
        registrations=[(5, "rtc_nrf_isr")],
        handlers=[("rtc_nrf_isr", "c:@F@rtc_nrf_isr", "nrf_rtc_timer.c", 586)],
    )
    try:
        rows = _rows(conn)
        assert rows[0]["name"] == "rtc_nrf_isr"
        assert "argument" not in rows[0]
    finally:
        conn.close()


def test_an_assembly_handler_reads_as_assembly(tmp_path):
    """The status must follow where the definition really is."""
    conn = _index(
        tmp_path,
        registrations=[(6, "asm_handler")],
        handlers=[("asm_handler", "asm:asm_handler", "isr.S", 137)],
    )
    try:
        rows = _rows(conn)
        assert rows[0]["status"] == "assembly"
        assert (rows[0]["file"], rows[0]["line"]) == ("isr.S", 137)
    finally:
        conn.close()


def test_a_handler_with_two_definitions_names_no_file(tmp_path):
    """Pointing at the wrong file is worse than pointing at none."""
    conn = _index(
        tmp_path,
        registrations=[(6, "shared")],
        handlers=[("shared", "c:@F@a@shared", "a.c", 10),
                  ("shared", "c:@F@b@shared", "b.c", 20)],
    )
    try:
        rows = _rows(conn)
        assert rows[0]["name"] == "shared"
        assert (rows[0]["file"], rows[0]["line"]) == ("", 0)
    finally:
        conn.close()


def test_dynamic_interrupts_add_a_notice(tmp_path):
    """A run-time registration leaves nothing to read, so say so.

    Without it the list reads as the whole story when it is not.
    """
    conn = _index(
        tmp_path,
        registrations=[(5, "handler")],
        handlers=[("handler", "c:@F@handler", "drv.c", 7)],
        config_text="CONFIG_DYNAMIC_INTERRUPTS=y\n",
    )
    try:
        rows = _rows(conn)
        assert rows[0]["slot"] == 5
        assert "CONFIG_DYNAMIC_INTERRUPTS" in rows[-1]["info"]
    finally:
        conn.close()


def test_a_registration_outside_any_gap_is_ignored(tmp_path):
    """A number with nowhere to belong is not a row."""
    conn = _index(
        tmp_path,
        declared=8,
        named=tuple(range(8)),  # no gaps at all
        registrations=[(3, "handler")],
        handlers=[("handler", "c:@F@handler", "drv.c", 7)],
    )
    try:
        assert _rows(conn) == []
    finally:
        conn.close()


@pytest.mark.parametrize("cc_in_db", [True, False])
def test_nothing_to_read_is_not_an_error(tmp_path, cc_in_db):
    """No artifact, or no build recorded in the index, answers empty.

    Measured: not one artifact of 60 on Mbed, CMSIS and Arduino projects
    carried the section, so this is the ordinary path elsewhere.
    """
    conn = _index(tmp_path, registrations=[], cc_in_db=cc_in_db)
    try:
        assert _rows(conn) == []
    finally:
        conn.close()
