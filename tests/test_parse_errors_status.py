"""The status tools report the parse errors of the units of each build.

Decision of the user (2026-10-24): always the counts, and a warning only
for a fatal error.  After a fatal error clang stops, thus symbols and calls
of that unit can be missing, and a reindex gives the same result.  Before,
nothing told the user, and a degraded index looked healthy.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_index_state import _project

CLEAN = {"units": 0, "errors": 0, "fatal_units": 0, "first": []}


def _set_errors(db_dir: Path, path: str, errors: int, fatal: bool, first: str, config_hash: str = "h-a") -> None:
    """Write the parse values of the unit *path* of the build *config_hash*, as an index run does."""
    from fw_context_mcp.indexer.db import open_db, upsert_file

    conn = open_db(db_dir / "index.db")
    try:
        file_id = upsert_file(conn, config_hash, path, "c", mtime=0.0)
        conn.execute(
            "UPDATE files SET parse_errors = ?, parse_fatal = ?, parse_first_error = ? WHERE id = ?",
            (errors, int(fatal), first, file_id),
        )
        conn.commit()
    finally:
        conn.close()


def _build_info(root: Path, variant: str = "a") -> dict:
    from fw_context_mcp.mcp.handlers import maintenance

    return maintenance.get_build_info(project_root=str(root), variant=variant)


def test_a_clean_build_reports_zero_and_no_warning(tmp_path):
    root, _ = _project(tmp_path)

    info = _build_info(root)

    assert info["parse_errors"] == CLEAN
    assert "Parse:" not in info["index_message"], info["index_message"]


def test_a_non_fatal_error_is_counted_without_a_warning(tmp_path):
    """A non-fatal error leaves the parse complete: only the counts show it."""
    root, db_dir = _project(tmp_path)
    _set_errors(db_dir, "a.c", 2, False, "unknown attribute '__malloc__' ignored (a.c:3)")

    info = _build_info(root)

    assert info["parse_errors"] == {
        "units": 1, "errors": 2, "fatal_units": 0,
        "first": [{"file": "a.c", "error": "unknown attribute '__malloc__' ignored (a.c:3)", "fatal": False}],
    }
    assert "Parse:" not in info["index_message"], info["index_message"]
    assert info["reindex_state"] is None


def test_a_fatal_error_gives_a_warning_and_no_reindex_reason(tmp_path):
    """A reindex parses with the same flags and headers, thus it is not the repair."""
    root, db_dir = _project(tmp_path)
    _set_errors(db_dir, "a.c", 1, True, "'missing.h' file not found (a.c:1)")

    info = _build_info(root)

    assert info["parse_errors"]["fatal_units"] == 1
    assert (
        "Parse: 1 unit(s) stopped at a fatal error, thus their symbols and calls can be missing "
        "(first: a.c: 'missing.h' file not found (a.c:1)). A reindex gives the same result"
    ) in info["index_message"], info["index_message"]
    assert info["reindex_needed"] is False
    assert info["reindex_reasons"] == []


def test_the_fatal_units_come_first_and_the_list_is_short(tmp_path):
    root, db_dir = _project(tmp_path)
    for name in ("b1.c", "b2.c", "b3.c"):
        _set_errors(db_dir, name, 5, False, f"error in {name}")
    _set_errors(db_dir, "z.c", 1, True, "fatal in z.c")

    info = _build_info(root)

    assert info["parse_errors"]["units"] == 4
    assert info["parse_errors"]["errors"] == 16
    assert [entry["file"] for entry in info["parse_errors"]["first"]] == ["z.c", "b1.c", "b2.c"]


def test_a_project_of_several_builds_names_the_build_of_the_fatal_error(tmp_path):
    from fw_context_mcp.mcp.handlers import maintenance

    root, db_dir = _project(tmp_path)
    _set_errors(db_dir, "b.c", 1, True, "fatal in b.c", config_hash="h-b")

    state = maintenance.get_project_status(project_root=str(root))

    assert (
        "Units stopped at a fatal parse error in 1 of 2 builds (b: 1)" in state["index_message"]
    ), state["index_message"]
    rows = {row["variant"]: row["parse_errors"] for row in state["builds"]}
    assert rows["a"] == CLEAN
    assert rows["b"]["fatal_units"] == 1


def test_an_index_of_an_older_row_format_reports_no_counts(tmp_path):
    """Its run did not record the errors, and the 0 of the migration is not "no error"."""
    from fw_context_mcp.indexer.db import open_db

    root, db_dir = _project(tmp_path)
    conn = open_db(db_dir / "index.db")
    try:
        conn.execute("UPDATE build_configs SET row_format = 'fw-context-rows/5' WHERE variant = 'a'")
        conn.commit()
    finally:
        conn.close()

    assert _build_info(root)["parse_errors"] is None
