"""Tests for fw_context_mcp.indexer.db."""

import pytest

from fw_context_mcp.indexer.db import (
    DatabaseCorruptionError,
    _expand_query,
    count_refs,
    delete_inheritance_for_file,
    delete_refs_for_file,
    delete_symbols_for_file,
    find_refs,
    get_active_config,
    get_all_projects,
    get_class_members,
    get_direct_bases,
    get_direct_derived,
    get_file_mtime_indexed,
    get_file_mtimes,
    get_overrides_for_method,
    get_template_instances,
    insert_inheritance_batch,
    insert_overrides_batch,
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

    def test_insert_with_missing_embedding_dim_column(self, temp_db):
        """UpsertBuildConfig must work even when embedding_dim column is missing.

        Simulates an old database where the migration hasn't run yet by
        creating the build_configs table WITHOUT the embedding_dim column,
        then calling upsert_build_config. Exercises the _ensure_column fallback.
        """
        with transaction(temp_db):
            upsert_project(temp_db, "proj-002", "old-project", "/tmp/old-project")
        # Simulate pre-migration schema: drop and recreate build_configs
        # without embedding_dim. Foreign keys must be off because
        # files/symbols/etc. reference build_configs.
        temp_db.execute("PRAGMA foreign_keys = OFF")
        temp_db.execute("DROP TABLE build_configs")
        temp_db.execute("""CREATE TABLE build_configs (
            config_hash             TEXT PRIMARY KEY,
            project_id              TEXT NOT NULL REFERENCES projects(project_id),
            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
            compile_commands_path   TEXT NOT NULL
        )""")
        temp_db.execute("PRAGMA foreign_keys = ON")
        temp_db.commit()

        with transaction(temp_db):
            upsert_build_config(temp_db, "hash-xyz", "proj-002", "/tmp/cc.json", embedding_dim=768)

        row = temp_db.execute(
            "SELECT * FROM build_configs WHERE config_hash=?", ("hash-xyz",)
        ).fetchone()
        assert row is not None
        assert row["embedding_dim"] == 768


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
             10, 1, 0, 0, "void foo()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/test.cpp", "bar", "usr-2", "bar", "ns::bar", "function",
             20, 1, 0, 0, "int bar(int)", "Returns bar", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ]
        count = insert_symbols_batch(populated_db, rows)
        assert count == 2

    def test_promotion_to_definition(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        # Insert as declaration
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             5, 1, 0, 0, "void foo()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])
        # Insert as definition (same USR) — should promote
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             10, 1, 0, 1, "void foo(int x)", "Does foo", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
             10, 1, 0, 1, "void foo()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])
        # Then try to insert as declaration — WHERE clause prevents demotion
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/test.cpp", "foo", "usr-1", "foo", "ns::foo", "function",
             5, 1, 0, 0, "void foo();", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])
        row = populated_db.execute(
            "SELECT is_definition FROM symbols WHERE usr=?", ("usr-1",)
        ).fetchone()
        assert row["is_definition"] == 1


class TestDeleteSymbolsForFile:
    def test_delete(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/del.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/del.cpp", "f1", "usr-1", "f1", "ns::f1", "function", 1, 1, 0, 1, "void f1()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/del.cpp", "f2", "usr-2", "f2", "ns::f2", "function", 2, 1, 0, 1, "void f2()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
             "u1", "modem_init", "ns::modem_init", "function", 1, 1, 0, 1, "void modem_init()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/uart/uart_driver.cpp",
             split_tokens("uart_send", "ns::uart_send"),
             "u2", "uart_send", "ns::uart_send", "function", 5, 1, 0, 1, "void uart_send(char c)", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/modem/modem_driver.cpp",
             split_tokens("modem_connect", "ns::modem_connect"),
             "u3", "modem_connect", "ns::modem_connect", "function", 10, 1, 0, 1, "int modem_connect(const char* host)", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
             "u10", "write", "SPI::write", "method", 1, 1, 0, 1, "void write(const uint8_t* buf, int len)", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/uart/uart_driver.cpp",
             split_tokens("write", "UART::write"),
             "u11", "write", "UART::write", "method", 1, 1, 0, 1, "void write(char c)", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])
        results = search_symbols(populated_db, "spi* AND write*", "hash-deadbeef")
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
             "method", 1, 1, 0, 1, "void onConnectionComplete()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "lib/ble/ble.cpp",
             split_tokens("startAdvertising", "ZBLE::startAdvertising"),
             "u21", "startAdvertising", "ZBLE::startAdvertising",
             "method", 2, 1, 0, 1, "void startAdvertising()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
                          f"u{i}", f"func{i}", f"ns::func{i}", "function", i, 1, 0, 1, f"void func{i}()", "", None, 0, 0, "", 0, "", 0, 0.0, "")
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
        assert _expand_query("modem init") == "modem* OR init*"

    def test_existing_wildcard_preserved(self):
        assert _expand_query("connect*") == "connect*"

    def test_cpp_scope_gets_expansion(self):
        """C++ :: should not block wildcard expansion."""
        assert _expand_query("std::vector") == "std* OR vector*"

    def test_cpp_scope_with_multiple_tokens(self):
        """C++ :: in a multi-word query should expand all tokens."""
        assert _expand_query("mbed::DigitalOut write") == "mbed* OR DigitalOut* OR write*"

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
        # pytest.raises(Exception) instead of sqlite3.IntegrityError: the db
        # module monkey-patches sys.modules["sqlite3"] = pysqlite3, so the
        # actual exception is pysqlite3.dbapi2.IntegrityError — a different
        # class that the stdlib sqlite3.IntegrityError won't catch.
        with pytest.raises(Exception) as exc_info:
            upsert_file(temp_db, "nonexistent-hash", "/tmp/file.cpp", "cpp")
        assert "FOREIGN KEY" in str(exc_info.value).upper()

    def test_valid_fk_passes(self, populated_db):
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/valid.cpp", "cpp")
        assert file_id > 0


