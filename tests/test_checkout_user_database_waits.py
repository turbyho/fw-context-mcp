"""After a checkout, the reason of a database of the user tells to wait during an index run.

The branch of a database of the user came before the branch of a run in
progress: during a run the reason said "make the database again ..., then
run `fw-context index`", and a caller that followed it started a second
run.  Each other reason of a database of the user (a missing file, a new
source file) told to wait.
"""

from __future__ import annotations

import pytest

from tests.test_index_state import _project, _schedule


@pytest.mark.parametrize("scheduled", [True, False])
def test_the_checkout_reason_of_a_database_of_the_user(tmp_path, monkeypatch, scheduled):
    from fw_context_mcp.indexer import git_context
    from fw_context_mcp.mcp.handlers.maintenance import get_build_info

    root, db_dir = _project(
        tmp_path, git_head="ref: refs/heads/one", build_system="cmake", index_table='compile_commands = "h-a.json"\n',
    )
    monkeypatch.setattr(git_context, "read_head", lambda project_root: "ref: refs/heads/two")
    if scheduled:
        _schedule(db_dir)

    reasons = get_build_info(project_root=str(root), variant="a")["reindex_reasons"]

    reason = next(r for r in reasons if r.startswith("checkout changed"))
    assert "make the database again" in reason, reason
    if scheduled:
        assert "wait for it rather than start a second one, and when this reason stays after the run" in reason
    else:
        assert "wait for it" not in reason, reason
