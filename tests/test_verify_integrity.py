"""Tests for post-index integrity verification and for cleanup coverage.

Two kinds of guard live here.

The first checks that ``_step_verify_integrity`` can actually SEE the failures
it exists for.  A verification step that always passes is worse than none: it
grants confidence.  Each check therefore has a case that breaks the index on
purpose and asserts the step notices.

The second is schema-driven.  A run of defects in this indexer came from a
cleanup path that missed a table.  These tests enumerate the tables from
``sqlite_master`` rather than from a hand-written list, so a table added later
is covered whether or not anyone remembers to come back here.
"""
from __future__ import annotations

import pytest

from fw_context_mcp.indexer._postprocess import (
    IndexIntegrityError,
    _step_verify_integrity,
)
from fw_context_mcp.indexer.db import delete_build_data, upsert_file
from fw_context_mcp.indexer.db._files import _delete_rows_owned_by

_HASH = "hash-deadbeef"
_PROBE_HASH = "probe-hash"


def _ctx(config_hash: str = _HASH, *, defer_fts: bool = False) -> dict:
    return {"config_hash": config_hash, "defer_fts": defer_fts}


def _data_tables(conn) -> list[str]:
    """Every real data table, with FTS5 and vec0 virtual tables removed.

    Taken from the schema rather than a literal list — that is the whole
    point of the coverage tests below.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if "_fts" not in r[0] and not r[0].startswith("vec_")]


def _tables_with_any_column(conn, columns: set[str]) -> list[tuple[str, str]]:
    """Return ``(table, column)`` for each data table carrying one of *columns*."""
    found: list[tuple[str, str]] = []
    for table in _data_tables(conn):
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        for col in info:
            if col[1] in columns:
                found.append((table, col[1]))
                break
    return found


def _insert_probe_row(conn, table: str, overrides: dict) -> None:
    """Insert one row into *table*, filling required columns with dummies.

    Generic on purpose: the coverage tests must be able to populate a table
    nobody has written a fixture for yet.  Callers turn foreign keys off, so
    the dummy values need not resolve to anything.
    """
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names: list[str] = []
    values: list[object] = []
    for col in info:
        _cid, name, ctype, notnull, default, pk = col[0], col[1], col[2], col[3], col[4], col[5]
        if name in overrides:
            names.append(name)
            values.append(overrides[name])
            continue
        if pk and "INT" in (ctype or "").upper():
            continue  # let SQLite assign the rowid
        if not notnull or default is not None:
            continue
        names.append(name)
        numeric = any(t in (ctype or "").upper() for t in ("INT", "REAL", "NUM"))
        values.append(0 if numeric else "probe")
    cols = ", ".join(names)
    marks = ", ".join("?" * len(names))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values)  # noqa: S608


class TestVerifyIntegrity:
    def test_a_consistent_index_passes(self, populated_db):
        conn = populated_db
        fid = upsert_file(conn, _HASH, "src/a.c", "c")
        _insert_probe_row(conn, "symbols", {
            "config_hash": _HASH, "file_id": fid, "usr": "U_a", "name": "a",
        })
        _step_verify_integrity(conn, _ctx())  # must not raise

    def test_a_foreign_key_violation_fails_the_run(self, populated_db):
        """A row pointing at a parent that is gone.

        This is what a cleanup path that missed a table looks like from the
        outside, which is the defect class the check exists for.
        """
        conn = populated_db
        conn.execute("PRAGMA foreign_keys = OFF")
        _insert_probe_row(conn, "symbols", {
            "config_hash": _HASH,
            "file_id": 999999,  # no such files row
            "usr": "U_orphan",
            "name": "orphan",
        })
        conn.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(IndexIntegrityError) as exc:
            _step_verify_integrity(conn, _ctx())
        assert "foreign key violation" in str(exc.value)

    def test_a_dangling_fts_entry_fails_the_run(self, populated_db):
        """A deleted symbol that search can still find.

        The ad trigger is dropped so the delete leaves the FTS index behind —
        the state a cleanup path that bypasses the trigger would produce.
        This case is also why the check passes ``rank = 1``: without it the
        integrity-check command reports "ok" here.
        """
        conn = populated_db
        fid = upsert_file(conn, _HASH, "src/a.c", "c")
        _insert_probe_row(conn, "symbols", {
            "config_hash": _HASH, "file_id": fid,
            "usr": "U_ghost", "name": "ghost_symbol",
        })
        assert conn.execute(
            "SELECT COUNT(*) FROM symbols_fts WHERE symbols_fts MATCH 'ghost_symbol'"
        ).fetchone()[0] == 1, "setup failed — the symbol never reached the FTS index"

        conn.execute("DROP TRIGGER symbols_ad")
        conn.execute("DELETE FROM symbols WHERE usr = 'U_ghost'")
        assert conn.execute(
            "SELECT COUNT(*) FROM symbols_fts WHERE symbols_fts MATCH 'ghost_symbol'"
        ).fetchone()[0] == 1, "setup failed — the entry was not left dangling"

        with pytest.raises(IndexIntegrityError) as exc:
            _step_verify_integrity(conn, _ctx())
        assert "symbols_fts" in str(exc.value)

    def test_a_deferred_fts_rebuild_is_not_read_as_a_broken_index(
        self, populated_db
    ):
        """A multi-build run rebuilds FTS once, after every build.

        The CLI passes defer_fts=True per (variant, image) and rebuilds at the
        end, so during each build the FTS index is legitimately behind its
        content table.  Checking it there condemned every build of
        zbox-ecb-fw-v5: verification failed, finalize_manifest never ran, and
        both variants stayed stamped "indexing" — hidden from readers.  The
        caller that owns the deferral runs the check after its rebuild.
        """
        conn = populated_db
        fid = upsert_file(conn, _HASH, "src/a.c", "c")
        _insert_probe_row(conn, "symbols", {
            "config_hash": _HASH, "file_id": fid,
            "usr": "U_ghost", "name": "ghost_symbol",
        })
        conn.execute("DROP TRIGGER symbols_ad")
        conn.execute("DELETE FROM symbols WHERE usr = 'U_ghost'")

        # Same state as the test above, which fails without the deferral.
        with pytest.raises(IndexIntegrityError):
            _step_verify_integrity(conn, _ctx())

        _step_verify_integrity(conn, _ctx(defer_fts=True))

    def test_a_deferred_run_still_checks_foreign_keys(self, populated_db):
        """Only the FTS half is deferred — the rest of the check still runs."""
        conn = populated_db
        conn.execute("PRAGMA foreign_keys = OFF")
        _insert_probe_row(conn, "symbols", {
            "config_hash": _HASH, "file_id": 999999, "usr": "U_orphan",
            "name": "orphan_symbol",
        })
        conn.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(IndexIntegrityError) as exc:
            _step_verify_integrity(conn, _ctx(defer_fts=True))
        assert "foreign key" in str(exc.value)

    def test_the_error_is_not_swallowed_as_a_failed_step(self):
        """IndexIntegrityError must stay outside SAFE_EXCEPT.

        The post-process loop catches SAFE_EXCEPT and downgrades it to a log
        line.  If this exception ever became a member of that tuple, a corrupt
        index would be reported as a successful run.
        """
        from fw_context_mcp.utils import SAFE_EXCEPT

        assert not issubclass(IndexIntegrityError, SAFE_EXCEPT)

    def test_it_runs_before_the_build_is_stamped_complete(self):
        """Order is the whole reason a failure protects the reader.

        Raising before ``finalize_manifest`` leaves a new config_hash marked
        'indexing', which ``get_active_config`` hides — so readers stay on the
        last complete build.  Raising before ``cleanup_old`` leaves that build
        in place to fall back to.
        """
        from fw_context_mcp.indexer._postprocess import _STEPS

        order = [name for name, _fn, _guard in _STEPS]
        assert order.index("verify_integrity") < order.index("finalize_manifest")
        assert order.index("verify_integrity") < order.index("cleanup_old")


class TestCleanupCoverage:
    """Every table must be reached by the cleanup that owns it.

    Enumerated from the schema, so adding a table without wiring it up fails
    here instead of silently leaving rows behind for the life of an index.
    """

    def test_delete_build_data_covers_every_build_scoped_table(self, populated_db):
        conn = populated_db
        tables = [t for t, _ in _tables_with_any_column(conn, {"config_hash"})]
        assert {"symbols", "refs", "macros"} <= set(tables), (
            f"schema introspection looks wrong: {tables}"
        )

        conn.execute("PRAGMA foreign_keys = OFF")
        for table in tables:
            _insert_probe_row(conn, table, {"config_hash": _PROBE_HASH})
        missing = [
            t for t in tables
            if conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE config_hash = ?", (_PROBE_HASH,)  # noqa: S608
            ).fetchone()[0] == 0
        ]
        assert missing == [], f"probe rows were not inserted into {missing}"

        delete_build_data(conn, _PROBE_HASH)

        leftovers = {
            t: n for t in tables
            if (n := conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE config_hash = ?", (_PROBE_HASH,)  # noqa: S608
            ).fetchone()[0])
        }
        assert leftovers == {}, (
            f"delete_build_data does not cover these tables: {leftovers}"
        )

    def test_owned_row_delete_covers_every_file_scoped_table(self, populated_db):
        """Tables keyed on a file must be cleared when that file is re-parsed.

        ``files`` itself is excluded: the re-parse case keeps its row on
        purpose.  ``inheritance`` has no file column — it is keyed on
        ``derived_usr`` and resolved through ``symbols`` — so this test cannot
        reach it; ``test_stale_branch_fix`` covers that path.
        """
        conn = populated_db
        targets = [
            (t, c) for t, c in _tables_with_any_column(conn, {"file_id", "from_file"})
            if t != "files"
        ]
        assert {t for t, _ in targets} >= {
            "symbols", "macros", "refs", "indirect_call_sites", "fp_assignments",
        }, f"schema introspection looks wrong: {targets}"

        fid = upsert_file(conn, _HASH, "src/owned.c", "c")
        conn.execute("PRAGMA foreign_keys = OFF")
        for table, column in targets:
            value = fid if column == "file_id" else "src/owned.c"
            _insert_probe_row(conn, table, {"config_hash": _HASH, column: value})

        _delete_rows_owned_by(conn, _HASH, [(fid, "src/owned.c")])

        leftovers: dict[str, int] = {}
        for table, column in targets:
            value = fid if column == "file_id" else "src/owned.c"
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)  # noqa: S608
            ).fetchone()[0]
            if n:
                leftovers[table] = n
        assert leftovers == {}, (
            f"_delete_rows_owned_by does not cover these tables: {leftovers}"
        )

    def test_the_files_row_survives_a_re_parse(self, populated_db):
        """The re-parse primitive must not delete the file record itself."""
        conn = populated_db
        fid = upsert_file(conn, _HASH, "src/keep.c", "c")
        _delete_rows_owned_by(conn, _HASH, [(fid, "src/keep.c")])
        assert conn.execute(
            "SELECT COUNT(*) FROM files WHERE id = ?", (fid,)
        ).fetchone()[0] == 1
