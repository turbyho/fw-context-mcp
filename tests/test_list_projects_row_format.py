"""``list_projects`` must read the row format, as ``get_active_build`` does.

``build_configs.row_format`` records which meaning the stored text carries.
``get_active_build`` and the daemon ask for a reindex when that format is
older than the format of this version.  ``list_projects`` read only the
schema version and the mtime of compile_commands.json, thus it reported
"ready" for the same index.  Measured on a real project after a bump to
``fw-context-rows/5``: ``get_active_build`` gave ``reindex_needed`` and
``list_projects`` gave ``ready``, from one server process.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import fw_context_mcp  # noqa: F401  — must precede sqlite3
from fw_context_mcp.config.settings import generate_project_id
from fw_context_mcp.indexer.db import (
    CURRENT_ROW_FORMAT,
    open_db,
    transaction,
    upsert_build_config,
    upsert_project,
)
from fw_context_mcp.mcp.handlers.maintenance import list_projects


def _project(tmp_path: Path, *, row_format: str | None) -> tuple[Path, str]:
    """Give the root and the project_id of a project with one index.

    With *row_format* None the project has no build, as after an index run
    that stopped before its first build config.
    """
    project_id = generate_project_id()
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        "[project]\n"
        f'id = "{project_id}"\n\n'
        "[index]\n"
        f'db_dir = "{index_dir.as_posix()}"\n'
        'compile_commands = "compile_commands.json"\n',
        encoding="utf-8",
    )
    (root / ".fw-context" / "local.toml").write_text("", encoding="utf-8")

    # Older than the build config, thus compile_commands.json reads as
    # unchanged and only the row format can make the index stale.
    cc_path = root / "compile_commands.json"
    cc_path.write_text("[]", encoding="utf-8")
    old = time.time() - 3600
    os.utime(cc_path, (old, old))

    conn = open_db(index_dir / project_id / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, project_id, "proj", str(root))
            if row_format is not None:
                upsert_build_config(
                    conn, f"hash-{project_id}", project_id, str(cc_path),
                    row_format=row_format,
                )
    finally:
        conn.close()
    return root, project_id


def _entry(root: Path, project_id: str) -> dict:
    entries = [e for e in list_projects(project_root=str(root)) if e.get("project_id") == project_id]
    assert len(entries) == 1, entries
    return entries[0]


def test_an_older_row_format_needs_a_reindex(tmp_path: Path) -> None:
    root, project_id = _project(tmp_path, row_format="fw-context-rows/0")

    entry = _entry(root, project_id)

    assert entry["reindex_needed"] is True, (
        "get_active_build asks for a reindex over this index, thus "
        "list_projects must not report it as current"
    )
    assert entry["status"] == "reindex_needed"


def test_an_absent_row_format_needs_a_reindex(tmp_path: Path) -> None:
    """An index of a version before the stamp carries text of no known meaning."""
    root, project_id = _project(tmp_path, row_format="")

    entry = _entry(root, project_id)

    assert entry["reindex_needed"] is True
    assert entry["status"] == "reindex_needed"


def test_the_current_row_format_is_ready(tmp_path: Path) -> None:
    root, project_id = _project(tmp_path, row_format=CURRENT_ROW_FORMAT)

    entry = _entry(root, project_id)

    assert entry["reindex_needed"] is False
    assert entry["status"] == "ready"


def test_a_newer_row_format_is_ready(tmp_path: Path) -> None:
    """A reindex cannot repair a newer format: this process is the old reader.

    ``get_active_build`` asks for a restart of the LLM client there, and not
    for a reindex.  A ``reindex_needed`` here would advise the one command
    that writes the same newer format again.
    """
    root, project_id = _project(tmp_path, row_format="fw-context-rows/999")

    entry = _entry(root, project_id)

    assert entry["reindex_needed"] is False
    assert entry["status"] == "ready"


def test_an_older_format_of_another_build_needs_a_reindex(tmp_path: Path) -> None:
    """The builds of one project are not always indexed together.

    The newest build is current here, and the build of another image holds
    rows of an older format.  ``get_all_projects`` gives the newest build
    alone, thus a check of that row only reported "ready".
    """
    root, project_id = _project(tmp_path, row_format=CURRENT_ROW_FORMAT)
    db_path = tmp_path / "index" / project_id / "index.db"
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_build_config(
                conn, f"hash-boot-{project_id}", project_id,
                str(root / "compile_commands.json"), image="boot",
                row_format="fw-context-rows/0",
            )
            # The current build is the newest one.
            conn.execute(
                "UPDATE build_configs SET created_at='2000-01-01 00:00:00' "
                "WHERE config_hash=?",
                (f"hash-boot-{project_id}",),
            )
    finally:
        conn.close()

    entry = _entry(root, project_id)

    assert entry["indexed_at"] != "2000-01-01 00:00:00", "the fixture must keep the current build newest"
    assert entry["reindex_needed"] is True
    assert entry["status"] == "reindex_needed"


def test_a_project_without_a_build_is_not_stale_by_format(tmp_path: Path) -> None:
    """No build gives no row format, and that is not an OLD row format."""
    root, project_id = _project(tmp_path, row_format=None)

    entry = _entry(root, project_id)

    assert entry["reindex_needed"] is False
    assert entry["status"] == "ready"
