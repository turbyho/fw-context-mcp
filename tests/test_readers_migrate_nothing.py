"""A command that only reads the index migrates nothing; the maintenance of the daemon waits for the lock.

Within one project only a writer that holds the index lock writes the
index.  Before, `fw-context search`, `db list`, `db stats`, `export` and
`watch status` opened the index with `open_db`, which runs the schema
migrations: after an upgrade of fw-context they migrated the index beside
an index run.  The daemon ran PRAGMA optimize (it can write) while a manual
run held the index.
"""
from __future__ import annotations

import argparse

import pytest

from tests.test_cli_build_selection import _hash_of
from tests.test_reindex_each_build import two_builds  # noqa: F401  (fixture)


def _no_open_db(monkeypatch) -> None:
    import fw_context_mcp.indexer.db as db_mod

    monkeypatch.setattr(db_mod, "open_db", lambda *a, **k: pytest.fail("a reader must not open the index for writes"))


def test_db_list_and_db_stats_read_only(two_builds, monkeypatch, capsys):  # noqa: F811
    from fw_context_mcp.cli._db import cmd_db_list, cmd_db_stats

    root, _db_path = two_builds
    hash_a = _hash_of(root, "a")  # the helper opens the index itself
    _no_open_db(monkeypatch)

    assert cmd_db_list(argparse.Namespace(project=str(root), verbose=False)) == 0
    assert cmd_db_stats(argparse.Namespace(project=str(root), config_hash=hash_a)) == 0


def test_an_older_index_gets_the_advice_and_no_migration(two_builds, monkeypatch, capsys):  # noqa: F811
    import fw_context_mcp.indexer.db as db_mod
    from fw_context_mcp.cli._db import cmd_db_list

    root, _db_path = two_builds
    _no_open_db(monkeypatch)
    monkeypatch.setattr(db_mod, "missing_columns", lambda conn: ["symbols.new_column"])

    assert cmd_db_list(argparse.Namespace(project=str(root), verbose=False)) == 1
    assert "Run 'fw-context index' to migrate it" in capsys.readouterr().err


def test_a_read_only_connection_refuses_a_write(two_builds):  # noqa: F811
    import sqlite3

    from fw_context_mcp.indexer.db import open_db_readonly

    _root, db_path = two_builds
    conn = open_db_readonly(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM build_configs").fetchone()[0] > 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM build_configs")
    finally:
        conn.close()


def test_the_daemon_skips_the_optimize_while_another_process_holds_the_index(two_builds, monkeypatch):  # noqa: F811
    from fw_context_mcp.indexer.db._locking import index_run_lock
    from fw_context_mcp.mcp.daemon import _optimize_db

    _root, db_path = two_builds
    _no_open_db(monkeypatch)

    with index_run_lock(db_path.parent):
        _optimize_db(db_path.parent)  # the lock is held: no open, no optimize


def test_an_empty_or_a_broken_file_is_named_and_not_a_traceback(tmp_path):
    """open_db made the tables of an empty file; a read-only open does not."""
    import sqlite3

    from fw_context_mcp.cli._builds import EMPTY, UNREADABLE, reading_problem
    from fw_context_mcp.indexer.db import open_db_readonly

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database" * 100)
    for path, kind in ((empty, EMPTY), (broken, UNREADABLE)):
        conn = open_db_readonly(path)
        try:
            assert reading_problem(conn)[0] == kind
        finally:
            conn.close()


def test_watch_status_goes_on_for_an_older_index(two_builds, monkeypatch, capsys):  # noqa: F811
    """Before, the status stopped with exit 1 before the lines that show the migrating run."""
    import fw_context_mcp.indexer.db as db_mod
    from fw_context_mcp.cli._watch import _print_modified_counts

    root, db_path = two_builds
    monkeypatch.setattr(db_mod, "missing_columns", lambda conn: ["symbols.new_column"])

    _print_modified_counts(db_path, "p", root)
    assert "Modified:   not known: an older fw-context wrote this index" in capsys.readouterr().out


def test_db_delete_all_deletes_a_broken_index(two_builds):  # noqa: F811
    """A broken index could not be listed, thus `--all` could not delete it."""
    from fw_context_mcp.cli._db import cmd_db_delete

    root, db_path = two_builds
    for suffix in ("-wal", "-shm"):
        (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)
    db_path.write_bytes(b"this is not a database" * 100)

    args = argparse.Namespace(project=str(root), all=True, config_hash=None, force=False, yes=True)
    assert cmd_db_delete(args) == 0
    assert not db_path.exists()


def test_db_delete_of_one_build_of_an_older_index_migrates_under_the_lock(two_builds, monkeypatch):  # noqa: F811
    """Like `--all` and `analyze`: the checks before the lock are skipped, open_db migrates under it."""
    import builtins

    import fw_context_mcp.cli._db as cli_db
    import fw_context_mcp.indexer.db as db_mod

    root, _db_path = two_builds
    hash_a = _hash_of(root, "a")
    monkeypatch.setattr(db_mod, "missing_columns", lambda conn: ["symbols.new_column"])
    monkeypatch.setattr(builtins, "input", lambda prompt: "y")
    steps: list[str] = []
    monkeypatch.setattr(cli_db, "_under_index_lock", lambda db_dir, args, work: steps.append("locked") or 0)

    args = argparse.Namespace(project=str(root), all=False, config_hash=hash_a, force=True, yes=False)
    assert cli_db.cmd_db_delete(args) == 0
    assert steps == ["locked"]


def test_the_daemon_is_named_as_an_fw_context_process(monkeypatch):
    from fw_context_mcp.indexer import _index_owner

    monkeypatch.setattr(_index_owner, "process_argv",
                        lambda pid: ["/usr/bin/python3", "-u", "-m", "fw_context_mcp.mcp.daemon", "/p"])
    assert _index_owner.is_fw_context_process(4242)
