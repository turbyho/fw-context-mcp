"""During an index run, ``index_message`` of an old schema or row format tells the caller to wait.

The reasons in ``reindex_reasons`` did, but ``index_message`` ended with a
bare "Run fw-context index." after the advice to wait in its front.  A
caller that acted on the end of the message started a second run of the
same work.
"""

from __future__ import annotations

import pytest

from tests.test_index_state import _project, _schedule


def _old(db_dir, what: str) -> None:
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_dir / "index.db")
    try:
        if what == "row format":
            conn.execute("UPDATE build_configs SET row_format = 'fw-context-rows/1' WHERE variant = 'a'")
            conn.commit()
        else:
            conn.execute("PRAGMA user_version = 1")
    finally:
        conn.close()


@pytest.mark.parametrize("what", ["row format", "schema"])
def test_the_message_tells_to_wait_during_a_run(tmp_path, what):
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info

    root, db_dir = _project(tmp_path)
    _old(db_dir, what)
    _schedule(db_dir)

    message = get_build_info(project_root=str(root), variant="a")["index_message"]

    assert "Run fw-context index." not in message and "— run fw-context index" not in message, message
    assert "wait for it, and when this reason stays after the run, run `fw-context index`" in message, message


@pytest.mark.parametrize("what", ["row format", "schema"])
def test_the_message_names_the_command_without_a_run(tmp_path, what):
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info

    root, db_dir = _project(tmp_path)
    _old(db_dir, what)

    message = get_build_info(project_root=str(root), variant="a")["index_message"]

    assert "Run `fw-context index`." in message, message
    assert "wait for it" not in message, message


def test_an_old_schema_and_a_changed_database_tell_to_wait(tmp_path):
    """The branch of the two together had the same bare command."""
    import os
    import time

    from fw_context_mcp.mcp.handlers.maintenance import get_build_info

    root, db_dir = _project(tmp_path)
    _old(db_dir, "schema")
    future = time.time() + 100
    os.utime(root / "h-a.json", (future, future))
    _schedule(db_dir)

    message = get_build_info(project_root=str(root), variant="a")["index_message"]

    assert "and compile_commands.json changed. An index run is in progress or scheduled; wait for it" in message, message
