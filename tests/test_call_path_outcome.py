"""find_call_path says why its search stopped, and gives the same paths each time.

The number of call paths cannot be counted, thus the tool has no page
notice.  It reported a full list of five paths, and an empty list after
the node budget, the same way as a complete search: "No path found"
after a spent budget is no proof that no path exists.

The walk iterated Python sets of USR strings, whose order changes with
the hash seed of each process, and the edge queries had no ORDER BY.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    CALL_PATHS_BUDGET,
    CALL_PATHS_CAPPED,
    CALL_PATHS_COMPLETE,
    CALL_PATHS_DIRECT,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    search_call_paths,
)
from tests._paging import CH, PROJECT_ID, make_project, symbol_row

MIDDLES = 8   # main calls mid0..mid7, each calls sink: eight paths


def _fill(conn, file_ids: list[int]) -> None:
    fid, path = file_ids[0], "src/f0.c"
    rows = [symbol_row(fid, path, "main", "main", "c:@F@main", 1),
            symbol_row(fid, path, "sink", "sink", "c:@F@sink", 2),
            symbol_row(fid, path, "lonely", "lonely", "c:@F@lonely", 3)]
    refs = []
    for i in range(MIDDLES):
        usr = f"c:@F@mid{i}"
        rows.append(symbol_row(fid, path, f"mid{i}", f"mid{i}", usr, 10 + i))
        refs.append((CH, usr, path, 100 + i, "c:@F@main", "call", None))
        refs.append((CH, "c:@F@sink", path, 200 + i, usr, "call", None))
    # lonely has callers of its own, none of which main reaches: both fronts
    # of the walk keep nodes, thus a small budget is what stops it.
    for i in range(MIDDLES):
        usr = f"c:@F@island{i}"
        rows.append(symbol_row(fid, path, f"island{i}", f"island{i}", usr, 50 + i))
        refs.append((CH, "c:@F@lonely", path, 300 + i, usr, "call", None))
    insert_symbols_batch(conn, rows)
    insert_refs_batch(conn, refs)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill)


@pytest.fixture
def conn(project: Path, tmp_path: Path):
    db = open_db(tmp_path / PROJECT_ID / "index.db")
    yield db
    db.close()


def test_a_full_list_of_paths_says_so(conn):
    paths, outcome = search_call_paths(conn, CH, "main", "sink")
    assert len(paths) == 5
    assert outcome == CALL_PATHS_CAPPED


def test_a_complete_search_says_so(conn):
    """No path from main to lonely: the walk ends when the forward front is empty."""
    paths, outcome = search_call_paths(conn, CH, "main", "lonely")
    assert paths == []
    assert outcome == CALL_PATHS_COMPLETE


def test_a_direct_call_says_that_longer_paths_were_not_searched(conn, project):
    from fw_context_mcp.mcp.handlers.callgraph import find_call_path

    _paths, outcome = search_call_paths(conn, CH, "mid0", "sink")
    assert outcome == CALL_PATHS_DIRECT
    rows = find_call_path("mid0", "sink", project_root=str(project))
    assert rows[-1] == {"info": "'mid0' calls 'sink' directly. Longer paths were not searched."}


def test_a_search_that_spends_its_budget_says_so(conn):
    """Both fronts still hold nodes when the budget runs out."""
    paths, outcome = search_call_paths(conn, CH, "main", "lonely", max_nodes=3)
    assert paths == []
    assert outcome == CALL_PATHS_BUDGET


def test_the_depth_bound_is_no_spent_budget(conn):
    """One level expands 16 nodes, above a budget of 3, and the depth of 1 ends the walk.

    A level always runs to its end, thus the count passes the budget on
    the last level.  The depth stopped this walk, not the budget.
    """
    _paths, outcome = search_call_paths(conn, CH, "main", "lonely", max_depth=1, max_nodes=3)
    assert outcome == CALL_PATHS_COMPLETE


def test_the_paths_come_in_a_fixed_order(conn):
    """The edges come in USR order: mid0 .. mid4 are the five paths."""
    paths, _ = search_call_paths(conn, CH, "main", "sink")
    assert [p["chain"] for p in paths] == [f"main → mid{i} → sink" for i in range(5)]


def test_the_handler_names_the_cap(project):
    from fw_context_mcp.mcp.handlers.callgraph import find_call_path

    rows = find_call_path("main", "sink", project_root=str(project))
    assert rows[-1] == {"info": "The answer holds the first 5 paths that the search found. "
                                "More paths can exist."}


def test_the_handler_does_not_call_a_spent_budget_no_path(project, monkeypatch):
    from fw_context_mcp.indexer import db as index_db
    from fw_context_mcp.mcp.handlers.callgraph import find_call_path

    real = index_db.search_call_paths
    monkeypatch.setattr(index_db, "search_call_paths",
                        lambda *a, **kw: real(*a, **{**kw, "max_nodes": 3}))
    rows = find_call_path("main", "lonely", project_root=str(project))
    assert len(rows) == 1
    assert "A path can still exist" in rows[0]["info"]
    assert "within depth" not in rows[0]["info"]
