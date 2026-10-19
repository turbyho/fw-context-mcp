"""The schema version is a hash: each difference runs the migration, whatever the size of the hash.

Before, ``ensure_schema`` compared the hashes with ``<`` and ``>``.  About each
second change of the schema gave a smaller hash, thus an older index looked
newer: ``>`` only stamped it, and its migration did not run.  The status
asked for no index run then (``<``).
"""
from __future__ import annotations

import pytest

from fw_context_mcp.indexer.db import CURRENT_SCHEMA_VERSION, open_db

# Only the migration of a version difference makes this index again
# (`_run_data_migrations`): no step of each open does.
_GATED_INDEX = "idx_refs_unique"


def _index(tmp_path):
    db = tmp_path / "index.db"
    open_db(db, skip_integrity_check=True).close()
    return db


def _raw(db):
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _has_index(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone() is not None


# A hash of each side: the top bit flipped, the largest value, and a small one.
@pytest.mark.parametrize("stamp", [CURRENT_SCHEMA_VERSION ^ 0x40000000, 0x7FFFFFFF, 1])
def test_an_index_of_another_version_migrates_whatever_its_hash(tmp_path, stamp):
    """A larger hash of an older index was only stamped before."""
    db = _index(tmp_path)
    conn = _raw(db)
    conn.execute(f"DROP INDEX {_GATED_INDEX}")
    conn.execute(f"PRAGMA user_version = {stamp}")
    conn.commit()
    conn.close()

    migrated = open_db(db, skip_integrity_check=True)
    try:
        assert _has_index(migrated, _GATED_INDEX), "the migration ran"
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        migrated.close()


def test_a_migration_that_stopped_runs_again_on_the_next_open(tmp_path, monkeypatch):
    """The stamp comes last: a stop before it leaves the old stamp, thus the next open finishes the work."""
    from fw_context_mcp.indexer.db import _connection as connection_mod

    db = _index(tmp_path)
    conn = _raw(db)
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION ^ 0x40000000}")
    conn.commit()
    conn.close()

    calls: list[str] = []
    real = connection_mod._run_data_migrations

    def stops_once(conn):
        calls.append("migration")
        if len(calls) == 1:
            raise KeyboardInterrupt  # Ctrl-C during the FTS rebuild
        real(conn)

    monkeypatch.setattr(connection_mod, "_run_data_migrations", stops_once)
    with pytest.raises(KeyboardInterrupt):
        open_db(db, skip_integrity_check=True)
    finished = open_db(db, skip_integrity_check=True)
    try:
        assert finished.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        finished.close()
    assert calls == ["migration", "migration"]


@pytest.mark.parametrize("stamp", [CURRENT_SCHEMA_VERSION ^ 0x40000000, 1])
def test_the_status_names_each_difference_of_the_version(tmp_path, stamp):
    """Before, `<` named only a smaller hash."""
    from fw_context_mcp.mcp.shared.stale import quick_structural_reasons

    db = _index(tmp_path)
    conn = _raw(db)
    conn.execute(f"PRAGMA user_version = {stamp}")
    conn.commit()
    cfg = {"compile_commands_path": str(tmp_path / "missing.json"), "created_at": "2026-01-01 00:00:00",
           "row_format": "", "config_hash": "h"}
    try:
        reasons = [reason for reason in quick_structural_reasons(conn, cfg) if reason.startswith("schema")]
    finally:
        conn.close()
    assert reasons == [f"schema {stamp} != {CURRENT_SCHEMA_VERSION}"]
