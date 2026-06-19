"""Tests for fw_context_mcp.indexer.db."""

import sqlite3

import pytest

from fw_context_mcp.indexer.db import (
    DatabaseCorruptionError,
    _expand_query,
    count_refs,
    delete_refs_for_file,
    delete_symbols_for_file,
    find_refs,
    get_active_config,
    get_all_projects,
    get_file_mtime_indexed,
    get_file_mtimes,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    search_symbols,
    split_tokens,
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

    def test_file_path_column_migration(self, tmpdir):
        """Schema migration: file_path column and FTS5 are upgraded."""
        db_path = tmpdir / "test.db"
        conn = open_db(db_path)
        sym_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
        assert "file_path" in sym_cols
        fts_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols_fts)").fetchall()]
        assert "file_path" in fts_cols
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
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             10, 1, 0, 0, "void foo()", "", None),
            ("hash-deadbeef", file_id, "src/test.cpp", "bar", "usr-2", "bar", "ns::bar", "function",
             20, 1, 0, 0, "int bar(int)", "Returns bar", None),
        ]
        count = insert_symbols_batch(populated_db, rows)
        assert count == 2

    def test_promotion_to_definition(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        # Insert as declaration
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             5, 1, 0, 0, "void foo()", "", None),
        ])
        # Insert as definition (same USR) — should promote
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             10, 1, 0, 1, "void foo(int x)", "Does foo", None),
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
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             10, 1, 0, 1, "void foo()", "", None),
        ])
        # Then try to insert as declaration — WHERE clause prevents demotion
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             5, 1, 0, 0, "void foo();", "", None),
        ])
        row = populated_db.execute(
            "SELECT is_definition FROM symbols WHERE usr=?", ("usr-1",)
        ).fetchone()
        assert row["is_definition"] == 1


