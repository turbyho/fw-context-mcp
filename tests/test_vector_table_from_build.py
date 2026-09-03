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
import json

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
from fw_context_mcp.indexer.intlist import read_isr_registrations
from fw_context_mcp.mcp.handlers.callgraph import (
    _build_directory,
    _from_the_build,
    _interrupt_summary,
    _vector_rows,
)
from tests.test_intlist import _elf, _intlist

CONFIG = "ch"


def _symbol(config, file_id, path, name, usr, kind="function", line=10):
    return (config, file_id, path, name, usr, name, name, kind, line, 1,
            line + 2, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0)


def _index(tmp_path, *, declared=8, named=(0, 1, 2), handlers=(),
           registrations=(), config_text=None, cc_in_db=True,
           cc_path=None, cc_directory=None):
    """Build an index whose table has gaps, plus a build that names them.

    *named* are the slots the index could read; the rest of *declared* are
    the gaps.  *registrations* are (irq, handler) or (irq, handler,
    argument) the build recorded — the argument being the symbol passed to
    the handler, which for a shim is the real worker.

    *cc_path* writes a compile_commands.json there and records THAT path
    in the index, which is how a real project looks: fw-context generates
    the file under `.fw-context/build/`, away from the build tree.
    *cc_directory* is the `directory` field of its entry; None leaves the
    field out, which is the build system that writes the file into its
    own tree.
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

    if cc_path is not None:
        cc_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"file": "main.c", "command": "cc -c main.c"}
        if cc_directory is not None:
            entry["directory"] = str(cc_directory)
        cc_path.write_text(json.dumps([entry]), encoding="utf-8")

    recorded_cc = cc_path if cc_path is not None else build / "compile_commands.json"
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(
            conn, CONFIG, "pid",
            str(recorded_cc) if cc_in_db else "",
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


def _recorded(tmp_path):
    """The registrations the build wrote, as the handler reads them."""
    return read_isr_registrations(tmp_path / "build")


def _rows(conn, tmp_path):
    recorded = _recorded(tmp_path)
    if recorded is None:
        return []
    return _from_the_build(
        conn, CONFIG, get_table_coverage(conn, CONFIG), recorded,
    )


def test_a_gap_the_build_can_name_becomes_a_row(tmp_path):
    """The whole point: a slot with no name in the index gets one."""
    conn = _index(
        tmp_path,
        registrations=[(5, "uarte_nrfx_isr_int")],
        handlers=[("uarte_nrfx_isr_int", "c:@F@uarte_nrfx_isr_int",
                   "uart_nrfx_uarte.c", 484)],
    )
    try:
        rows = _rows(conn, tmp_path)
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
        assert [row["slot"] for row in _rows(conn, tmp_path)] == [5]
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
        rows = _rows(conn, tmp_path)
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
        rows = _rows(conn, tmp_path)
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
        rows = _rows(conn, tmp_path)
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
        rows = _rows(conn, tmp_path)
        assert rows[0]["name"] == "shared"
        assert (rows[0]["file"], rows[0]["line"]) == ("", 0)
    finally:
        conn.close()


class TestTheSummary:
    """"Which interrupts are unserviced" had no answer on a generated table.

    Measured: ``unhandled_only`` returned zero rows on all eleven images of
    a Zephyr project, against 39 to 72 on four CMSIS and Mbed ones, because
    that answer is read from an alias edge which a generator does not
    write.  The registrations settle it from the other side.
    """

    def test_the_complement_of_the_registrations_is_reported(self, tmp_path):
        _index(tmp_path, declared=8,
               registrations=[(1, "a"), (5, "b"), (6, "c")],
               handlers=[])

        summary = _interrupt_summary(_recorded(tmp_path))
        assert summary is not None
        text = summary["interrupts"]
        assert "8 interrupts in the table, 3 connected by the build" in text
        assert "1, 5-6" in text
        assert "Nothing is connected to the other 5: 0, 2-4, 7." in text

    def test_the_complement_comes_from_the_declared_length(self, tmp_path):
        """NOT from the named slots of a table, which gets mcuboot wrong.

        Measured there: the software table names 44 of 48 slots, but slot 2
        holds ``uarte_0_direct_isr`` — an interrupt wired straight into the
        vector table.  Counting named slots calls that unserviced; counting
        registrations does not.  Here slot 1 is named AND registered, and
        must not appear as unserviced.
        """
        _index(tmp_path, declared=4, named=(0, 1, 2),
               registrations=[(1, "direct_isr")],
               handlers=[("direct_isr", "c:@F@direct_isr", "drv.c", 7)])

        summary = _interrupt_summary(_recorded(tmp_path))
        assert summary is not None
        assert "4 interrupts in the table, 1 connected by the build: 1." in summary["interrupts"]
        assert "Nothing is connected to the other 3: 0, 2-3." in summary["interrupts"]

    def test_run_time_registration_stops_the_complement_being_asserted(
        self, tmp_path,
    ):
        """With CONFIG_DYNAMIC_INTERRUPTS the list is not the whole story.

        Claiming the rest are unserviced would then be a guess, so only the
        connected part is stated as fact.
        """
        _index(tmp_path, declared=8, registrations=[(1, "a")], handlers=[],
               config_text="CONFIG_DYNAMIC_INTERRUPTS=y\n")

        summary = _interrupt_summary(_recorded(tmp_path))
        assert summary is not None
        text = summary["interrupts"]
        assert "1 connected by the build: 1." in text
        assert "may be connected at run time" in text
        assert "Nothing is connected" not in text

    def test_a_build_that_registered_nothing_says_nothing(self, tmp_path):
        """No claim to make, so no row."""
        _index(tmp_path, declared=8, registrations=[], handlers=[])

        assert _interrupt_summary(_recorded(tmp_path)) is None


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
        assert _rows(conn, tmp_path) == []
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
        assert _rows(conn, tmp_path) == []
    finally:
        conn.close()


class TestTheBuildTree:
    """Where the handler looks for the artifact.

    Measured on the Zephyr project: the index records
    `.fw-context/build/compile_commands.nrf52840-dev.stage0.json`, whose
    parent holds nothing, while the entries of that file name
    `build/nrf52840_dev/stage0` — where the artifact is.  Taking the
    parent made all nine images answer "no registrations".
    """

    def test_the_directory_field_names_the_tree(self, tmp_path):
        """A compile_commands.json outside the tree still finds it."""
        conn = _index(
            tmp_path,
            registrations=[(5, "uarte_nrfx_isr_int")],
            handlers=[("uarte_nrfx_isr_int", "c:@F@uarte_nrfx_isr_int",
                       "uart_nrfx_uarte.c", 484)],
            cc_path=tmp_path / ".fw-context" / "build" / "compile_commands.json",
            cc_directory=tmp_path / "build",
        )
        try:
            assert _build_directory(conn, CONFIG) == tmp_path / "build"
            # And the whole path works: the registration becomes a row.
            rows = _vector_rows(
                conn, CONFIG, unhandled_only=False, limit=400,
            )
            assert any(
                row.get("slot") == 5 and row.get("source") == "build"
                for row in rows
            )
        finally:
            conn.close()

    def test_without_the_field_the_parent_answers(self, tmp_path):
        """CMake writes the file into the tree, and that stays right."""
        conn = _index(
            tmp_path,
            registrations=[],
            cc_path=tmp_path / "build" / "compile_commands.json",
            cc_directory=None,
        )
        try:
            assert _build_directory(conn, CONFIG) == tmp_path / "build"
        finally:
            conn.close()

    def test_no_compile_commands_in_the_index_answers_none(self, tmp_path):
        """Nothing recorded is not a directory to guess at."""
        conn = _index(tmp_path, registrations=[], cc_in_db=False)
        try:
            assert _build_directory(conn, CONFIG) is None
        finally:
            conn.close()


class TestTheAnswerSurvives:
    """Two ways the answer used to be lost on the way out."""

    def test_unhandled_only_reports_the_summary(self, tmp_path):
        """A generated table has no alias edge, and used to say so wrong.

        Measured: all nine images of the Zephyr project answered "No
        vector table in this build" to `unhandled_only`, because the
        early return ran before the registrations were read.
        """
        conn = _index(
            tmp_path,
            declared=8,
            named=(0, 1, 2),
            registrations=[(5, "uarte_nrfx_isr_int")],
            handlers=[("uarte_nrfx_isr_int", "c:@F@uarte_nrfx_isr_int",
                       "uart_nrfx_uarte.c", 484)],
        )
        try:
            rows = _vector_rows(
                conn, CONFIG, unhandled_only=True, limit=400,
            )
            assert any("interrupts" in row for row in rows)
            assert not any(
                "No vector table in this build" in str(row.get("info", ""))
                for row in rows
            )
        finally:
            conn.close()

    def test_the_summary_outlives_the_limit(self, tmp_path):
        """The longest table is where the summary matters most.

        Measured: 573 generated slots plus 11 assembly ones against the
        default limit of 400 cut off both `coverage` and `interrupts`.
        """
        conn = _index(
            tmp_path,
            declared=8,
            named=(0, 1, 2),
            registrations=[(5, "uarte_nrfx_isr_int")],
            handlers=[("uarte_nrfx_isr_int", "c:@F@uarte_nrfx_isr_int",
                       "uart_nrfx_uarte.c", 484)],
        )
        try:
            rows = _vector_rows(conn, CONFIG, unhandled_only=False, limit=2)
            keys = [next(iter(row)) if len(row) == 1 else "slot" for row in rows]
            assert "truncated" in keys
            assert "coverage" in keys
            assert "interrupts" in keys
            # The cut still happened, and the notice comes before the
            # lines that describe the whole table.
            assert keys.index("truncated") < keys.index("coverage")
        finally:
            conn.close()

    def test_neither_source_answers_and_that_is_said_once(self, tmp_path):
        """No table and no artifact keeps the original message."""
        conn = _index(tmp_path, declared=8, named=(), registrations=[])
        try:
            rows = _vector_rows(conn, CONFIG, unhandled_only=False, limit=400)
            assert len(rows) == 1
            assert "No vector table in this build" in rows[0]["info"]
        finally:
            conn.close()
