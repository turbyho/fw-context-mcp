"""PageRank and the hotspot cache follow the call graph after each index run.

An incremental run deletes the symbols of each translation unit that it
parses again, and inserts them with new ids.  The foreign key of
``hotspot_cache.symbol_id`` has ``ON DELETE CASCADE``, thus the cache rows
of those symbols go too.  Before this fix, the post-process step saw the
cache rows of the other files and did not compute again: ``find_hotspots``
then did not show a function of a changed file, and its PageRank stayed 0.
The data of one firmware index showed this: a full run gave 2866 cache rows
where the incremental index had 2590.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer._postprocess import _build_pagerank, _step_pagerank_hotspot
from fw_context_mcp.indexer.db import (
    delete_symbols_for_file,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

CH = "hash-rank"


def _function(fid: int, path: str, name: str) -> tuple:
    return (
        CH, fid, path, name, f"u_{name}", name, name, "function",
        10, 1, 20, 1, "", "", None, 0, 0, "",
        0, "", 1, 0.0, "", 0,
    )


def _call(caller: str, callee: str, path: str, line: int) -> tuple:
    return (CH, f"u_{callee}", path, line, f"u_{caller}", "call", None)


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", str(tmp_path))
        upsert_build_config(conn, CH, "proj-001", str(tmp_path / "compile_commands.json"))
        main_id = upsert_file(conn, CH, "src/main.c", "c")
        util_id = upsert_file(conn, CH, "src/util.c", "c")
        insert_symbols_batch(conn, [
            _function(main_id, "src/main.c", "main"),
            # A function of the file that does not change.  Its cache row
            # stays, thus the cache is not empty after the cascade.
            _function(main_id, "src/main.c", "factorial"),
            _function(util_id, "src/util.c", "helper_a"),
            _function(util_id, "src/util.c", "helper_b"),
        ])
        insert_refs_batch(conn, [
            _call("main", "helper_a", "src/main.c", 12),
            _call("main", "helper_b", "src/main.c", 13),
            _call("main", "factorial", "src/main.c", 14),
            _call("helper_b", "helper_a", "src/util.c", 15),
        ])
    yield conn, main_id, util_id
    conn.close()


def _ctx(tmp_path: Path) -> dict:
    # The context of a run with no --force, that is an incremental run.
    return {"config_hash": CH, "force": False, "db_dir": tmp_path}


def _hotspots(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT s.name, h.caller_count FROM hotspot_cache h JOIN symbols s ON s.id = h.symbol_id"
        " WHERE h.config_hash = ?",
        (CH,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _ranked(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM symbols WHERE config_hash = ? AND pagerank > 0", (CH,)
    ).fetchall()
    return {row[0] for row in rows}


def test_an_incremental_run_ranks_the_symbols_of_a_changed_file(db, tmp_path: Path):
    """The symbols of util.c come back with new ids, and the step ranks them again."""
    conn, _main_id, util_id = db
    _step_pagerank_hotspot(conn, _ctx(tmp_path))
    assert _hotspots(conn) == {"factorial": 1, "helper_a": 2, "helper_b": 1}

    # The parse of util.c again, as an incremental run does it: delete the
    # symbols of the file (the cascade removes their cache rows), insert
    # them again, and add a new caller of helper_a.
    with transaction(conn):
        delete_symbols_for_file(conn, util_id)
        insert_symbols_batch(conn, [
            _function(util_id, "src/util.c", "helper_a"),
            _function(util_id, "src/util.c", "helper_b"),
            _function(util_id, "src/util.c", "helper_c"),
        ])
        insert_refs_batch(conn, [_call("helper_c", "helper_a", "src/util.c", 16)])

    _step_pagerank_hotspot(conn, _ctx(tmp_path))

    assert _hotspots(conn) == {"factorial": 1, "helper_a": 3, "helper_b": 1}
    assert _ranked(conn) == {"main", "factorial", "helper_a", "helper_b", "helper_c"}


def test_a_function_that_leaves_the_call_graph_loses_its_rank(db):
    """A score from an earlier run must not stay on a symbol with no call edge."""
    conn, _main_id, _util_id = db
    _build_pagerank(conn, CH)
    assert "helper_b" in _ranked(conn)

    with transaction(conn):
        conn.execute("DELETE FROM refs WHERE config_hash = ? AND (to_usr = 'u_helper_b' OR from_usr = 'u_helper_b')", (CH,))

    _build_pagerank(conn, CH)

    assert _ranked(conn) == {"main", "factorial", "helper_a"}


def test_no_call_edge_clears_every_rank(db):
    """With no call edge left, no symbol keeps the score of an earlier run."""
    conn, _main_id, _util_id = db
    _build_pagerank(conn, CH)
    with transaction(conn):
        conn.execute("DELETE FROM refs WHERE config_hash = ?", (CH,))

    _build_pagerank(conn, CH)

    assert _ranked(conn) == set()
