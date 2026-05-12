"""Tests for fw_context_mcp.indexer.db."""

import sqlite3
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    delete_symbols_for_file,
    get_active_config,
    get_all_projects,
    get_file_mtimes,
    get_file_mtime_indexed,
    insert_symbols_batch,
    open_db,
    search_symbols,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)


class TestOpenDb:
    def test_creates_database(self, tmpdir):
        db_path = tmpdir / "test.db"
        conn = open_db(db_path)
        assert db_path.exists()
        conn.close()

    def test_creates_tables(self, tmpdir):
        db_path = tmpdir / "test.db"
        conn = open_db(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        assert "projects" in table_names
        assert "build_configs" in table_names
        assert "files" in table_names
        assert "symbols" in table_names
        assert "symbols_fts" in table_names
        conn.close()

    def test_idempotent(self, tmpdir):
        db_path = tmpdir / "test.db"
        conn1 = open_db(db_path)
        conn1.close()
        conn2 = open_db(db_path)
        conn2.close()

    def test_mtime_column_migration(self, tmpdir):
        """Schema migration: mtime column is added if missing."""
        db_path = tmpdir / "test.db"
        conn = open_db(db_path)
        # Verify mtime column exists
        cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]
        assert "mtime" in cols
        conn.close()


class TestTransaction:
    def test_commit(self, temp_db):
        with transaction(temp_db):
            upsert_project(temp_db, "p1", "test1", "/tmp/test1")
        row = temp_db.execute(
            "SELECT * FROM projects WHERE project_id=?", ("p1",)
        ).fetchone()
        assert row is not None
        assert row["name"] == "test1"

    def test_rollback(self, temp_db):
        with pytest.raises(ValueError):
            with transaction(temp_db):
                upsert_project(temp_db, "p2", "test2", "/tmp/test2")
                raise ValueError("simulated error")
        row = temp_db.execute(
            "SELECT * FROM projects WHERE project_id=?", ("p2",)
        ).fetchone()
        assert row is None


class TestUpsertProject:
    def test_insert_new(self, temp_db):
        with transaction(temp_db):
            upsert_project(temp_db, "proj-new", "New Project", "/tmp/new")
        row = temp_db.execute(
            "SELECT * FROM projects WHERE project_id=?", ("proj-new",)
        ).fetchone()
        assert row["name"] == "New Project"

    def test_replace_existing(self, temp_db):
        with transaction(temp_db):
            upsert_project(temp_db, "proj-001", "Updated Name", "/tmp/updated")
        row = temp_db.execute(
            "SELECT * FROM projects WHERE project_id=?", ("proj-001",)
        ).fetchone()
        assert row["name"] == "Updated Name"


class TestUpsertBuildConfig:
    def test_insert(self, populated_db):
        with transaction(populated_db):
            upsert_build_config(populated_db, "hash-abc", "proj-001", "/tmp/cc.json")
        row = populated_db.execute(
            "SELECT * FROM build_configs WHERE config_hash=?", ("hash-abc",)
        ).fetchone()
        assert row is not None

    def test_ignore_duplicate(self, populated_db):
        """INSERT OR IGNORE — second insert with same hash is no-op."""
        with transaction(populated_db):
            upsert_build_config(populated_db, "hash-000", "proj-001", "/tmp/cc.json")
            upsert_build_config(populated_db, "hash-000", "proj-001", "/tmp/cc.json")
        count = populated_db.execute(
            "SELECT COUNT(*) FROM build_configs WHERE config_hash=?", ("hash-000",)
        ).fetchone()[0]
        assert count == 1


