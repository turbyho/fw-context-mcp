"""Where the `compile_commands.<hash>.json` debug artifact goes, and how it goes away.

`compute_config_hash` writes the artifact, and three places remove it: the
retention of old builds, the orphan cleanup at the start of each run, and
`fw-context db delete`.  The last two look in the database directory of the
project.  The writer and the retention added the project id to that
directory one more time.  Measured on this machine: 39 artifacts in
`~/.fw-context/index/<id>/<id>/`, which no cleanup ever saw.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer._embedding import _cleanup_orphaned_cc_artifacts
from fw_context_mcp.indexer._postprocess import cleanup_old_builds_multi
from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project
from fw_context_mcp.indexer.manifest import compute_config_hash

OLD = "a" * 64
NEW = "b" * 64


def test_the_artifact_is_next_to_the_manifest(tmp_path):
    # db_dir is the database directory of the project, where the manifest
    # of the same hash goes (`manifest._manifest_path`).
    config_hash = compute_config_hash([], tmp_path, "pid", [], db_dir=tmp_path)
    assert (tmp_path / f"compile_commands.{config_hash}.json").is_file()
    assert not (tmp_path / "pid").exists()


def test_retention_removes_the_artifact_of_an_old_build(tmp_path):
    conn = open_db(tmp_path / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, OLD, "pid", "a.json", variant="v", image="")
            upsert_build_config(conn, NEW, "pid", "b.json", variant="v", image="")
        for config_hash in (OLD, NEW):
            (tmp_path / f"compile_commands.{config_hash}.json").write_text("{}", encoding="utf-8")
        assert cleanup_old_builds_multi(conn, "pid", tmp_path, [("v", "")]) == 1
    finally:
        conn.close()
    assert not (tmp_path / f"compile_commands.{OLD}.json").exists()
    assert (tmp_path / f"compile_commands.{NEW}.json").exists()


def test_the_artifacts_in_the_old_nested_directory_are_removed(tmp_path):
    # Every file there is a stale artifact: the writer now writes next to
    # the manifest, and nothing reads the nested directory.
    conn = open_db(tmp_path / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, NEW, "pid", "b.json")
    finally:
        conn.close()
    nested = tmp_path / "pid"
    nested.mkdir()
    for config_hash in (OLD, NEW):
        (nested / f"compile_commands.{config_hash}.json").write_text("{}", encoding="utf-8")
    assert _cleanup_orphaned_cc_artifacts(tmp_path / "index.db", "pid") == 2
    assert not nested.exists()


def test_a_nested_directory_with_another_file_stays(tmp_path):
    nested = tmp_path / "pid"
    nested.mkdir()
    (nested / f"compile_commands.{OLD}.json").write_text("{}", encoding="utf-8")
    (nested / "notes.txt").write_text("", encoding="utf-8")
    assert _cleanup_orphaned_cc_artifacts(tmp_path / "index.db", "pid") == 1
    assert sorted(path.name for path in nested.iterdir()) == ["notes.txt"]


def test_the_cleanup_reads_the_directory_that_db_delete_reads(tmp_path: Path):
    # `fw-context db delete` removes `<index dir>/<id>/compile_commands.<hash>.json`,
    # which is the database directory of the project.
    (tmp_path / f"compile_commands.{OLD}.json").write_text("{}", encoding="utf-8")
    assert _cleanup_orphaned_cc_artifacts(tmp_path / "index.db", "pid") == 1