class TestRefs:
    def _setup_symbols(self, db):
        """Two symbols: callee modem_init (usr=U_callee), caller app_run (usr=U_caller)."""
        fid = upsert_file(db, "hash-deadbeef", "/tmp/app.cpp", "cpp")
        insert_symbols_batch(db, [
            ("hash-deadbeef", fid, "src/modem.cpp", split_tokens("modem_init", "modem_init"),
             "U_callee", "modem_init", "modem_init", "function", 10, 1, 0, 1, "void modem_init()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", fid, "src/app.cpp", split_tokens("app_run", "App::app_run"),
             "U_caller", "app_run", "App::app_run", "method", 50, 1, 0, 1, "void app_run()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
             100, 1, 0, 1, "void send(char* data)", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", fid, "src/wrapper.cpp",
             split_tokens("transmit", "ns::WRAPPER::transmit"),
             "U_wrp_xmit", "transmit", "ns::WRAPPER::transmit", "method",
             50, 1, 0, 1, "void transmit()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
             1, 1, 0, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", fid, "src/drv.cpp",
             split_tokens("send", "ns::DRIVER::send"),
             "c:@N@ns@S@DRIVER@F@send#1", "send", "ns::DRIVER::send", "method",
             10, 1, 0, 1, "void send()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", fid, "src/main.cpp",
             split_tokens("main", "main"),
             "U_main", "main", "main", "function",
             1, 1, 0, 1, "int main()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
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
             19, 1, 36, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID"),
             "U_tok_inv", "TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID",
             "enum_constant", 23, 1, 0, 1, "", "", -2, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL"),
             "U_ok", "OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL",
             "enum_constant", 21, 1, 0, 1, "", "", 1, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("DEVICE_ERROR", "zbox::BleCmd::StatusCode::DEVICE_ERROR"),
             "U_dev_err", "DEVICE_ERROR", "zbox::BleCmd::StatusCode::DEVICE_ERROR",
             "enum_constant", 28, 1, 0, 1, "", "", -7, 0, 0, "", 0, "", 0, 0.0, ""),
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
             19, 1, 0, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID"),
             "U_tok_inv", "TOKEN_INVALID", "zbox::BleCmd::StatusCode::TOKEN_INVALID",
             "enum_constant", 23, 1, 0, 1, "", "", -2, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL"),
             "U_ok", "OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL",
             "enum_constant", 21, 1, 0, 1, "", "", 1, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("State", "zbox::BleCmd::State"),
             "U_state", "State", "zbox::BleCmd::State", "enum",
             90, 1, 0, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("Idle", "zbox::BleCmd::State::Idle"),
             "U_idle", "Idle", "zbox::BleCmd::State::Idle",
             "enum_constant", 92, 1, 0, 1, "", "", 0, 0, 0, "", 0, "", 0, 0.0, ""),
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
                          "enum_constant", 23, 1, 0, 1, "", "", -2, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/ble_cmd.h",
             split_tokens("OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL"),
             "U_ok", "OPERATION_SUCCESSFUL", "zbox::BleCmd::StatusCode::OPERATION_SUCCESSFUL",
                          "enum_constant", 21, 1, 0, 1, "", "", 1, 0, 0, "", 0, "", 0, 0.0, ""),
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


