"""The override graph follows the source after each index run.

A re-parse of a file keeps its ``overrides`` rows: they key on USRs, not on
symbol ids.  Before this fix, the post-process step then saw rows for the
config and did not compute again.  An index run with no --force thus kept
an override of a method that the source no longer has, and did not add an
override that the source added.  A real incremental run showed the two:
``Derived::f -> Base::f`` stayed after ``Derived::f`` was removed, and
``Derived::g -> Base::g`` did not come.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer._postprocess import _build_overrides, _step_build_overrides
from fw_context_mcp.indexer.db import (
    delete_symbols_for_file,
    insert_inheritance_batch,
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

CH = "hash-overrides"


def _struct(fid: int, name: str) -> tuple:
    return (
        CH, fid, "src/shapes.hpp", name, f"u_{name}", name, name, "struct",
        1, 1, 5, 1, "", "", None, 0, 0, "",
        0, "", 1, 0.0, "", 0,
    )


def _virtual_method(fid: int, path: str, parent: str, name: str) -> tuple:
    return (
        CH, fid, path, name, f"u_{parent}_{name}", name, f"{parent}::{name}", "method",
        10, 1, 12, 1, f"int {name}(int x)", "", None, 1, 0, f"u_{parent}",
        0, "", 1, 0.0, "", 0,
    )


@pytest.fixture
def db(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    with transaction(conn):
        upsert_project(conn, "proj-001", "test", str(tmp_path))
        upsert_build_config(conn, CH, "proj-001", str(tmp_path / "compile_commands.json"))
        header_id = upsert_file(conn, CH, "src/shapes.hpp", "cpp")
        base_id = upsert_file(conn, CH, "src/base.cpp", "cpp")
        derived_id = upsert_file(conn, CH, "src/derived.cpp", "cpp")
        insert_symbols_batch(conn, [
            _struct(header_id, "Base"),
            _struct(header_id, "Derived"),
            _virtual_method(base_id, "src/base.cpp", "Base", "f"),
            _virtual_method(base_id, "src/base.cpp", "Base", "g"),
            _virtual_method(derived_id, "src/derived.cpp", "Derived", "f"),
        ])
        insert_inheritance_batch(conn, [(CH, "u_Derived", "u_Base", "public", 0)])
    yield conn, derived_id
    conn.close()


def _ctx(tmp_path: Path) -> dict:
    # The context of a run with no --force, that is an incremental run.
    return {"config_hash": CH, "force": False, "db_dir": tmp_path}


def _overrides(conn) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT derived_usr, base_usr FROM overrides WHERE config_hash = ?", (CH,)
    ).fetchall()
    return {(row[0], row[1]) for row in rows}


def test_an_incremental_run_follows_a_changed_override(db, tmp_path: Path):
    """Derived stops overriding f and starts overriding g."""
    conn, derived_id = db
    _step_build_overrides(conn, _ctx(tmp_path))
    assert _overrides(conn) == {("u_Derived_f", "u_Base_f")}

    # The parse of derived.cpp again, as an incremental run does it.
    with transaction(conn):
        delete_symbols_for_file(conn, derived_id)
        insert_symbols_batch(conn, [_virtual_method(derived_id, "src/derived.cpp", "Derived", "g")])

    _step_build_overrides(conn, _ctx(tmp_path))

    assert _overrides(conn) == {("u_Derived_g", "u_Base_g")}


def test_no_virtual_method_clears_the_overrides(db, tmp_path: Path):
    """With no virtual method left, no override of an earlier run stays."""
    conn, _derived_id = db
    _build_overrides(conn, CH, tmp_path)
    assert _overrides(conn) == {("u_Derived_f", "u_Base_f")}
    with transaction(conn):
        conn.execute("UPDATE symbols SET is_virtual = 0 WHERE config_hash = ?", (CH,))

    _build_overrides(conn, CH, tmp_path)

    assert _overrides(conn) == set()
