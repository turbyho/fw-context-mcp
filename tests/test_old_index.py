"""An index of an older version: the columns of a newer version are not there.

Only `open_db` of an index run adds the columns of `_MIGRATION_ADD_COLUMNS`;
the MCP server reads the database without a migration.  An index that a
version before the build columns (``variant``, ``image``, ``board``) wrote
made each row read fail with ``IndexError: No item with that key``: each
tool gave "Internal server error", the status failed before it found the
old schema, and the daemon stopped at its start, thus no run repaired it.

The fixture removes the three columns from the index of ``two_builds``, as
an older version wrote it.
"""

from __future__ import annotations

import pytest

from tests.test_reindex_each_build import two_builds  # noqa: F401  (fixture)

DROPPED = ("variant", "image", "board")


@pytest.fixture
def old_index(two_builds):  # noqa: F811
    """``(root, db_path)`` of an index without the build columns."""
    from fw_context_mcp.indexer.db import open_db

    root, db_path = two_builds
    conn = open_db(db_path, skip_integrity_check=True)
    try:
        for column in DROPPED:
            conn.execute(f"ALTER TABLE build_configs DROP COLUMN {column}")
        conn.commit()
    finally:
        conn.close()
    return root, db_path


def test_the_missing_columns_are_named(old_index):
    import sqlite3

    from fw_context_mcp.indexer.db import missing_columns

    _, db_path = old_index
    conn = sqlite3.connect(db_path)
    try:
        assert set(missing_columns(conn)) == {f"build_configs.{column}" for column in DROPPED}
    finally:
        conn.close()


def test_the_status_names_the_old_schema(old_index):
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info, get_project_status

    root, _ = old_index

    for state in (get_project_status(project_root=str(root)), get_build_info(project_root=str(root))):
        assert state["status"] == "reindex_needed", state
        assert state["reindex_reasons"][0].startswith("schema_mismatch: an older version wrote this index"), state
        assert "build_configs.variant" in state["reindex_reasons"][0]
        assert "Run `fw-context index`" in state["reindex_reasons"][0]


def test_a_query_gets_the_old_schema_and_the_status_tool(old_index):
    """No command: the query does not know the state of the run that adds the columns."""
    import asyncio

    from fw_context_mcp.mcp import server

    root, _ = old_index

    out = asyncio.run(server.mcp.call_tool("lookup_symbol", {"name": "main_value", "project_root": str(root)}))

    rows = out[1]["result"]
    assert rows == [{"error": rows[0]["error"]}], "no state of a build: the index has no build of this version"
    assert rows[0]["error"].startswith("schema_mismatch: an older version wrote this index"), rows
    assert rows[0]["error"].endswith("To see the action, call get_project_status().")


def test_the_daemon_starts_the_run_that_adds_the_columns(old_index):
    from fw_context_mcp.mcp.daemon import _staleness_check

    root, _ = old_index

    needed, reasons = _staleness_check(root)

    assert needed is True
    assert reasons[0].startswith("schema_mismatch: an older version wrote this index"), reasons


def test_the_list_shows_the_old_index_and_the_other_projects(old_index):
    from fw_context_mcp.mcp.handlers.maintenance import list_projects

    root, db_path = old_index

    rows = list_projects(project_root=str(root))

    row = next(row for row in rows if row.get("db") == str(db_path))
    assert row["error"].startswith("schema_mismatch: an older version wrote this index"), rows
    assert row["error"].endswith("To see the action, call get_project_status for that project."), "no command: no run state"


def test_the_status_names_the_repair_of_a_failed_run_first(old_index):
    """A run that a refusal stopped before `open_db` leaves the columns out; a plain run is refused again."""
    from fw_context_mcp.indexer.autobuild import record_problem
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status

    root, db_path = old_index
    record_problem(db_path.parent, "The build is not there. Run `fw-context index --build`.", build_missing=True)

    state = get_project_status(project_root=str(root))

    assert state["reindex_state"] == "failed", state
    assert state["reindex_reasons"][0] == "The build is not there. Run `fw-context index --build`.", state
    assert state["reindex_reasons"][1].startswith("schema_mismatch: an older version wrote this index")
    assert state["index_message"].startswith("The build is not there. Run `fw-context index --build`.")


def test_an_index_run_repairs_it(old_index):
    """`open_db` of an index run adds the columns, and the status reads the builds again."""
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status

    root, db_path = old_index
    open_db(db_path, skip_integrity_check=True).close()

    state = get_project_status(project_root=str(root))

    assert len(state["builds"]) == 1, "the two rows read as one build without names until the next run"
    assert not any(reason.startswith("schema_mismatch: an older version") for reason in state["reindex_reasons"])


def test_the_status_tells_to_wait_for_a_run(old_index):
    """A second run would take over the run that adds the columns."""
    import os

    from fw_context_mcp.mcp.background import PENDING_MARKER
    from fw_context_mcp.mcp.handlers.maintenance import get_project_status
    from fw_context_mcp.mcp.shared.pid_file import PidFile

    root, db_path = old_index
    PidFile(db_path.parent / PENDING_MARKER, pid=os.getppid()).write()

    state = get_project_status(project_root=str(root))

    assert state["reindex_state"] == "pending", state
    assert "in progress or scheduled; wait for it" in state["reindex_reasons"][0], state


def test_reindex_file_gets_the_old_schema(old_index):
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

    root, _ = old_index

    result = reindex_file_impl("src/main.c", str(root), with_analysis=False)

    assert list(result) == ["error"], result
    assert result["error"].startswith("schema_mismatch: an older version wrote this index")