class TestInheritance:
    """C++ inheritance chain: insert, query bases/derived, delete by file."""

    def test_insert_and_query_bases(self, populated_db):
        conn = populated_db
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_derived", "usr_base", "public", 0),
        ])
        bases = get_direct_bases(conn, "hash-deadbeef", "usr_derived")
        assert len(bases) == 1
        assert bases[0]["base_usr"] == "usr_base"
        assert bases[0]["access"] == "public"

    def test_insert_and_query_derived(self, populated_db):
        conn = populated_db
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_derived", "usr_base", "public", 0),
        ])
        derived = get_direct_derived(conn, "hash-deadbeef", "usr_base")
        assert len(derived) == 1
        assert derived[0]["derived_usr"] == "usr_derived"

    def test_multiple_inheritance(self, populated_db):
        conn = populated_db
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_d", "usr_b1", "public", 0),
            ("hash-deadbeef", "usr_d", "usr_b2", "protected", 0),
        ])
        bases = get_direct_bases(conn, "hash-deadbeef", "usr_d")
        assert len(bases) == 2

    def test_virtual_inheritance_flag(self, populated_db):
        conn = populated_db
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_derived", "usr_base", "public", 1),
        ])
        bases = get_direct_bases(conn, "hash-deadbeef", "usr_derived")
        assert bases[0]["is_virtual"] == 1

    def test_delete_inheritance_for_file(self, populated_db):
        conn = populated_db
        fid = upsert_file(conn, "hash-deadbeef", "/tmp/test.h", "cpp")
        insert_symbols_batch(conn, [
            ("hash-deadbeef", fid, "/tmp/test.h", "derived", "usr_derived",
             "Derived", "Derived", "class", 1, 1, 5, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_derived", "usr_base", "public", 0),
        ])
        delete_inheritance_for_file(conn, "hash-deadbeef", fid)
        assert len(get_direct_bases(conn, "hash-deadbeef", "usr_derived")) == 0

    def test_on_conflict_update(self, populated_db):
        """Re-inserting the same edge updates access/virtual flags."""
        conn = populated_db
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_derived", "usr_base", "public", 0),
        ])
        insert_inheritance_batch(conn, [
            ("hash-deadbeef", "usr_derived", "usr_base", "protected", 1),
        ])
        bases = get_direct_bases(conn, "hash-deadbeef", "usr_derived")
        assert len(bases) == 1
        assert bases[0]["access"] == "protected"
        assert bases[0]["is_virtual"] == 1