class TestUpsertFile:
    def test_insert_new(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/src/main.cpp", "cpp")
        assert file_id > 0
        row = populated_db.execute(
            "SELECT * FROM files WHERE id=?", (file_id,)
        ).fetchone()
        assert row["path"] == "/tmp/src/main.cpp"
        assert row["language"] == "cpp"

    def test_update_existing(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/src/main.cpp", "c")
        # Update language to 'cpp' — same path+config_hash should reuse id
        file_id2 = upsert_file(populated_db, "hash-deadbeef", "/tmp/src/main.cpp", "cpp")
        assert file_id == file_id2
        row = populated_db.execute(
            "SELECT language FROM files WHERE id=?", (file_id2,)
        ).fetchone()
        assert row["language"] == "cpp"


class TestGetFileMtimes:
    def test_empty(self, populated_db):
        mtimes = get_file_mtimes(populated_db, "hash-deadbeef")
        assert mtimes == {}

    def test_with_files(self, populated_db):
        upsert_file(populated_db, "hash-deadbeef", "/tmp/a.cpp", "cpp", mtime=1.5)
        upsert_file(populated_db, "hash-deadbeef", "/tmp/b.cpp", "cpp", mtime=2.5)
        mtimes = get_file_mtimes(populated_db, "hash-deadbeef")
        assert len(mtimes) == 2
        assert mtimes["/tmp/a.cpp"][1] == 1.5
        assert mtimes["/tmp/b.cpp"][1] == 2.5


class TestGetFileMtimeIndexed:
    def test_existing_file(self, populated_db):
        upsert_file(populated_db, "hash-deadbeef", "/tmp/x.cpp", "cpp", mtime=3.0)
        mtime = get_file_mtime_indexed(populated_db, "hash-deadbeef", "/tmp/x.cpp")
        assert mtime == 3.0

    def test_nonexistent_file(self, populated_db):
        mtime = get_file_mtime_indexed(populated_db, "hash-deadbeef", "/tmp/no.cpp")
        assert mtime is None


class TestInsertSymbolsBatch:
    def test_insert(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        rows = [
            ("hash-deadbeef", file_id, "usr-1", "foo", "ns::foo", "function",
             10, 1, 0, "void foo()", ""),
            ("hash-deadbeef", file_id, "usr-2", "bar", "ns::bar", "function",
             20, 1, 0, "int bar(int)", "Returns bar"),
        ]
        count = insert_symbols_batch(populated_db, rows)
        assert count == 2

    def test_promotion_to_definition(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        # Insert as declaration
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "usr-1", "foo", "ns::foo", "function",
             5, 1, 0, "void foo()", ""),
        ])
        # Insert as definition (same USR) — should promote
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "usr-1", "foo", "ns::foo", "function",
             10, 1, 1, "void foo(int x)", "Does foo"),
        ])
        row = populated_db.execute(
            "SELECT is_definition, signature, line FROM symbols WHERE usr=?", ("usr-1",)
        ).fetchone()
        assert row["is_definition"] == 1
        assert row["signature"] == "void foo(int x)"
        assert row["line"] == 10  # definition line wins

    def test_definition_not_demoted(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        # Insert as definition first
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "usr-1", "foo", "ns::foo", "function",
             10, 1, 1, "void foo()", ""),
        ])
        # Then try to insert as declaration — WHERE clause prevents demotion
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "usr-1", "foo", "ns::foo", "function",
             5, 1, 0, "void foo();", ""),
        ])
        row = populated_db.execute(
            "SELECT is_definition FROM symbols WHERE usr=?", ("usr-1",)
        ).fetchone()
        assert row["is_definition"] == 1


class TestDeleteSymbolsForFile:
    def test_delete(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/del.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "usr-1", "f1", "ns::f1", "function", 1, 1, 1, "void f1()", ""),
            ("hash-deadbeef", file_id, "usr-2", "f2", "ns::f2", "function", 2, 1, 1, "void f2()", ""),
        ])
        count_before = populated_db.execute(
            "SELECT COUNT(*) FROM symbols WHERE file_id=?", (file_id,)
        ).fetchone()[0]
        assert count_before == 2

        delete_symbols_for_file(populated_db, file_id)
        count_after = populated_db.execute(
            "SELECT COUNT(*) FROM symbols WHERE file_id=?", (file_id,)
        ).fetchone()[0]
        assert count_after == 0


class TestGetActiveConfig:
    def test_returns_most_recent(self, populated_db):
        cfg = get_active_config(populated_db, "proj-001")
        assert cfg is not None
        assert cfg["config_hash"] == "hash-deadbeef"

    def test_none_for_unknown_project(self, populated_db):
        cfg = get_active_config(populated_db, "no-such-project")
        assert cfg is None


class TestGetAllProjects:
    def test_returns_projects(self, populated_db):
        rows = get_all_projects(populated_db)
        assert len(rows) >= 1
        project_ids = [r["project_id"] for r in rows]
        assert "proj-001" in project_ids

    def test_empty_db(self, temp_db):
        rows = get_all_projects(temp_db)
        assert rows == []


class TestSearchSymbols:
    def test_fts5_search(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/search.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "u1", "modem_init", "ns::modem_init",
             "function", 1, 1, 1, "void modem_init()", ""),
            ("hash-deadbeef", file_id, "u2", "uart_send", "ns::uart_send",
             "function", 5, 1, 1, "void uart_send(char c)", ""),
            ("hash-deadbeef", file_id, "u3", "modem_connect", "ns::modem_connect",
             "function", 10, 1, 1, "int modem_connect(const char* host)", ""),
        ])

        results = search_symbols(populated_db, "modem", "hash-deadbeef")
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"modem_init", "modem_connect"}

    def test_search_no_results(self, populated_db):
        results = search_symbols(populated_db, "nonexistent_symbol_xyz", "hash-deadbeef")
        assert results == []

    def test_search_limit(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/limit.cpp", "cpp")
        rows_data = [
            ("hash-deadbeef", file_id, f"u{i}", f"func{i}", f"ns::func{i}",
             "function", i, 1, 1, f"void func{i}()", "")
            for i in range(10)
        ]
        insert_symbols_batch(populated_db, rows_data)

        results = search_symbols(populated_db, "func*", "hash-deadbeef", limit=3)
        assert len(results) == 3


class TestForeignKeyConstraint:
    def test_upsert_file_requires_build_config(self, temp_db):
        """upsert_file should fail when config_hash doesn't exist in build_configs."""
        with pytest.raises(sqlite3.IntegrityError):
            upsert_file(temp_db, "nonexistent-hash", "/tmp/file.cpp", "cpp")

    def test_valid_fk_passes(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/valid.cpp", "cpp")
        assert file_id > 0
