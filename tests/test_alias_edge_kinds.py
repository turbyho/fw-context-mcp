"""The synthetic weak-alias edges of the recursive call-graph queries.

``find_all_callers_recursive`` and ``find_callees_recursive`` add an edge
through each weak-alias pair (``uart_irq`` → ``__uart_irq``).  The real
edges take only call kinds; the synthetic ones once took every row, thus a
function that only took the address of the alias became its caller.
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
)
from fw_context_mcp.indexer.db._callgraph import find_all_callers_recursive, find_callees_recursive

CH = "hash-alias"


def _symbol(fid: int, name: str, usr: str, line: int, *, kind: str = "function", definition: bool = True) -> tuple:
    return (
        CH, fid, "src/uart.c", name, usr, name, name, kind,
        line, 1, line + 5, int(definition), f"void {name}(void)", "", None, 0, 0, "",
        0, "", 1, 0.0, "", 0,
    )


def _ref(to_usr: str, line: int, from_usr: str, ref_kind: str) -> tuple:
    return (CH, to_usr, "src/uart.c", line, from_usr, ref_kind, None)


@pytest.fixture
def db():
    tmpdir = Path(tempfile.mkdtemp())
    conn = open_db(tmpdir / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", "/tmp/test")
        upsert_build_config(conn, CH, "proj-001", "/tmp/compile_commands.json")
    fid = upsert_file(conn, CH, "src/uart.c", "c")
    insert_symbols_batch(conn, [
        # The weak alias: a declaration only, with a __-prefixed definition.
        _symbol(fid, "uart_irq", "u_alias", 1, definition=False),
        _symbol(fid, "__uart_irq", "u_def", 10),
        _symbol(fid, "main", "u_main", 20),
        _symbol(fid, "table_init", "u_table", 30),
        _symbol(fid, "hw_read", "u_hw_read", 40),
        _symbol(fid, "g_buf", "u_g_buf", 50, kind="varglobal"),
    ])
    insert_refs_batch(conn, [
        _ref("u_alias", 21, "u_main", "call"),
        # Takes the address of the alias: no call.
        _ref("u_alias", 31, "u_table", "ref"),
        _ref("u_hw_read", 11, "u_def", "call"),
        # Reads a variable: no call.
        _ref("u_g_buf", 12, "u_def", "ref"),
    ])
    yield conn
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_a_reference_to_the_alias_is_no_caller_of_the_definition(db):
    names = {row["name"] for row in find_all_callers_recursive(db, CH, "__uart_irq")}

    assert names == {"main"}


def test_a_reference_of_the_definition_is_no_callee_of_the_alias(db):
    names = {row["name"] for row in find_callees_recursive(db, CH, "uart_irq")}

    assert names == {"hw_read"}


def test_a_single_underscore_definition_is_an_alias_target_too(tmp_path):
    """``_spi_irq`` is a candidate as well as ``__spi_irq``: the LIKE filter keeps both."""
    conn = open_db(tmp_path / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", "/tmp/test")
        upsert_build_config(conn, CH, "proj-001", "/tmp/compile_commands.json")
    fid = upsert_file(conn, CH, "src/uart.c", "c")
    insert_symbols_batch(conn, [
        _symbol(fid, "spi_irq", "u_spi_alias", 1, definition=False),
        _symbol(fid, "_spi_irq", "u_spi_def", 10),
        _symbol(fid, "main", "u_main", 20),
    ])
    insert_refs_batch(conn, [_ref("u_spi_alias", 21, "u_main", "call")])

    names = {row["name"] for row in find_all_callers_recursive(conn, CH, "_spi_irq")}
    conn.close()

    assert names == {"main"}


# ── The alias cache ─────────────────────────────────────────────────────────


def test_a_new_connection_never_reads_the_pairs_of_a_closed_one(tmp_path):
    """The cache was keyed by id(conn), and CPython gives that id to the next object.

    Forty short connections, each to its own database, make the reuse
    of an id likely; each must answer from its own database.
    """
    for i in range(40):
        conn = open_db(tmp_path / f"db{i}.db")
        with transaction(conn):
            upsert_project(conn, "proj-001", "test", "/tmp/test")
            upsert_build_config(conn, CH, "proj-001", "/tmp/compile_commands.json")
        fid = upsert_file(conn, CH, "src/uart.c", "c")
        alias, target = f"irq{i}", f"__irq{i}"
        insert_symbols_batch(conn, [
            _symbol(fid, alias, f"u_alias{i}", 1, definition=False),
            _symbol(fid, target, f"u_def{i}", 10),
            _symbol(fid, "main", "u_main", 20),
        ])
        insert_refs_batch(conn, [_ref(f"u_alias{i}", 21, "u_main", "call")])
        conn.commit()

        names = {row["name"] for row in find_all_callers_recursive(conn, CH, target)}
        conn.close()

        assert names == {"main"}, f"database {i}"


def test_a_reindex_by_another_connection_drops_the_cached_pairs(tmp_path):
    """The executor keeps one connection; an index run commits through another."""
    reader = open_db(tmp_path / "test.db")
    with transaction(reader):
        upsert_project(reader, "proj-001", "test", "/tmp/test")
        upsert_build_config(reader, CH, "proj-001", "/tmp/compile_commands.json")
    fid = upsert_file(reader, CH, "src/uart.c", "c")
    insert_symbols_batch(reader, [
        _symbol(fid, "dma_irq", "u_dma_alias", 1, definition=False),
        _symbol(fid, "main", "u_main", 20),
    ])
    insert_refs_batch(reader, [_ref("u_dma_alias", 21, "u_main", "call")])
    reader.commit()
    # Fills the cache: dma_irq resolves, and the walk reads the alias pairs,
    # of which there are none yet.
    assert {row["name"] for row in find_all_callers_recursive(reader, CH, "dma_irq")} == {"main"}

    writer = open_db(tmp_path / "test.db")
    insert_symbols_batch(writer, [_symbol(fid, "__dma_irq", "u_dma_def", 10)])
    writer.commit()
    writer.close()

    names = {row["name"] for row in find_all_callers_recursive(reader, CH, "__dma_irq")}
    reader.close()

    assert names == {"main"}
