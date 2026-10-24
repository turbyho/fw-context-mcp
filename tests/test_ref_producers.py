"""The edges of template instances keep each unit that found them (``ref_producers``).

An edge from the body of an instantiation lies in the file of the template,
which one unit owns, but each unit that uses the instance finds it.  A new
parse of the owner, or of one producer, must not remove an edge that
another, unchanged unit still finds: an incremental run must give the
graph of a full run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    delete_refs_for_files,
    insert_refs_batch,
    link_ref_producers,
    open_db,
    release_ref_producer,
    transaction,
    upsert_build_config,
    upsert_project,
)
from fw_context_mcp.indexer.models import RefOrigin

CH = "hash-producers"
UNIT_A = ("a.cpp", "flags-a")
UNIT_B = ("b.cpp", "flags-b")
INST = int(RefOrigin.INSTANTIATION)
DIRECT = int(RefOrigin.DIRECT)


def _row(origins: int, to_usr: str = "u_sample", line: int = 6, col: int = 12) -> tuple:
    return (CH, to_usr, "tmpl.h", line, "u_read_twice", "call", None, col, origins)


@pytest.fixture
def conn(tmp_path: Path):
    db = open_db(tmp_path / "index.db")
    with transaction(db):
        upsert_project(db, "pid", "p", str(tmp_path))
        upsert_build_config(db, CH, "pid", "cc.json")
    yield db
    db.close()


def _produce(conn, unit, *rows) -> None:
    insert_refs_batch(conn, list(rows))
    link_ref_producers(conn, list(rows), unit)


def _state(conn) -> list[tuple]:
    return [
        tuple(r) for r in conn.execute(
            "SELECT r.to_usr, r.from_col, r.origins, "
            "(SELECT COUNT(*) FROM ref_producers p WHERE p.ref_id = r.id) "
            "FROM refs r WHERE r.config_hash = ? ORDER BY r.to_usr, r.from_col",
            (CH,),
        )
    ]


def test_two_units_give_one_row_with_two_producers(conn):
    _produce(conn, UNIT_A, _row(INST))
    _produce(conn, UNIT_B, _row(INST))
    assert _state(conn) == [("u_sample", 12, INST, 2)]


def test_a_row_stays_while_one_producer_is_left(conn):
    _produce(conn, UNIT_A, _row(INST))
    _produce(conn, UNIT_B, _row(INST))
    release_ref_producer(conn, CH, UNIT_A)
    assert _state(conn) == [("u_sample", 12, INST, 1)]
    release_ref_producer(conn, CH, UNIT_B)
    assert _state(conn) == []


def test_a_released_unit_keeps_the_other_origins_of_a_row(conn):
    """A call that the walker of the template also found stays a direct edge."""
    _produce(conn, UNIT_A, _row(INST | DIRECT))
    release_ref_producer(conn, CH, UNIT_A)
    assert _state(conn) == [("u_sample", 12, DIRECT, 0)]


def test_a_new_parse_of_the_owner_keeps_a_produced_edge(conn):
    """The owner of tmpl.h is parsed again: a row that unit B still produces stays."""
    _produce(conn, UNIT_B, _row(INST | DIRECT))
    insert_refs_batch(conn, [_row(DIRECT, to_usr="u_other", col=3)])
    delete_refs_for_files(conn, CH, ["tmpl.h"], keep_instance_edges=True)
    assert _state(conn) == [("u_sample", 12, INST, 1)]


def test_a_file_that_left_the_build_loses_each_row(conn):
    _produce(conn, UNIT_B, _row(INST))
    delete_refs_for_files(conn, CH, ["tmpl.h"])
    assert _state(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM ref_producers").fetchone()[0] == 0


def test_a_row_without_the_bit_gets_no_producer(conn):
    _produce(conn, UNIT_A, _row(DIRECT))
    assert _state(conn) == [("u_sample", 12, DIRECT, 0)]


def test_release_of_a_unit_without_edges_changes_nothing(conn):
    _produce(conn, UNIT_A, _row(INST))
    release_ref_producer(conn, CH, UNIT_B)
    assert _state(conn) == [("u_sample", 12, INST, 1)]


_HEADER = """\
#pragma once
struct Sensor { int sample(); };
template <class T> int read_twice(T& device) { return device.sample(); }
"""
_USER = '#include "tmpl.h"\nint {name}(Sensor& s) {{ return read_twice(s); }}\n'
_NOT_A_USER = '#include "tmpl.h"\nint {name}(Sensor& s) {{ return s.sample(); }}\n'


def _project(tmp_path: Path, a: str, b: str) -> Path:
    import json

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "tmpl.h").write_text(_HEADER, encoding="utf-8")
    (project / "a.cpp").write_text(a.format(name="use_a"), encoding="utf-8")
    (project / "b.cpp").write_text(b.format(name="use_b"), encoding="utf-8")
    entries = [
        {"directory": str(project), "file": name,
         "arguments": ["c++", "-std=c++17", "-c", name, "-o", f"{name}.o"]}
        for name in ("a.cpp", "b.cpp")
    ]
    (project / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    return project


def _run(tmp_path: Path, project: Path) -> tuple[Path, str]:
    from fw_context_mcp.indexer.runner import run

    db_path = tmp_path / "db" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    config_hash = run(
        compile_commands=project / "compile_commands.json", db_path=db_path, project_root=project,
        project_id="prod", project_name="prod", index_refs=True, record_git=False,
    )
    return db_path, config_hash


def _instance_edge(db_path: Path, config_hash: str) -> list[tuple]:
    db = open_db(db_path)
    try:
        return [
            tuple(r) for r in db.execute(
                """SELECT r.from_file, r.from_line, r.origins,
                          (SELECT group_concat(p.unit_path, ',') FROM
                              (SELECT unit_path FROM ref_producers WHERE ref_id = r.id ORDER BY unit_path) p)
                   FROM refs r JOIN symbols s ON s.config_hash = r.config_hash AND s.usr = r.to_usr
                   WHERE r.config_hash = ? AND s.qualified_name = 'Sensor::sample' AND r.from_file = 'tmpl.h'""",
                (config_hash,),
            )
        ]
    finally:
        db.close()


@pytest.mark.libclang
class TestAnIncrementalRunKeepsTheEdgesOfInstances:
    def test_both_units_produce_the_edge(self, tmp_path):
        db_path, ch = _run(tmp_path, _project(tmp_path, _USER, _USER))
        assert _instance_edge(db_path, ch) == [("tmpl.h", 3, INST, "a.cpp,b.cpp")]

    def test_a_new_parse_of_the_owner_keeps_the_edge_of_the_other_unit(self, tmp_path):
        """a.cpp owns tmpl.h (the first unit); it stops using the template."""
        project = _project(tmp_path, _USER, _USER)
        _run(tmp_path, project)
        (project / "a.cpp").write_text(_NOT_A_USER.format(name="use_a"), encoding="utf-8")
        db_path, ch = _run(tmp_path, project)
        assert _instance_edge(db_path, ch) == [("tmpl.h", 3, INST, "b.cpp")]

    def test_the_edge_goes_with_its_last_producer(self, tmp_path):
        project = _project(tmp_path, _NOT_A_USER, _USER)
        _run(tmp_path, project)
        (project / "b.cpp").write_text(_NOT_A_USER.format(name="use_b"), encoding="utf-8")
        db_path, ch = _run(tmp_path, project)
        assert _instance_edge(db_path, ch) == []

    def test_the_incremental_run_equals_a_full_run(self, tmp_path):
        project = _project(tmp_path, _USER, _USER)
        _run(tmp_path, project)
        (project / "a.cpp").write_text(_NOT_A_USER.format(name="use_a"), encoding="utf-8")
        incremental = _instance_edge(*_run(tmp_path, project))
        full_dir = tmp_path / "full"
        full_dir.mkdir()
        full = _instance_edge(*_run(full_dir, _project(full_dir, _NOT_A_USER, _USER)))
        assert incremental == full