class TestDeleteSymbolsForFile:
    def test_delete(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/del.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/del.cpp", "f1", "usr-1", "f1", "ns::f1", "function", 1, 1, 0, 1, "void f1()", "", None),
            ("hash-deadbeef", file_id, "src/del.cpp", "f2", "usr-2", "f2", "ns::f2", "function", 2, 1, 0, 1, "void f2()", "", None),
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
            ("hash-deadbeef", file_id, "src/modem/modem_driver.cpp",
             split_tokens("modem_init", "ns::modem_init"),
             "u1", "modem_init", "ns::modem_init", "function", 1, 1, 0, 1, "void modem_init()", "", None),
            ("hash-deadbeef", file_id, "src/uart/uart_driver.cpp",
             split_tokens("uart_send", "ns::uart_send"),
             "u2", "uart_send", "ns::uart_send", "function", 5, 1, 0, 1, "void uart_send(char c)", "", None),
            ("hash-deadbeef", file_id, "src/modem/modem_driver.cpp",
             split_tokens("modem_connect", "ns::modem_connect"),
             "u3", "modem_connect", "ns::modem_connect", "function", 10, 1, 0, 1, "int modem_connect(const char* host)", "", None),
        ])

        results = search_symbols(populated_db, "modem", "hash-deadbeef")
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"modem_init", "modem_connect"}

    def test_fts5_search_by_path(self, populated_db):
        """Symbols can be found via file path tokens in FTS5."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/spi_driver.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/spi/spi_driver.cpp",
             split_tokens("write", "SPI::write"),
             "u10", "write", "SPI::write", "method", 1, 1, 0, 1, "void write(const uint8_t* buf, int len)", "", None),
            ("hash-deadbeef", file_id, "src/uart/uart_driver.cpp",
             split_tokens("write", "UART::write"),
             "u11", "write", "UART::write", "method", 1, 1, 0, 1, "void write(char c)", "", None),
        ])
        results = search_symbols(populated_db, "spi* write*", "hash-deadbeef")
        assert len(results) == 1
        assert results[0]["name"] == "write"
        assert "spi" in results[0]["file_path"].lower()

    def test_fts5_search_by_name_tokens(self, populated_db):
        """camelCase names are searchable via split tokens — connect* finds onConnectionComplete."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/ble.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "lib/ble/ble.cpp",
             split_tokens("onConnectionComplete", "ZBLE::onConnectionComplete"),
             "u20", "onConnectionComplete", "ZBLE::onConnectionComplete",
             "method", 1, 1, 0, 1, "void onConnectionComplete()", "", None),
            ("hash-deadbeef", file_id, "lib/ble/ble.cpp",
             split_tokens("startAdvertising", "ZBLE::startAdvertising"),
             "u21", "startAdvertising", "ZBLE::startAdvertising",
             "method", 2, 1, 0, 1, "void startAdvertising()", "", None),
        ])
        # connect* must find onConnectionComplete via name_tokens = "on connection complete"
        results = search_symbols(populated_db, "connect*", "hash-deadbeef")
        assert len(results) == 1
        assert results[0]["name"] == "onConnectionComplete"
        assert results[0]["name_tokens"] == "on connection complete zble"

    def test_search_no_results(self, populated_db):
        results = search_symbols(populated_db, "nonexistent_symbol_xyz", "hash-deadbeef")
        assert results == []

    def test_search_limit(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/limit.cpp", "cpp")
        rows_data = [
            ("hash-deadbeef", file_id, "src/limit.cpp",
             split_tokens(f"func{i}", f"ns::func{i}"),
             f"u{i}", f"func{i}", f"ns::func{i}", "function", i, 1, 0, 1, f"void func{i}()", "", None)
            for i in range(10)
        ]
        insert_symbols_batch(populated_db, rows_data)

        results = search_symbols(populated_db, "func*", "hash-deadbeef", limit=3)
        assert len(results) == 3


class TestExpandQuery:
    """Tests for _expand_query — wildcard expansion with FTS5 syntax awareness."""

    def test_bare_word_gets_wildcard(self):
        assert _expand_query("connect") == "connect*"

    def test_multiple_words_all_get_wildcards(self):
        assert _expand_query("modem init") == "modem* init*"

    def test_existing_wildcard_preserved(self):
        assert _expand_query("connect*") == "connect*"

    def test_cpp_scope_gets_expansion(self):
        """C++ :: should not block wildcard expansion."""
        assert _expand_query("std::vector") == "std* vector*"

    def test_cpp_scope_with_multiple_tokens(self):
        """C++ :: in a multi-word query should expand all tokens."""
        assert _expand_query("mbed::DigitalOut write") == "mbed* DigitalOut* write*"

    def test_column_filter_bypassed(self):
        """Column-filter syntax with single colon must not get expanded."""
        assert _expand_query("name_tokens : connect") == "name_tokens : connect"

    def test_quoted_string_bypassed(self):
        assert _expand_query('"hello world"') == '"hello world"'

    def test_fts5_operators_bypassed(self):
        assert _expand_query("NEAR(a, b)") == "NEAR(a, b)"

    def test_or_operator_bypassed(self):
        assert _expand_query("connect OR write") == "connect OR write"

    def test_parentheses_bypassed(self):
        assert _expand_query("(connect write)") == "(connect write)"


class TestForeignKeyConstraint:
    def test_upsert_file_requires_build_config(self, temp_db):
        """upsert_file should fail when config_hash doesn't exist in build_configs."""
        with pytest.raises(sqlite3.IntegrityError):
            upsert_file(temp_db, "nonexistent-hash", "/tmp/file.cpp", "cpp")

    def test_valid_fk_passes(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/valid.cpp", "cpp")
        assert file_id > 0


class TestRefs:
    def _setup_symbols(self, db):
        """Two symbols: callee modem_init (usr=U_callee), caller app_run (usr=U_caller)."""
        fid = upsert_file(db, "hash-deadbeef", "/tmp/app.cpp", "cpp")
        insert_symbols_batch(db, [
            ("hash-deadbeef", fid, "src/modem.cpp", split_tokens("modem_init", "modem_init"),
             "U_callee", "modem_init", "modem_init", "function", 10, 1, 0, 1, "void modem_init()", "", None),
            ("hash-deadbeef", fid, "src/app.cpp", split_tokens("app_run", "App::app_run"),
             "U_caller", "app_run", "App::app_run", "method", 50, 1, 0, 1, "void app_run()", "", None),
        ])

    def test_count_refs_empty(self, populated_db):
        assert count_refs(populated_db, "hash-deadbeef") == 0

    def test_insert_and_find_call(self, populated_db):
        self._setup_symbols(populated_db)
        # app_run (U_caller) calls modem_init (U_callee) at app.cpp:55
        insert_refs_batch(populated_db, [
            ("hash-deadbeef", "U_callee", "src/app.cpp", 55, "U_caller", "call"),
        ])
        assert count_refs(populated_db, "hash-deadbeef") == 1

        rows = find_refs(populated_db, "hash-deadbeef", "modem_init", ref_kind="call")
        assert len(rows) == 1
        r = rows[0]
        assert r["from_file"] == "src/app.cpp"
        assert r["from_line"] == 55
        assert r["ref_kind"] == "call"
        # caller resolved via from_usr → symbols.usr
        assert r["caller_name"] == "app_run"
        assert r["caller_qname"] == "App::app_run"

    def test_find_refs_kind_filter(self, populated_db):
        self._setup_symbols(populated_db)
        insert_refs_batch(populated_db, [
            ("hash-deadbeef", "U_callee", "src/app.cpp", 55, "U_caller", "call"),
            ("hash-deadbeef", "U_callee", "src/app.cpp", 60, "U_caller", "ref"),
        ])
        calls = find_refs(populated_db, "hash-deadbeef", "modem_init", ref_kind="call")
        assert len(calls) == 1
        all_refs = find_refs(populated_db, "hash-deadbeef", "modem_init", ref_kind=None)
        assert len(all_refs) == 2

    def test_find_refs_unknown_caller_null_from_usr(self, populated_db):
        self._setup_symbols(populated_db)
        # reference from file scope (from_usr NULL) — LEFT JOIN keeps it
        insert_refs_batch(populated_db, [
            ("hash-deadbeef", "U_callee", "src/app.cpp", 5, None, "call"),
        ])
        rows = find_refs(populated_db, "hash-deadbeef", "modem_init")
        assert len(rows) == 1
        assert rows[0]["caller_name"] is None

    def test_delete_refs_for_file(self, populated_db):
        self._setup_symbols(populated_db)
        insert_refs_batch(populated_db, [
            ("hash-deadbeef", "U_callee", "src/app.cpp", 55, "U_caller", "call"),
            ("hash-deadbeef", "U_callee", "src/other.cpp", 5, None, "call"),
        ])
        assert count_refs(populated_db, "hash-deadbeef") == 2
        delete_refs_for_file(populated_db, "hash-deadbeef", "src/app.cpp")
        assert count_refs(populated_db, "hash-deadbeef") == 1
        remaining = find_refs(populated_db, "hash-deadbeef", "modem_init")
        assert remaining[0]["from_file"] == "src/other.cpp"

    def test_find_refs_no_match(self, populated_db):
        self._setup_symbols(populated_db)
        rows = find_refs(populated_db, "hash-deadbeef", "nonexistent")
        assert rows == []

    def test_find_refs_with_partial_namespace(self, populated_db):
        """Partially-qualified names (Class::method) resolve via suffix LIKE."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/wrapper.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "src/modem.cpp",
             split_tokens("send", "ns::DRIVER::send"),
             "U_drv_send", "send", "ns::DRIVER::send", "method",
             100, 1, 0, 1, "void send(char* data)", "", None),
            ("hash-deadbeef", fid, "src/wrapper.cpp",
             split_tokens("transmit", "ns::WRAPPER::transmit"),
             "U_wrp_xmit", "transmit", "ns::WRAPPER::transmit", "method",
             50, 1, 0, 1, "void transmit()", "", None),
        ])
        # Reference: WRAPPER::transmit calls DRIVER::send
        insert_refs_batch(populated_db, [
            ("hash-deadbeef", "U_drv_send", "src/wrapper.cpp", 55, "U_wrp_xmit", "call"),
        ])

        # Partially-qualified name should resolve via suffix LIKE
        rows = find_refs(populated_db, "hash-deadbeef", "DRIVER::send", ref_kind="call")
        assert len(rows) == 1
        assert rows[0]["caller_qname"] == "ns::WRAPPER::transmit"

        # Fully-qualified still works
        rows = find_refs(populated_db, "hash-deadbeef", "ns::DRIVER::send", ref_kind="call")
        assert len(rows) == 1

    def test_find_refs_partial_namespace_aggregate(self, populated_db):
        """Partially-qualified class name resolves for aggregate prefix matching."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/drv.cpp", "cpp")
        # Use USRs where method USR shares class USR prefix (matching libclang convention)
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "src/drv.cpp",
             split_tokens("DRIVER", "ns::DRIVER"),
             "c:@N@ns@S@DRIVER", "DRIVER", "ns::DRIVER", "class",
             1, 1, 0, 1, "", "", None),
            ("hash-deadbeef", fid, "src/drv.cpp",
             split_tokens("send", "ns::DRIVER::send"),
             "c:@N@ns@S@DRIVER@F@send#1", "send", "ns::DRIVER::send", "method",
             10, 1, 0, 1, "void send()", "", None),
            ("hash-deadbeef", fid, "src/main.cpp",
             split_tokens("main", "main"),
             "U_main", "main", "main", "function",
             1, 1, 0, 1, "int main()", "", None),
        ])
        insert_refs_batch(populated_db, [
            ("hash-deadbeef", "c:@N@ns@S@DRIVER@F@send#1", "src/main.cpp", 5, "U_main", "call"),
        ])

        # Partially-qualified class name → aggregate prefix match
        rows = find_refs(populated_db, "hash-deadbeef", "DRIVER")
        assert len(rows) == 1
        assert rows[0]["caller_name"] == "main"