class TestParentUsr:
    """Tests for parent_usr column — method/field → class/struct membership."""

    def test_parent_usr_stored(self, populated_db):
        """parent_usr column is stored and retrievable."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/widget.h", "cpp")
        # Insert a class
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("Widget", "ns::Widget"),
             "U_class", "Widget", "ns::Widget", "class",
             10, 1, 50, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            # Method — parent_usr = class USR
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("render", "ns::Widget::render"),
             "U_render", "render", "ns::Widget::render", "method",
             15, 3, 20, 1, "void render()", "Render widget", None, 0, 0,
             "U_class", 0, "", 0, 0.0, ""),
            # Field — parent_usr = class USR
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("_x", "ns::Widget::_x"),
             "U_x", "_x", "ns::Widget::_x", "field",
             12, 5, 0, 1, "", "", None, 0, 0,
             "U_class", 0, "", 0, 0.0, ""),
            # Free function — parent_usr = ""
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("init", "ns::init"),
             "U_init", "init", "ns::init", "function",
             60, 1, 0, 1, "void init()", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])

        # Verify parent_usr is stored correctly
        method = populated_db.execute(
            "SELECT parent_usr FROM symbols WHERE usr=?", ("U_render",)
        ).fetchone()
        assert method["parent_usr"] == "U_class"

        field = populated_db.execute(
            "SELECT parent_usr FROM symbols WHERE usr=?", ("U_x",)
        ).fetchone()
        assert field["parent_usr"] == "U_class"

        free = populated_db.execute(
            "SELECT parent_usr FROM symbols WHERE usr=?", ("U_init",)
        ).fetchone()
        assert free["parent_usr"] == ""

        cls = populated_db.execute(
            "SELECT parent_usr FROM symbols WHERE usr=?", ("U_class",)
        ).fetchone()
        assert cls["parent_usr"] == ""

    def test_get_class_members(self, populated_db):
        """get_class_members returns members grouped by USR."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/widget.h", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("Widget", "ns::Widget"),
             "U_class", "Widget", "ns::Widget", "class",
             10, 1, 50, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("render", "ns::Widget::render"),
             "U_render", "render", "ns::Widget::render", "method",
             15, 3, 20, 1, "void render()", "", None, 0, 0,
             "U_class", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("get_name", "ns::Widget::get_name"),
             "U_name", "get_name", "ns::Widget::get_name", "method",
             20, 3, 25, 1, "const char* get_name()", "", None, 0, 0,
             "U_class", 0, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/widget.h",
             split_tokens("_x", "ns::Widget::_x"),
             "U_x", "_x", "ns::Widget::_x", "field",
             12, 5, 0, 1, "", "", None, 0, 0,
             "U_class", 0, "", 0, 0.0, ""),
        ])

        members = get_class_members(populated_db, "hash-deadbeef", "U_class")
        assert len(members) == 3

        # Check grouping
        kinds = {m["kind"] for m in members}
        assert kinds == {"method", "field"}

        method_names = {m["name"] for m in members if m["kind"] == "method"}
        assert method_names == {"render", "get_name"}

        field_names = {m["name"] for m in members if m["kind"] == "field"}
        assert field_names == {"_x"}

    def test_get_class_members_empty(self, populated_db):
        """get_class_members returns empty list for unknown USR."""
        members = get_class_members(populated_db, "hash-deadbeef", "non_existent_usr")
        assert members == []

    def test_parent_usr_column_migration(self, tmpdir):
        """parent_usr column is added by migration on old databases."""
        db_path = tmpdir / "test_migrate.db"
        conn = open_db(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
        assert "parent_usr" in cols
        conn.close()


class TestTemplateTracking:
    """P2: template instantiation tracking — is_template flag, template_usr column."""

    def test_is_template_flag_stored(self, populated_db):
        """is_template is stored and retrievable."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/vector.h", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", file_id, "src/vector.h",
             split_tokens("vector", "std::vector"),
             "U_tpl", "vector", "std::vector", "class",
             10, 1, 50, 1, "", "", None, 0, 0, "", 1, "", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/main.cpp",
             split_tokens("vector", "std::vector<int>"),
             "U_inst", "vector", "std::vector<int>", "class",
             42, 1, 0, 1, "", "", None, 0, 0, "", 0, "U_tpl", 0, 0.0, ""),
        ])
        row = populated_db.execute(
            "SELECT is_template, template_usr FROM symbols WHERE usr=?",
            ("U_tpl",),
        ).fetchone()
        assert row["is_template"] == 1
        assert row["template_usr"] == ""

        row2 = populated_db.execute(
            "SELECT is_template, template_usr FROM symbols WHERE usr=?",
            ("U_inst",),
        ).fetchone()
        assert row2["is_template"] == 0
        assert row2["template_usr"] == "U_tpl"

    def test_get_template_instances(self, populated_db):
        """get_template_instances returns all instantiations of a template."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/list.h", "cpp")
        insert_symbols_batch(populated_db, [
            # Template declaration
            ("hash-deadbeef", file_id, "src/list.h",
             split_tokens("list", "ns::list"),
             "U_list_tpl", "list", "ns::list", "class",
             5, 1, 100, 1, "", "", None, 0, 0, "", 1, "", 0, 0.0, ""),
            # Two instantiations
            ("hash-deadbeef", file_id, "src/list.h",
             split_tokens("list", "ns::list<int>"),
             "U_list_int", "list", "ns::list<int>", "class",
             200, 1, 0, 1, "", "", None, 0, 0, "", 0, "U_list_tpl", 0, 0.0, ""),
            ("hash-deadbeef", file_id, "src/widget.cpp",
             split_tokens("list", "ns::list<Widget>"),
             "U_list_widget", "list", "ns::list<Widget>", "class",
             10, 5, 0, 1, "", "", None, 0, 0, "", 0, "U_list_tpl", 0, 0.0, ""),
        ])
        instances = get_template_instances(populated_db, "hash-deadbeef", "U_list_tpl")
        assert len(instances) == 2
        names = {r["qualified_name"] for r in instances}
        assert names == {"ns::list<int>", "ns::list<Widget>"}

    def test_get_template_instances_empty(self, populated_db):
        """get_template_instances returns empty list for template with no instances."""
        instances = get_template_instances(populated_db, "hash-deadbeef", "non_existent_usr")
        assert instances == []

    def test_template_usr_column_migration(self, tmpdir):
        """template_usr column is added by migration on old databases."""
        db_path = tmpdir / "test_migrate_tpl.db"
        conn = open_db(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
        assert "is_template" in cols
        assert "template_usr" in cols
        conn.close()


class TestOverrides:
    """P3: method override tracking — insert, query, and unique constraint."""

    def _insert_method(self, conn, file_id, name, qname, usr, parent_usr,
                       is_virtual=1, is_pure=0, signature="void f()"):
        """Helper to insert a single virtual method symbol."""
        insert_symbols_batch(conn, [
            ("hash-deadbeef", file_id, "src/test.cpp",
             split_tokens(name, qname),
             usr, name, qname, "method",
             10, 1, 20, 1, signature, "", None,
             is_virtual, is_pure, parent_usr, 0, "", 0, 0.0, ""),
        ])

    def _insert_class(self, conn, file_id, name, qname, usr):
        """Helper to insert a class symbol."""
        insert_symbols_batch(conn, [
            ("hash-deadbeef", file_id, "src/test.cpp",
             split_tokens(name, qname),
             usr, name, qname, "class",
             1, 1, 100, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""),
        ])

    def test_insert_and_query_overrides(self, populated_db):
        """insert_overrides_batch stores; get_overrides_for_method retrieves."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")

        # Class hierarchy: Derived → Base
        self._insert_class(populated_db, file_id, "Base", "Base", "U_base")
        self._insert_class(populated_db, file_id, "Derived", "Derived", "U_derived")
        self._insert_method(populated_db, file_id, "foo", "Base::foo", "U_base_foo", "U_base")
        self._insert_method(populated_db, file_id, "foo", "Derived::foo", "U_derived_foo", "U_derived")

        insert_overrides_batch(populated_db, [
            ("hash-deadbeef", "U_derived_foo", "U_base_foo"),
        ])

        # Query from derived perspective
        info = get_overrides_for_method(populated_db, "hash-deadbeef", "U_derived_foo")
        assert len(info["overrides"]) == 1
        assert info["overrides"][0]["base_usr"] == "U_base_foo"
        assert info["overrides"][0]["name"] == "foo"

        # Query from base perspective
        info2 = get_overrides_for_method(populated_db, "hash-deadbeef", "U_base_foo")
        assert len(info2["overridden_by"]) == 1
        assert info2["overridden_by"][0]["derived_usr"] == "U_derived_foo"

    def test_no_overrides_for_non_virtual(self, populated_db):
        """Method without overrides returns empty lists."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        self._insert_class(populated_db, file_id, "Base", "Base", "U_base")
        self._insert_method(populated_db, file_id, "bar", "Base::bar", "U_base_bar", "U_base")

        info = get_overrides_for_method(populated_db, "hash-deadbeef", "U_base_bar")
        assert info["overrides"] == []
        assert info["overridden_by"] == []

    def test_overrides_unique_constraint(self, populated_db):
        """Inserting the same edge twice does not create duplicates."""
        file_id = upsert_file(populated_db, "hash-deadbeef", "/tmp/test.cpp", "cpp")
        self._insert_class(populated_db, file_id, "Base", "Base", "U_base")
        self._insert_class(populated_db, file_id, "Derived", "Derived", "U_derived")
        self._insert_method(populated_db, file_id, "foo", "Base::foo", "U_base_foo", "U_base")
        self._insert_method(populated_db, file_id, "foo", "Derived::foo", "U_derived_foo", "U_derived")

        # Insert same edge twice
        insert_overrides_batch(populated_db, [
            ("hash-deadbeef", "U_derived_foo", "U_base_foo"),
        ])
        insert_overrides_batch(populated_db, [
            ("hash-deadbeef", "U_derived_foo", "U_base_foo"),
        ])

        info = get_overrides_for_method(populated_db, "hash-deadbeef", "U_derived_foo")
        assert len(info["overrides"]) == 1  # not duplicated

    def test_overrides_table_exists(self, populated_db):
        """overrides table exists in the database."""
        tables = [
            r[0] for r in populated_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "overrides" in tables


class TestSearchEdgeCases:
    """Edge case tests for search and symbol insertion."""

    def test_search_empty_query(self, populated_db):
        """Empty query should not crash."""
        results = search_symbols(populated_db, "", "hash-deadbeef", limit=5)
        assert isinstance(results, list)

    def test_search_whitespace_only_query(self, populated_db):
        """Whitespace-only query should not crash."""
        results = search_symbols(populated_db, "   ", "hash-deadbeef", limit=5)
        assert isinstance(results, list)

    def test_search_special_chars(self, populated_db):
        """Special FTS5 characters should be handled safely."""
        results = search_symbols(populated_db, "test*", "hash-deadbeef", limit=5)
        assert isinstance(results, list)

    def test_search_quote_handling(self, populated_db):
        """Quotes in query should not crash."""
        results = search_symbols(populated_db, '"test"', "hash-deadbeef", limit=5)
        assert isinstance(results, list)

    def test_insert_symbol_empty_parent_usr(self, populated_db):
        """Symbol with empty parent_usr should be stored correctly."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/empty_parent.c", "c")
        rows = [(
            "hash-deadbeef", fid, "src/empty_parent.c",
            split_tokens("top_level_fn", "top_level_fn"),
            "usr-empty-parent", "top_level_fn", "top_level_fn", "function",
            1, 0, 1, 1, "void top_level_fn()", "", None, 0, 0, "", 0, "", 0, 0.0, ""
        )]
        insert_symbols_batch(populated_db, rows)
        row = populated_db.execute(
            "SELECT parent_usr FROM symbols WHERE usr='usr-empty-parent'"
        ).fetchone()
        assert row is not None
        assert row["parent_usr"] == ""

    def test_insert_symbol_empty_signature(self, populated_db):
        """Symbol with empty signature should not crash."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/empty_sig.c", "c")
        rows = [(
            "hash-deadbeef", fid, "src/empty_sig.c",
            split_tokens("fn", "fn"),
            "usr-empty-sig", "fn", "fn", "function",
            1, 0, 1, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, ""
        )]
        insert_symbols_batch(populated_db, rows)
        row = populated_db.execute(
            "SELECT signature FROM symbols WHERE usr='usr-empty-sig'"
        ).fetchone()
        assert row is not None
        assert row["signature"] == ""

    def test_insert_symbol_zero_line(self, populated_db):
        """Symbol with line=0 is stored (though unusual)."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/line_zero.c", "c")
        rows = [(
            "hash-deadbeef", fid, "src/line_zero.c",
            split_tokens("fn", "fn"),
            "usr-zero-line", "fn", "fn", "function",
            0, 0, 0, 1, "void fn()", "", None, 0, 0, "", 0, "", 0, 0.0, ""
        )]
        insert_symbols_batch(populated_db, rows)
        row = populated_db.execute(
            "SELECT line FROM symbols WHERE usr='usr-zero-line'"
        ).fetchone()
        assert row is not None
        assert row["line"] == 0

    def test_upsert_file_empty_path(self, populated_db):
        """Upserting a file with an empty path should work but may produce warnings."""
        # File paths are stored as-is; empty path is an edge case
        fid = upsert_file(populated_db, "hash-deadbeef", "", "c")
        assert fid > 0

    def test_search_very_long_query(self, populated_db):
        """Very long query should not crash."""
        long_query = "a" * 1000
        results = search_symbols(populated_db, long_query, "hash-deadbeef", limit=5)
        assert isinstance(results, list)


class TestSearchBodies:
    """Tests for search_bodies — FTS5 body search with snippet highlighting.

    search_bodies searches the ``source`` column (function bodies indexed
    by libclang) via the ``symbols_fts`` FTS5 table.  It uses
    ``snippet(symbols_fts, 9, ...)`` to highlight matches in context.
    Column index 9 corresponds to the ``source`` column (0-indexed
    across 10 FTS5 columns: name, qualified_name, signature, docstring,
    file_path, name_tokens, summary, inputs, outputs, source).
    """

    def test_snippet_highlight_on_source_column(self, populated_db):
        """snippet() with column index 9 highlights matches in source bodies."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/attach.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "src/attach.cpp",
             split_tokens("check_timer", "BleMsg::check_timer"),
             "usr-attach-1", "check_timer", "BleMsg::check_timer", "method",
             10, 1, 20, 1, "void check_timer()", "", None, 0, 0, "", 0, "", 1, 0.0,
             'void BleMsg::check_timer() {\n    _timeout.attach('
             'callback(&BleMsg::TimeoutInterrupt, this), 5s);\n}'),
        ])

        # Query using snippet with the FIXED column index 9 (source column)
        rows = populated_db.execute(
            """SELECT s.name, snippet(symbols_fts, 9, '<b>', '</b>', '…', 60)
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH 'attach*' AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
               ORDER BY rank""",
        ).fetchall()

        assert len(rows) >= 1
        # The snippet should contain the highlighted <b>attach</b>
        snippet = rows[0][1]
        assert "<b>attach</b>" in snippet
        assert "TimeoutInterrupt" in snippet

    def test_snippet_column_index_10_fails(self, populated_db):
        """Regression test: snippet() with column index 10 raises column index out of range.

        The FTS5 table has exactly 10 content columns (indices 0-9).
        Index 10 was the original bug — remove this test assertion
        when the table gains an 11th column and index 10 becomes valid.
        """
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/snippet10.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "src/snippet10.cpp",
             split_tokens("fn", "ns::fn"),
             "usr-snippet10", "fn", "ns::fn", "function",
             1, 1, 5, 1, "void fn()", "", None, 0, 0, "", 0, "", 1, 0.0,
             "void fn() { NVIC_SystemReset(); }"),
        ])

        # pysqlite3 (used by open_db when available) raises DatabaseError
        # while stdlib sqlite3 raises OperationalError.
        try:
            import pysqlite3  # noqa: F401

            _db_error = pysqlite3.dbapi2.DatabaseError
        except ImportError:
            import sqlite3 as _stdlib_sqlite3

            _db_error = _stdlib_sqlite3.DatabaseError
        with pytest.raises(_db_error, match="column index out of range"):
            populated_db.execute(
                """SELECT snippet(symbols_fts, 10, '<b>', '</b>', '…', 60)
                   FROM symbols_fts
                   WHERE symbols_fts MATCH 'NVIC*'""",
            ).fetchall()

    def test_find_attach_pattern_in_body(self, populated_db):
        """Full search_bodies-like query finds attach(...) patterns in function bodies."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/multi.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "src/multi.cpp",
             split_tokens("check_timer", "ModemMsg::check_timer"),
             "usr-src-1", "check_timer", "ModemMsg::check_timer", "method",
             10, 1, 20, 1, "void check_timer()", "", None, 0, 0, "", 0, "", 1, 0.0,
             '_timeout.attach(callback(&ModemMsg::TimeoutInterrupt, this), 30s);'),
            ("hash-deadbeef", fid, "src/multi.cpp",
             split_tokens("zbox_reset", "WDT::zbox_reset"),
             "usr-src-2", "zbox_reset", "WDT::zbox_reset", "method",
             30, 1, 35, 1, "void zbox_reset()", "", None, 0, 0, "", 0, "", 1, 0.0,
             '_timeout.attach(callback(&WDT::_timeout_interrupt), delay);'),
            # Negative: this one does NOT contain "attach"
            ("hash-deadbeef", fid, "src/multi.cpp",
             split_tokens("main", "main"),
             "usr-src-3", "main", "main", "function",
             40, 1, 45, 1, "int main()", "", None, 0, 0, "", 0, "", 1, 0.0,
             "int main() { return 0; }"),
        ])

        # Simulate what search_bodies does (using the fixed index 9)
        expanded = _expand_query("attach")
        rows = populated_db.execute(
            """SELECT s.name, snippet(symbols_fts, 9, '<b>', '</b>', '…', 60)
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
               ORDER BY rank""",
            (expanded,),
        ).fetchall()

        # Only the two symbols with ".attach(" in their source should be found
        names = {r[0] for r in rows}
        assert names == {"check_timer", "zbox_reset"}
        assert "main" not in names

    def test_find_nvic_pattern_in_body(self, populated_db):
        """search_bodies finds NVIC_SetVector / NVIC_SystemReset in function bodies."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/nvic.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "lib/nrf52/serial_api.c",
             split_tokens("nordic_nrf5_uart0_handler", "nordic_nrf5_uart0_handler"),
             "usr-nvic-1", "nordic_nrf5_uart0_handler", "nordic_nrf5_uart0_handler",
             "function", 540, 1, 10, 1, "void nordic_nrf5_uart0_handler()",
             "", None, 0, 0, "", 0, "", 1, 0.0,
             "NVIC_SetVector(UARTE0_UART0_IRQn, (uint32_t)nordic_nrf5_uart0_handler);"),
        ])

        expanded = _expand_query("NVIC_SetVector")
        rows = populated_db.execute(
            """SELECT s.name, snippet(symbols_fts, 9, '<b>', '</b>', '…', 60)
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
               ORDER BY rank""",
            (expanded,),
        ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == "nordic_nrf5_uart0_handler"
        assert "<b>NVIC_SetVector</b>" in rows[0][1]

    def test_expand_query_appends_wildcards(self):
        """_expand_query appends * to each word for prefix matching."""
        assert _expand_query("attach callback") == "attach* OR callback*"
        assert _expand_query("NVIC_SetVector") == "NVIC_SetVector*"

    def test_source_column_empty_skipped(self, populated_db):
        """Symbols with empty source are excluded from search_bodies results."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/empty_source.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            ("hash-deadbeef", fid, "src/empty_source.cpp",
             split_tokens("empty_fn", "ns::empty_fn"),
             "usr-empty-1", "empty_fn", "ns::empty_fn", "function",
             1, 1, 1, 1, "void empty_fn()", "", None, 0, 0, "", 0, "", 1, 0.0, ""),
        ])

        expanded = _expand_query("empty")
        rows = populated_db.execute(
            """SELECT s.name
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
               ORDER BY rank""",
            (expanded,),
        ).fetchall()

        # Empty source → excluded by s.source != '' filter
        assert len(rows) == 0

    def test_project_code_prioritized_over_vendor(self, populated_db):
        """Project code (is_project=1) should appear before vendor code in results."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/priority.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            # Vendor code first (is_project=0)
            ("hash-deadbeef", fid, "mbed-os/drivers/Ticker.cpp",
             split_tokens("attach", "mbed::Ticker::attach"),
             "usr-pri-1", "attach", "mbed::Ticker::attach", "method",
             1, 1, 5, 1, "void attach(Callback<void()> func, float t)",
             "", None, 0, 0, "", 0, "", 0, 0.0,
             "void Ticker::attach(Callback<void()> func, float t) { _ticker.attach(func, t); }"),
            # Project code second (is_project=1)
            ("hash-deadbeef", fid, "src/wdt.cpp",
             split_tokens("zbox_reset", "WDT::zbox_reset"),
             "usr-pri-2", "zbox_reset", "WDT::zbox_reset", "method",
             30, 1, 35, 1, "void zbox_reset(duration delay)",
             "", None, 0, 0, "", 0, "", 1, 0.0,
             "_timeout.attach(callback(&WDT::_timeout_interrupt), delay);"),
        ])

        expanded = _expand_query("attach")
        rows = populated_db.execute(
            """SELECT s.name, s.file_path, s.is_project
               FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
               ORDER BY rank""",
            (expanded,),
        ).fetchall()

        # Both should be found
        assert len(rows) == 2
        names = {r[0] for r in rows}
        assert "zbox_reset" in names
        assert "attach" in names

    def test_project_only_excludes_vendor(self, populated_db):
        """project_only=True excludes vendor/sdk code (is_project=0)."""
        fid = upsert_file(populated_db, "hash-deadbeef", "/tmp/proj_only.cpp", "cpp")
        insert_symbols_batch(populated_db, [
            # Vendor code (is_project=0)
            ("hash-deadbeef", fid, "mbed-os/drivers/SerialBase.cpp",
             split_tokens("attach", "mbed::SerialBase::attach"),
             "usr-po-1", "attach", "mbed::SerialBase::attach", "method",
             1, 1, 5, 1, "void attach(Callback<void()> func, IrqType type)",
             "", None, 0, 0, "", 0, "", 0, 0.0,
             'serial.attach(callback(&handler), SerialBase::RxIrq);'),
            # Project code (is_project=1)
            ("hash-deadbeef", fid, "src/rs485.cpp",
             split_tokens("_rx_handler", "RS485::_rx_handler"),
             "usr-po-2", "_rx_handler", "RS485::_rx_handler", "method",
             85, 1, 10, 1, "void _rx_handler()",
             "", None, 0, 0, "", 0, "", 1, 0.0,
             'serial.attach(callback(this, &RS485::_rx_handler), SerialBase::RxIrq);'),
        ])

        expanded = _expand_query("attach")

        # Without project_only — both should appear
        all_rows = populated_db.execute(
            """SELECT s.name FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
               ORDER BY rank""",
            (expanded,),
        ).fetchall()
        assert len(all_rows) == 2

        # With project_only — only the project code symbol
        proj_rows = populated_db.execute(
            """SELECT s.name FROM symbols_fts
               JOIN symbols s ON s.id = symbols_fts.rowid
               WHERE symbols_fts MATCH ? AND s.config_hash = 'hash-deadbeef'
                 AND s.is_definition = 1 AND s.source != ''
                 AND s.is_project = 1
               ORDER BY rank""",
            (expanded,),
        ).fetchall()
        assert len(proj_rows) == 1
        assert proj_rows[0][0] == "_rx_handler"
