"""The resolution of the slot facts into call edges (indexer/_slot_resolution.py).

The facts go into the tables directly, thus each test shows one rule of the
resolution without a parse: the origins, the fixed point over the arguments,
the type-only targets, and the replacement of the edges at each run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.models import RefOrigin

SLOT = int(RefOrigin.SLOT)
TYPE_ONLY = int(RefOrigin.TYPE_ONLY)


@pytest.fixture
def conn(tmp_path: Path):
    from fw_context_mcp.indexer.db import open_db

    connection = open_db(tmp_path / "index.db")
    yield connection
    connection.close()


def _facts(conn, *, sources=(), flows=(), sites=(), arguments=(), escapes=()) -> None:
    """Write facts; a site is (line, caller, kind, slot, slot_type[, callee]) in file m.c, column 1, ordinal 0."""
    from fw_context_mcp.indexer.db import insert_slot_facts, transaction

    with transaction(conn):
        insert_slot_facts(
            conn, "ch",
            [("m.c", 1, 1, function, function_type, slot) for function, function_type, slot in sources],
            [("m.c", 1, 1, source, target) for source, target in flows],
            [("m.c", line, 1, 0, caller, kind, slot, slot_type, "", (rest or [""])[0])
             for line, caller, kind, slot, slot_type, *rest in sites],
            [("m.c", line, 1, 0, index, value_kind, value) for line, index, value_kind, value in arguments],
            [("m.c", 1, 1, slot, "address taken") for slot in escapes],
        )


def _edges(conn) -> set[tuple]:
    from fw_context_mcp.indexer._slot_resolution import resolve_slot_edges
    from fw_context_mcp.indexer.db import transaction

    with transaction(conn):
        resolve_slot_edges(conn, "ch")
    return {
        (row["from_usr"], row["to_usr"], row["from_line"], row["ref_kind"], row["origins"])
        for row in conn.execute("SELECT * FROM refs WHERE config_hash = 'ch'")
    }


def test_a_source_that_flows_into_the_slot_of_a_site_is_a_target(conn):
    _facts(
        conn,
        sources=[("on_rx", "void (int)", "P|set_cb|0")],
        flows=[("P|set_cb|0", "V|g_cb")],
        sites=[(7, "fire", "pointer", "V|g_cb", "void (int)")],
    )
    assert _edges(conn) == {("fire", "on_rx", 7, "call", SLOT)}


def test_an_argument_reaches_the_parameter_of_each_target_until_nothing_is_new(conn):
    """`run` calls `apply(g_cb)` through a pointer; `apply` calls its parameter."""
    _facts(
        conn,
        sources=[("apply", "void (h, int)", "P|run|0"), ("on_rx", "void (int)", "V|g_cb")],
        sites=[
            (5, "run", "pointer", "P|run|0", "void (h, int)"),
            (6, "apply", "pointer", "P|apply|0", "void (int)"),
        ],
        arguments=[(5, 0, "slot", "V|g_cb")],
    )
    assert _edges(conn) == {("run", "apply", 5, "call", SLOT), ("apply", "on_rx", 6, "call", SLOT)}


def test_an_escaped_slot_adds_each_function_of_its_type(conn):
    _facts(
        conn,
        sources=[("first", "void ()", "V|hook"), ("second", "void ()", ""), ("other", "int ()", "")],
        sites=[(9, "tick", "pointer", "V|hook", "void ()")],
        escapes=["V|hook"],
    )
    assert _edges(conn) == {("tick", "first", 9, "call", SLOT), ("tick", "second", 9, "call", TYPE_ONLY)}


def test_an_escape_flows_on_with_the_value(conn):
    _facts(
        conn,
        sources=[("first", "void ()", "V|hook"), ("second", "void ()", "")],
        flows=[("V|hook", "V|copy")],
        sites=[(9, "tick", "pointer", "V|copy", "void ()")],
        escapes=["V|hook"],
    )
    assert ("tick", "second", 9, "call", TYPE_ONLY) in _edges(conn)


def test_a_site_with_no_slot_gets_the_functions_of_its_type(conn):
    _facts(
        conn,
        sources=[("first", "void ()", ""), ("lam", "lambda", "")],
        sites=[(3, "a", "pointer", "", "void ()")],
    )
    assert _edges(conn) == {("a", "first", 3, "call", TYPE_ONLY)}


def test_a_slot_that_no_code_fills_and_that_does_not_escape_gives_no_edge(conn):
    """A HAL field that the build never sets (`mutecallback`) is no call of each `void ()` handler."""
    _facts(
        conn,
        sources=[("first", "void ()", "")],
        sites=[(4, "b", "pointer", "V|empty", "void ()")],
    )
    assert _edges(conn) == set()


def test_an_object_site_passes_its_arguments_to_its_operator_and_gets_no_type_only_target(conn):
    _facts(
        conn,
        sources=[("lam", "lambda", "V|w"), ("h1", "void (int)", "")],
        sites=[
            (11, "use", "object", "V|w", "W:fn", "Functor::operator()"),
            (12, "Functor::operator()", "pointer", "P|Functor::operator()|0", "void (int)"),
            (13, "use", "object", "V|none", "W:fn", "Other::operator()"),
        ],
        arguments=[(11, 0, "function", "h1")],
    )
    assert _edges(conn) == {
        ("use", "lam", 11, "call", SLOT),
        ("Functor::operator()", "h1", 12, "call", SLOT),
    }


def test_a_second_run_replaces_the_edges(conn):
    _facts(
        conn,
        sources=[("on_rx", "void (int)", "V|g_cb")],
        sites=[(7, "fire", "pointer", "V|g_cb", "void (int)")],
    )
    assert _edges(conn) == {("fire", "on_rx", 7, "call", SLOT)}
    # No source is left, and the slot does not escape: no edge.
    conn.execute("DELETE FROM slot_sources")
    assert _edges(conn) == set()


def test_an_address_site_gets_no_target(conn):
    """A jump to a number (a bootloader that starts the application) reaches no code of the build."""
    _facts(
        conn,
        sources=[("SysTick_Handler", "void ()", "V|vectors")],
        sites=[(4, "jump", "address", "", "void ()")],
    )
    assert _edges(conn) == set()


def test_an_index_of_the_earlier_slot_tables_gets_the_new_column(tmp_path: Path):
    """An index made before `slot_sites.callee` takes the facts of a new run."""
    from fw_context_mcp.indexer.db import insert_slot_facts, open_db, transaction

    path = tmp_path / "old.db"
    old = open_db(path)
    old.execute("ALTER TABLE slot_sites DROP COLUMN callee")
    old.commit()
    old.close()
    connection = open_db(path)
    try:
        with transaction(connection):
            insert_slot_facts(connection, "ch", [], [], [("m.c", 1, 1, 0, "f", "pointer", "", "void ()", "", "")], [], [])
        assert connection.execute("SELECT callee FROM slot_sites").fetchone()[0] == ""
    finally:
        connection.close()


def test_a_row_of_an_other_analysis_keeps_its_row_and_loses_the_bits(conn):
    from fw_context_mcp.indexer.db import insert_refs_batch, transaction

    with transaction(conn):
        insert_refs_batch(conn, [("ch", "on_rx", "m.c", 7, "fire", "call", None, 1, int(RefOrigin.DIRECT))])
    _facts(
        conn,
        sources=[("on_rx", "void (int)", "V|g_cb")],
        sites=[(7, "fire", "pointer", "V|g_cb", "void (int)")],
    )
    assert _edges(conn) == {("fire", "on_rx", 7, "call", int(RefOrigin.DIRECT) | SLOT)}
    conn.execute("DELETE FROM slot_sources")
    assert _edges(conn) == {("fire", "on_rx", 7, "call", int(RefOrigin.DIRECT))}