class TestEnumValue:
    """Enum constant values are stored and returned."""

    def test_enum_constant_with_value(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/cmd.h", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("StatusCode", "zbox::BleCmd::StatusCode"),
             "U_enum", "StatusCode", "zbox::BleCmd::StatusCode", "enum",
             19, 1, 36, 1, "", "", None),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID"),
             "U_tok_inv", "TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID",
             "enum_constant", 23, 1, 0, 1, "", "", -2),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL"),
             "U_ok", "OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL",
             "enum_constant", 21, 1, 0, 1, "", "", 1),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("DEVICE_ERROR", "zbox::BleCmd::StatusCode::DEVICE_ERROR"),
             "U_dev_err", "DEVICE_ERROR", "zbox::BleCmd::StatusCode::DEVICE_ERROR",
             "enum_constant", 28, 1, 0, 1, "", "", -7),
        ])

        # Verify enum_value is stored and retrievable
        row = populated_db.execute(
            "SELECT name, enum_value FROM symbols WHERE usr=?", ("U_tok_inv",)
        ).fetchone()
        assert row["name"] == "TOKEN_INVALID"
        assert row["enum_value"] == -2

        # Verify NULL for non-enum_constant (enum itself)
        row_enum = populated_db.execute(
            "SELECT enum_value FROM symbols WHERE usr=?", ("U_enum",)
        ).fetchone()
        assert row_enum["enum_value"] is None

    def test_get_file_map_groups_by_parent_enum(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/cmd.h", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("StatusCode", "zbox::BleCmd::StatusCode"),
             "U_enum", "StatusCode", "zbox::BleCmd::StatusCode", "enum",
             19, 1, 0, 1, "", "", None),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID"),
             "U_tok_inv", "TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID",
             "enum_constant", 23, 1, 0, 1, "", "", -2),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL"),
             "U_ok", "OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL",
             "enum_constant", 21, 1, 0, 1, "", "", 1),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("State", "zbox::BleCmd::State"),
             "U_state", "State", "zbox::BleCmd::State", "enum",
             90, 1, 0, 1, "", "", None),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("Idle", "zbox::BleCmd::State::Idle"),
             "U_idle", "Idle", "zbox::BleCmd::State::Idle",
             "enum_constant", 92, 1, 0, 1, "", "", 0),
        ])

        from fw_context_mcp.indexer.db import get_file_map
        result = get_file_map(populated_db, "hash-deadbeef", "src/ble_cmd.h", max_per_kind=0)

        enum_const = result["symbols"]["enum_constant"]
        assert enum_const["count"] == 3
        assert "subgroups" in enum_const
        assert len(enum_const["subgroups"]) == 2

        # Find StatusCode subgroup
        subgroups_by_name = {s["name"]: s for s in enum_const["subgroups"]}
        assert "zbox::BleCmd::StatusCode" in subgroups_by_name
        sc = subgroups_by_name["zbox::BleCmd::StatusCode"]
        assert sc["count"] == 2
        assert len(sc["constants"]) == 2
        # Verify constants include name, qualified_name, line, and enum_value
        sc_names = {c["name"] for c in sc["constants"]}
        assert sc_names == {"OPERATION_SUCCESSFUL", "TOKEN_INVALID"}
        for c in sc["constants"]:
            assert "enum_value" in c
            assert "qualified_name" in c
            assert "line" in c
        tok_const = next(c for c in sc["constants"] if c["name"] == "TOKEN_INVALID")
        assert tok_const["enum_value"] == -2

        # State subgroup
        st = subgroups_by_name["zbox::BleCmd::State"]
        assert st["count"] == 1
        idle = st["constants"][0]
        assert idle["name"] == "Idle"
        assert idle["enum_value"] == 0

    def test_enum_constant_searchable_by_name(self, populated_db):
        """Enum constants remain searchable by name via FTS5."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/cmd.h", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID"),
             "U_tok_inv", "TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID",
             "enum_constant", 23, 1, 0, 1, "", "", -2),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL"),
             "U_ok", "OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL",
             "enum_constant", 21, 1, 0, 1, "", "", 1),
        ])

        results = search_symbols(populated_db, "TOKEN", "hash-deadbeef")
        assert len(results) == 1
        assert results[0]["name"] == "TOKEN_INVALID"
        assert results[0]["enum_value"] == -2


class TestIntegrityCheck:
    """Verify that open_db() detects database corruption."""

    def test_integrity_ok(self, tmpdir):
        """A freshly created database passes integrity check."""
        db_path = tmpdir / "ok.db"
        conn = open_db(db_path)
        conn.close()
        # Re-opening should still pass
        conn2 = open_db(db_path)
        conn2.close()

    def test_corrupt_db_raises_database_corruption_error(self, tmpdir):
        """A file that is not a valid SQLite database raises DatabaseCorruptionError."""
        db_path = tmpdir / "not_a_db.db"
        db_path.write_text("this is not a sqlite database", encoding="utf-8")
        with pytest.raises(DatabaseCorruptionError):
            open_db(db_path)

    def test_corrupt_db_includes_path_in_message(self, tmpdir):
        """The exception message includes the database path."""
        db_path = tmpdir / "garbage.db"
        db_path.write_text("garbage", encoding="utf-8")
        with pytest.raises(DatabaseCorruptionError) as exc_info:
            open_db(db_path)
        assert str(db_path) in str(exc_info.value)

    def test_corrupt_db_has_action_hint(self, tmpdir):
        """The exception carries db_path and details attributes."""
        db_path = tmpdir / "broken.db"
        db_path.write_text("broken", encoding="utf-8")
        with pytest.raises(DatabaseCorruptionError) as exc_info:
            open_db(db_path)
        assert exc_info.value.db_path == str(db_path)
        assert exc_info.value.details  # non-empty
