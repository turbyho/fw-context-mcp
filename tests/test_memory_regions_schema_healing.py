"""`memory_regions` must appear in a database the version gate skips.

`CURRENT_SCHEMA_VERSION` is a HASH of the column set, so a schema change can
make it SMALLER.  `ensure_schema` migrates only when the stored version is
BELOW the current one; a stored version above it takes the `elif` branch,
which re-stamps and never runs `_SCHEMA`.

`_ensure_migrated_columns` saves every ADD COLUMN unconditionally.  A new
TABLE has no such net — only the self-healing CRITICAL_TABLE block, which
runs on every open.

Measured before the annotation: adding the table moved the version from
1739388653 down to 275729191, every existing index re-stamped itself, and a
full reindex of birdie1-v2-fw-v3 walked all 449 translation units and died
at the last step with "no such table: memory_regions".

The unit tests never saw it: they build a fresh database, where `_SCHEMA`
runs in full.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.db import (
    get_entry_point,
    get_entry_points_by_config,
    get_memory_regions,
    get_memory_regions_by_config,
    open_db,
    transaction,
)
from fw_context_mcp.indexer.db._schema import (
    _CRITICAL_TABLES,
    CURRENT_SCHEMA_VERSION,
)


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


class TestTheAnnotation:
    def test_the_table_is_in_the_self_healing_block(self):
        # The block runs on every open, which is the only unconditional
        # path a new table has.
        assert "memory_regions" in _CRITICAL_TABLES

    def test_its_index_is_too(self):
        assert "idx_memory_regions_config" in _CRITICAL_TABLES


class TestAnExistingDatabase:
    """The failure mode, reproduced."""

    def test_a_version_above_the_current_one(self, tmp_path: Path):
        # What every index held before this change: a version stamped from
        # an older column set, which happens to be a LARGER number.
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        try:
            with transaction(conn):
                conn.execute("DROP TABLE IF EXISTS memory_regions")
            assert "memory_regions" not in _tables(conn)
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1000}")
        finally:
            conn.close()

        conn = open_db(db_path)
        try:
            assert "memory_regions" in _tables(conn), (
                "the version gate skipped _SCHEMA and nothing created the table"
            )
        finally:
            conn.close()

    def test_a_version_equal_to_the_current_one(self, tmp_path: Path):
        # The state after one re-stamp: the gate now says "current" and no
        # migration runs at all.  This is what birdie1 held.
        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        try:
            with transaction(conn):
                conn.execute("DROP TABLE IF EXISTS memory_regions")
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        finally:
            conn.close()

        conn = open_db(db_path)
        try:
            assert "memory_regions" in _tables(conn)
        finally:
            conn.close()

    def test_the_table_is_usable_after_healing(self, tmp_path: Path):
        # Creating it is not enough: the columns the code writes must be
        # there, so the healed table has to match the real one.
        from fw_context_mcp.indexer.db import (
            get_memory_regions,
            replace_memory_regions,
            upsert_build_config,
            upsert_project,
        )

        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        try:
            with transaction(conn):
                conn.execute("DROP TABLE IF EXISTS memory_regions")
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        finally:
            conn.close()

        conn = open_db(db_path)
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(tmp_path))
                upsert_build_config(conn, "ch", "pid", "cc.json")
            with transaction(conn):
                replace_memory_regions(conn, "ch", [{
                    "name": "FLASH", "attributes": "rx",
                    "origin": "0x8000000", "length": "64K",
                    "origin_value": 0x8000000, "length_value": 65536,
                    "file_path": "app.ld", "line": 3,
                }])
            regions = get_memory_regions(conn, "ch")
        finally:
            conn.close()
        assert [r["name"] for r in regions] == ["FLASH"]
        assert regions[0]["length_value"] == 65536


class TestTheReadPathToleratesAnOlderIndex:
    """Neither read path of the MCP server migrates a database.

    `_quick_open_readonly` skips the schema block by design — its docstring
    says so — and `SyncQueryExecutor` connects with plain sqlite3 and
    "deliberately differs from open_db".  An index written before this
    feature therefore holds neither the column nor the table, and a query
    that requires them raises.

    Measured: HA_Boiler holds user_version 155546819 and no `entry_point`
    column, and naming that column in a shared SELECT made
    `get_active_build` raise `no such column: b.entry_point`.

    "Not recorded" is the honest answer there, and it is already the answer
    for a build system that records no linker script.
    """

    def _old_index(self, tmp_path: Path):
        """A connection to a database with neither the column nor the table.

        Opened WITHOUT `open_db` afterwards, the way the read paths do, so
        nothing heals it behind the test's back.
        """
        import sqlite3 as raw

        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        try:
            # Every column the table really has, minus the new one.  Keeping
            # the rest matters: `get_all_builds_for_project` needs
            # `created_at` and the others, and a fixture that dropped them
            # would fail for a reason an older index never has.
            kept = [
                row[1]
                for row in conn.execute("PRAGMA table_info(build_configs)")
                if row[1] != "entry_point"
            ]
            assert "entry_point" not in kept
            assert "created_at" in kept, "the fixture must keep the old columns"
            columns = ", ".join(kept)
            with transaction(conn):
                conn.execute("DROP TABLE IF EXISTS memory_regions")
                # SQLite before 3.35 cannot DROP COLUMN, and rebuilding is
                # the portable way to reach the shape an older schema had.
                conn.execute(
                    f"CREATE TABLE bc_old AS SELECT {columns} "  # noqa: S608
                    f"FROM build_configs"
                )
                conn.execute("DROP TABLE build_configs")
                conn.execute("ALTER TABLE bc_old RENAME TO build_configs")
                conn.execute(
                    "INSERT INTO build_configs (config_hash, project_id) "
                    "VALUES ('ch', 'pid')"
                )
        finally:
            conn.close()
        plain = raw.connect(str(db_path))
        plain.row_factory = raw.Row
        return plain

    def test_the_regions_of_one_config(self, tmp_path):
        conn = self._old_index(tmp_path)
        try:
            assert get_memory_regions(conn, "ch") == []
        finally:
            conn.close()

    def test_the_regions_of_several_configs(self, tmp_path):
        conn = self._old_index(tmp_path)
        try:
            assert get_memory_regions_by_config(conn, ["ch", "other"]) == {}
        finally:
            conn.close()

    def test_the_entry_point_of_one_config(self, tmp_path):
        conn = self._old_index(tmp_path)
        try:
            assert get_entry_point(conn, "ch") == ""
        finally:
            conn.close()

    def test_the_entry_points_of_several_configs(self, tmp_path):
        conn = self._old_index(tmp_path)
        try:
            assert get_entry_points_by_config(conn, ["ch", "other"]) == {}
        finally:
            conn.close()

    def test_the_shared_build_query_needs_no_new_column(self, tmp_path):
        # `get_all_builds_for_project` runs on both read paths, thus it must
        # name no column an older index can lack.
        from fw_context_mcp.indexer.db import get_all_builds_for_project

        conn = self._old_index(tmp_path)
        try:
            rows = get_all_builds_for_project(conn, "pid")
        finally:
            conn.close()
        assert len(rows) == 1


class TestTheFingerprintDescribesTheSchema:
    """`CURRENT_SCHEMA_VERSION` is only useful if it covers every column.

    The version is a hash of what `_parse_expected_columns` reads out of
    `_SCHEMA` with a regex.  A column the parse misses is a column whose
    addition does not move the version — and an existing index then reports
    itself as current while the column is absent.

    Measured while adding `memory_regions`: a comment inside `_SCHEMA` that
    held the two words which open a table definition made the parse read the
    following `CREATE INDEX` as a table body.  The fingerprint then held ONE
    column for that table instead of ten, and nothing said so.
    """

    def test_every_table_matches_the_database(self, tmp_path: Path):
        from fw_context_mcp.indexer.db._schema import (
            _MIGRATION_ADD_COLUMNS,
            _SCHEMA,
            _parse_expected_columns,
        )

        conn = open_db(tmp_path / "index.db")
        try:
            real = {}
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ):
                table = row[0]
                real[table] = {
                    info[1]
                    for info in conn.execute(f"PRAGMA table_info({table})")
                }
        finally:
            conn.close()

        parsed = _parse_expected_columns(_SCHEMA, _MIGRATION_ADD_COLUMNS)
        problems = []
        for table, columns in parsed.items():
            actual = real.get(table)
            if actual is None:
                # A virtual or FTS table the parse also picks up; those are
                # rebuilt on every open and carry no fingerprint.
                continue
            missing = columns - actual
            extra = actual - columns
            if missing or extra:
                problems.append(
                    f"{table}: parse misses {sorted(extra)}, "
                    f"parse invents {sorted(missing)}"
                )
        assert not problems, "\n".join(problems)

    def test_no_comment_holds_the_words_that_open_a_table(self):
        from fw_context_mcp.indexer.db._schema import _SCHEMA

        # Built from parts so this test does not trip its own check.
        needle = "CREATE" + " TABLE"
        offenders = [
            line.strip()
            for line in _SCHEMA.splitlines()
            if line.strip().startswith("--") and needle in line.upper()
        ]
        assert not offenders, (
            "a comment in _SCHEMA holds the words that open a table "
            f"definition, which corrupts the fingerprint: {offenders}"
        )


class TestTheMechanismItself:
    """`memory_regions` is the first table to use the self-healing block.

    Before this change the block was EMPTY: the extraction reads
    `-- CRITICAL_TABLE` comments out of `_SCHEMA`, and no table carried one.
    The mechanism was written and never used, so these tests also cover the
    extraction, which nothing else exercises.

    A table that has always existed does not need the annotation — every
    database already holds it.  A table added by a schema change does, for
    as long as the version gate can skip `_SCHEMA`.
    """

    def test_the_block_is_no_longer_empty(self):
        assert _CRITICAL_TABLES.strip(), (
            "the extraction found no CRITICAL_TABLE annotation at all"
        )

    def test_the_block_is_valid_sql(self, tmp_path: Path):
        # A syntax error here would break EVERY open, because the block runs
        # unconditionally.
        import sqlite3 as raw

        conn = raw.connect(str(tmp_path / "probe.db"))
        try:
            conn.executescript(
                "CREATE TABLE build_configs (config_hash TEXT PRIMARY KEY);\n"
                + _CRITICAL_TABLES
            )
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN "
                    "('table','index')"
                )
            }
        finally:
            conn.close()
        assert "memory_regions" in names
        assert "idx_memory_regions_config" in names

    def test_running_the_block_twice_is_safe(self, tmp_path: Path):
        # It runs on every open, thus every statement in it must be
        # idempotent — CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT
        # EXISTS.
        import sqlite3 as raw

        conn = raw.connect(str(tmp_path / "probe.db"))
        try:
            conn.executescript(
                "CREATE TABLE build_configs (config_hash TEXT PRIMARY KEY);"
            )
            conn.executescript(_CRITICAL_TABLES)
            conn.executescript(_CRITICAL_TABLES)
        finally:
            conn.close()
