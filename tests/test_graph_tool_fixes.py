"""Answers of the graph tools: notes, name matching, paths, and info texts.

- ``find_indirect_targets`` keeps the ``_note`` of the ``fn_ptr_type``
  fallback, and matches ``_`` in a name as itself.
- ``trace_data_flow`` keeps the ambiguity ``warning`` out of its three paths.
- ``find_hotspots`` does not claim "no references" after the refs guard
  found some.
- ``find_dead_code`` and ``get_vector_table`` give absolute paths.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from fw_context_mcp.indexer.db import (
    insert_fp_assignments_batch,
    insert_indirect_call_sites_batch,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.indexer.db._refs import find_indirect_targets as db_find_indirect_targets
from fw_context_mcp.mcp.handlers import callgraph
from fw_context_mcp.utils import compute_source_hash

CH = "hash-graph"


def _symbol(fid: int, name: str, usr: str, *, kind: str = "function", signature: str = "") -> tuple:
    return (
        CH, fid, "src/drv.c", name, usr, name, name, kind,
        10, 1, 20, 1, signature, "", None, 0, 0, "",
        0, "", 1, 0.0, "", 0,
    )


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", str(tmp_path))
        upsert_build_config(conn, CH, "proj-001", str(tmp_path / "compile_commands.json"))
    # The indexed file exists: a row of a file that is gone reads as a
    # change, and an empty result then carries a warning.
    drv = tmp_path / "src" / "drv.c"
    drv.parent.mkdir()
    drv.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    fid = upsert_file(conn, CH, "src/drv.c", "c", mtime=drv.stat().st_mtime, source_hash=compute_source_hash(drv))
    insert_symbols_batch(conn, [
        _symbol(fid, "main", "u_main"),
        _symbol(fid, "on_rx_done", "u_on_rx_done"),
        # A field whose type names no indexed class: the type fallback
        # fails, and the fn_ptr_type fallback runs.
        _symbol(fid, "onData", "u_on_data", kind="field", signature="Callback onData"),
    ])
    insert_refs_batch(conn, [(CH, "u_on_rx_done", "src/drv.c", 30, "u_main", "call", None)])
    insert_fp_assignments_batch(conn, [
        (CH, "src/drv.c", 40, "u_on_data", "onData", "u_on_rx_done", "on_rx_done",
         "void (*)(int)", "assignment", "u_main"),
    ])
    # A call through ANOTHER pointer of the same type.
    insert_indirect_call_sites_batch(conn, [
        (CH, "src/other.c", 77, "u_main", "other_cb(1)", "u_other_cb", "other_cb", "void (*)(int)"),
    ])
    conn.commit()
    yield conn
    conn.close()


def _handler_db(tmp_path: Path):
    from fw_context_mcp.mcp.handlers._base import BaseHandler, DbContext
    from fw_context_mcp.mcp.shared.executor import SyncQueryExecutor

    db_path = tmp_path / "test.db"
    ctx = DbContext(
        db_path=db_path,
        executor=SyncQueryExecutor(str(db_path.resolve()), db_path),
        config_hash=CH,
        project_id="proj-001",
        root=tmp_path,
        cfg=None,
    )
    return mock.patch.object(BaseHandler, "resolve_db_context", return_value=ctx)


def _without_stale_warning(rows: list[dict]) -> list[dict]:
    """The indexed files are not on disk, thus each answer leads with a stale warning."""
    return [r for r in rows if not (set(r) == {"warning"} and "stale" in r["warning"])]


# ── find_indirect_targets ───────────────────────────────────────────────────


def test_an_underscore_in_the_name_matches_itself(db):
    """``rx_done`` reaches ``on_rx_done`` through the substring match."""
    rows = db_find_indirect_targets(db, CH, "rx_done")

    assert [r["rhs_name"] for r in rows] == ["on_rx_done"]


def test_an_underscore_is_no_wildcard(db):
    assert db_find_indirect_targets(db, CH, "onXrx") == []
    assert db_find_indirect_targets(db, CH, "on_rx_donX") == []


def test_the_fn_ptr_type_note_reaches_the_answer(db, tmp_path):
    from tests._paging import answers

    with _handler_db(tmp_path):
        rows = answers(_without_stale_warning(
            callgraph.find_indirect_targets(name="onData", project_root=str(tmp_path))
        ))

    assert len(rows) == 1, rows
    assert rows[0]["call_line"] == 77
    assert "fn_ptr_type" in rows[0]["_note"]


# ── trace_data_flow ─────────────────────────────────────────────────────────


def test_the_ambiguity_warning_takes_no_path_slot(db, tmp_path):
    insert_symbols_batch(db, [
        _symbol(1, "pack", "u_pack", signature="void pack(SensorData *d)"),
        _symbol(1, "uart_send", "u_uart_send"),
    ])
    db.commit()
    paths = [{"warning": "'uart_send' matches 2 symbols"}] + [
        {"depth": 1, "chain": f"pack → uart_send#{i}", "target_usr": f"u{i}"} for i in range(4)
    ]
    with _handler_db(tmp_path), mock.patch.object(
        callgraph.index_db, "search_call_paths", return_value=(paths, "complete"),
    ):
        rows = _without_stale_warning(callgraph.trace_data_flow(
            type_name="SensorData", to_symbol="uart_send", project_root=str(tmp_path),
        ))

    # Found by its key: the page notice and the _summary row come before it.
    entry = next(r for r in rows if "source_name" in r)
    assert entry["reachable"] is True
    assert [p["chain"] for p in entry["paths"]] == [f"pack → uart_send#{i}" for i in range(3)]
    assert entry["warning"] == "'uart_send' matches 2 symbols"


# ── find_hotspots ───────────────────────────────────────────────────────────


def test_no_hotspot_with_references_says_so(db, tmp_path):
    with _handler_db(tmp_path), \
            mock.patch.object(callgraph.index_db, "find_hotspots", return_value=[]), \
            mock.patch.object(callgraph.index_db, "count_hotspots", return_value=0):
        rows = _without_stale_warning(callgraph.find_hotspots(project_root=str(tmp_path), project_only=False))

    assert rows == [{"info": "No hotspots found: no function has a call or indirect reference."}]


# ── absolute paths ──────────────────────────────────────────────────────────


def test_absolute_sites(tmp_path):
    sites = callgraph._absolute_sites("src/a.c:10, /abs/b.c:20", tmp_path)

    assert sites == f"{tmp_path / 'src/a.c'}:10, /abs/b.c:20"


def test_absolute_vector_paths_reach_the_nested_entries(tmp_path):
    row = {
        "slot": 44, "file": "src/timer.c", "table_file": "startup.S",
        "overridden": {"file": "startup.S", "line": 412},
        "aliases": {"name": "Default_Handler", "file": "startup.S", "line": 380},
        "installed": [{"name": "h", "file": "src/h.c", "line": 3, "at": "src/init.c:9"}],
    }
    build_row = {"slot": 89, "file": "", "table_file": "isr_tables.c"}

    out = callgraph._absolute_vector_paths([row, build_row, {"coverage": "…"}], tmp_path)

    assert out[0]["file"] == str(tmp_path / "src/timer.c")
    assert out[0]["table_file"] == str(tmp_path / "startup.S")
    assert out[0]["overridden"]["file"] == str(tmp_path / "startup.S")
    assert out[0]["aliases"]["file"] == str(tmp_path / "startup.S")
    assert out[0]["installed"][0]["file"] == str(tmp_path / "src/h.c")
    assert out[0]["installed"][0]["at"] == f"{tmp_path / 'src/init.c'}:9"
    assert out[1]["file"] == ""
    assert out[2] == {"coverage": "…"}
