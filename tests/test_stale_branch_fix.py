"""Tests for stale-branch ghost-record purge (plan: index_stale_branch_fix).

WP5 test suite — covers purge_file_records, delete_overrides_for_file,
_dangling_incoming_refs, staleness checks, the postprocess purge step,
the safety guard, and end-to-end integration.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from fw_context_mcp.indexer.db import (
    delete_overrides_for_file,
    insert_fp_assignments_batch,
    insert_indirect_call_sites_batch,
    insert_inheritance_batch,
    insert_overrides_batch,
    insert_refs_batch,
    insert_symbols_batch,
    open_db,
    purge_file_records,
    purge_missing_files_batch,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

# ── schemaless helpers ───────────────────────────────────────────────


def _shell_db(tmp_path: Path) -> sqlite3.Connection:
    """Open an in‑memory SQLite database with the full schema."""
    db = tmp_path / "test.db"
    conn = open_db(db, skip_integrity_check=True)
    project_id = "deadbeef" * 4
    upsert_project(conn, project_id, "test", str(tmp_path))
    upsert_build_config(
        conn, "CCCC" * 8, project_id, str(tmp_path / "cc.json")
    )
    return conn


def _make_file(conn, config_hash: str, path: str, file_id: int | None = None) -> int:
    """Insert a files row and return its id."""
    if file_id is not None:
        # Bypass upsert — use explicit id for controlled tests
        conn.execute(
            """INSERT INTO files(id, config_hash, path, language, generated, mtime)
               VALUES (?,?,?,'c',0,1000.0)""",
            (file_id, config_hash, path),
        )
        return file_id
    return upsert_file(conn, config_hash, path, "c", mtime=1000.0)


def _symbol_usr(name: str) -> str:
    return f"c:@F@{name}"


# ── WP1 unit tests ───────────────────────────────────────────────────


class TestPurgeFileRecords:
    """Unit tests for purge_file_records — WP1 core function."""

    def test_purge_removes_all_table_rows(self, tmp_path: Path):
        """Symbols, macros, inheritance, overrides, refs, indirect_call_sites,
        fp_assignments, and files rows are gone after purge."""
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        f1 = _make_file(conn, ch, "src/a.c", file_id=1)
        f2 = _make_file(conn, ch, "src/b.c", file_id=2)  # untouched

        # Insert symbols
        usr1 = _symbol_usr("func_a")
        usr2 = _symbol_usr("func_b")
        insert_symbols_batch(
            conn,
            [
                (ch, f1, "src/a.c", "func a", usr1, "func_a", "func_a",
                 "function", 1, 1, 5, 1, "void func_a(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
                (ch, f2, "src/b.c", "func b", usr2, "func_b", "func_b",
                 "function", 1, 1, 5, 1, "void func_b(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
            ],
        )

        # Insert macro for f1
        conn.execute(
            "INSERT INTO macros(config_hash, file_id, name, value, expanded_value, line, is_function_like) "
            "VALUES (?,?,?,?,?,?,0)",
            (ch, f1, "MY_MACRO", "1", "1", 1),
        )

        # Insert inheritance edge (f1 → f2)
        class_usr1 = "c:@S@A"
        class_usr2 = "c:@S@B"
        insert_symbols_batch(
            conn,
            [
                (ch, f1, "src/a.c", "class a", class_usr1, "A", "A",
                 "class", 1, 1, 5, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, "", 0),
                (ch, f2, "src/b.c", "class b", class_usr2, "B", "B",
                 "class", 1, 1, 5, 1, "", "", None, 0, 0, "", 0, "", 0, 0.0, "", 0),
            ],
        )
        insert_inheritance_batch(conn, [(ch, class_usr2, class_usr1, "public", 0)])

        # Insert override edge — func_b overrides func_a
        insert_overrides_batch(conn, [(ch, usr2, usr1)])

        # Insert refs
        insert_refs_batch(
            conn,
            [(ch, usr2, "src/a.c", 3, usr1, "call", None)],
        )

        # Insert indirect call site
        insert_indirect_call_sites_batch(
            conn,
            [(ch, "src/a.c", 4, usr1, "fp()", usr2, "func_b", "void(*)()")],
        )

        # Insert fp_assignments
        insert_fp_assignments_batch(
            conn,
            [(ch, "src/a.c", 5, "c:@F@field", "field", usr2, "func_b", "void(*)()", "assignment", usr1)],
        )

        conn.commit()

        # ── Purge f1 ──
        removed = purge_file_records(conn, ch, f1, "src/a.c", db_dir=tmp_path)
        assert removed == 2  # func_a + A

        # symbols from f1 gone, f2 remains
        syms = {r["name"] for r in conn.execute("SELECT name FROM symbols").fetchall()}
        assert "func_a" not in syms
        assert "A" not in syms
        assert "func_b" in syms
        assert "B" in syms

        # macros gone
        assert conn.execute("SELECT COUNT(*) FROM macros WHERE file_id=?", (f1,)).fetchone()[0] == 0

        # inheritance gone
        inh = conn.execute("SELECT COUNT(*) FROM inheritance").fetchone()[0]
        assert inh == 0

        # overrides gone
        ovr = conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0]
        assert ovr == 0

        # refs gone (both from-file and to-usr)
        refs = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        assert refs == 0

        # indirect_call_sites gone (dangling incoming ref cleanup)
        ics = conn.execute("SELECT COUNT(*) FROM indirect_call_sites").fetchone()[0]
        assert ics == 0

        # fp_assignments gone
        fpa = conn.execute("SELECT COUNT(*) FROM fp_assignments").fetchone()[0]
        assert fpa == 0

        # files row gone
        files_left = {r["id"] for r in conn.execute("SELECT id FROM files").fetchall()}
        assert f1 not in files_left

        # FK cascade: embeddings/llm_analysis/hotspot_cache empty (should be anyway here)
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM hotspot_cache").fetchone()[0] == 0

        conn.close()

    def test_purge_keeps_other_files_untouched(self, tmp_path: Path):
        """Rows of other files survive — WP5 test 4."""
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        f1 = _make_file(conn, ch, "src/a.c", file_id=1)
        f2 = _make_file(conn, ch, "src/b.c", file_id=2)

        usr1 = _symbol_usr("func_a")
        usr2 = _symbol_usr("func_b")
        insert_symbols_batch(
            conn,
            [
                (ch, f1, "src/a.c", "func a", usr1, "func_a", "func_a",
                 "function", 1, 1, 5, 1, "void func_a(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
                (ch, f2, "src/b.c", "func b", usr2, "func_b", "func_b",
                 "function", 1, 1, 5, 1, "void func_b(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
            ],
        )
        conn.commit()

        removed = purge_file_records(conn, ch, f1, "src/a.c", db_dir=tmp_path)
        assert removed == 1

        syms = {r["name"] for r in conn.execute("SELECT name FROM symbols").fetchall()}
        assert "func_a" not in syms
        assert "func_b" in syms

        files_left = {r["id"] for r in conn.execute("SELECT id FROM files").fetchall()}
        assert f1 not in files_left
        assert f2 in files_left

        conn.close()


class TestDeleteDanglingIncomingRefs:
    """WP5 test 2 — _delete_dangling_incoming_refs closes the incoming-refs gap."""

    def test_incoming_refs_cleaned(self, tmp_path: Path):
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        f_surv = _make_file(conn, ch, "src/surviving.c", file_id=1)
        f_dead = _make_file(conn, ch, "src/dead.c", file_id=2)

        usr_surv = "c:@F@handler"
        usr_dead1 = "c:@F@dead1"
        usr_dead2 = "c:@F@dead2"

        insert_symbols_batch(
            conn,
            [
                (ch, f_surv, "src/surviving.c", "handler", usr_surv, "handler", "handler",
                 "function", 1, 1, 5, 1, "void handler(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
                (ch, f_dead, "src/dead.c", "dead1", usr_dead1, "dead1", "dead1",
                 "function", 1, 1, 5, 1, "void dead1(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
                (ch, f_dead, "src/dead.c", "dead2", usr_dead2, "dead2", "dead2",
                 "function", 1, 1, 5, 1, "void dead2(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
                # Unrelated symbol to confirm it survives
                (ch, f_surv, "src/surviving.c", "unrelated", "c:@F@unrelated", "unrelated", "unrelated",
                 "function", 10, 1, 15, 1, "void unrelated(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
            ],
        )

        # Insert edges from surviving file → dead symbols
        insert_refs_batch(conn, [(ch, usr_dead1, "src/surviving.c", 1, usr_surv, "call", None)])
        insert_refs_batch(conn, [(ch, "c:@F@unrelated", "src/surviving.c", 11, usr_surv, "call", None)])
        insert_indirect_call_sites_batch(
            conn, [(ch, "src/surviving.c", 2, usr_surv, "fp()", usr_dead2, "dead2", "void(*)()")]
        )
        insert_fp_assignments_batch(
            conn, [(ch, "src/surviving.c", 3, usr_dead1, "dead1_field", usr_dead2, "dead2", "void(*)()", "assignment", usr_surv)]
        )
        conn.commit()

        # Purge the dead file
        from fw_context_mcp.indexer.db._files import _delete_dangling_incoming_refs

        _delete_dangling_incoming_refs(conn, ch, [usr_dead1, usr_dead2])

        # Incoming refs to dead symbols gone
        refs = conn.execute("SELECT to_usr FROM refs").fetchall()
        ref_usrs = {r["to_usr"] for r in refs}
        assert usr_dead1 not in ref_usrs
        assert "c:@F@unrelated" in ref_usrs  # unrelated survives

        # indirect_call_sites to dead symbols gone
        ics = conn.execute("SELECT * FROM indirect_call_sites").fetchall()
        assert len(ics) == 0

        # fp_assignments referencing dead symbols gone
        fpa = conn.execute("SELECT * FROM fp_assignments").fetchall()
        assert len(fpa) == 0

        conn.close()


class TestDeleteOverridesForFile:
    """WP5 test 3 — delete_overrides_for_file works."""

    def test_delete_overrides_for_file(self, tmp_path: Path):
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        f1 = _make_file(conn, ch, "src/a.c", file_id=1)
        f2 = _make_file(conn, ch, "src/b.c", file_id=2)

        usr_a = _symbol_usr("method_a")
        usr_b = _symbol_usr("method_b")

        insert_symbols_batch(
            conn,
            [
                (ch, f1, "src/a.c", "method a", usr_a, "method_a", "Base::method_a",
                 "method", 1, 1, 3, 1, "void method_a(void)", "", None,
                 1, 0, "c:@S@Base", 0, "", 0, 0.0, "", 0),
                (ch, f2, "src/b.c", "method b", usr_b, "method_b", "Derived::method_b",
                 "method", 1, 1, 3, 1, "void method_a(void)", "", None,
                 1, 0, "c:@S@Derived", 0, "", 0, 0.0, "", 0),
            ],
        )
        insert_overrides_batch(conn, [(ch, usr_b, usr_a)])
        conn.commit()

        # usr_b (derived) is in f2 → deleting for f2 removes the edge
        delete_overrides_for_file(conn, ch, f2)
        assert conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 0

        conn.close()

    def test_other_file_overrides_untouched(self, tmp_path: Path):
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        f1 = _make_file(conn, ch, "src/a.c", file_id=1)
        f2 = _make_file(conn, ch, "src/b.c", file_id=2)

        usr_a = _symbol_usr("meth_a")
        usr_b = _symbol_usr("meth_b")

        insert_symbols_batch(
            conn,
            [
                (ch, f1, "src/a.c", "meth a", usr_a, "meth_a", "A::meth_a",
                 "method", 1, 1, 3, 1, "void meth_a(void)", "", None,
                 1, 0, "c:@S@A", 0, "", 0, 0.0, "", 0),
                (ch, f2, "src/b.c", "meth b", usr_b, "meth_b", "B::meth_b",
                 "method", 1, 1, 3, 1, "void meth_a(void)", "", None,
                 1, 0, "c:@S@B", 0, "", 0, 0.0, "", 0),
            ],
        )
        insert_overrides_batch(conn, [(ch, usr_b, usr_a)])  # derived=usr_b (in f2), base=usr_a (in f1)
        conn.commit()

        # Delete overrides for f1 — derived is in f2, so edge stays
        delete_overrides_for_file(conn, ch, f1)
        assert conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 1

        # Delete overrides for f2 — derived is in f2, edge removed
        delete_overrides_for_file(conn, ch, f2)
        assert conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 0

        conn.close()


class TestPurgeMissingFilesBatch:
    """Batched variant — one lock/txn for multiple files."""

    def test_batch_purge(self, tmp_path: Path):
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        f1 = _make_file(conn, ch, "src/a.c", file_id=1)
        f2 = _make_file(conn, ch, "src/b.c", file_id=2)

        usr1 = _symbol_usr("func_a")
        usr2 = _symbol_usr("func_b")
        insert_symbols_batch(
            conn,
            [
                (ch, f1, "src/a.c", "func a", usr1, "func_a", "func_a",
                 "function", 1, 1, 5, 1, "void func_a(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
                (ch, f2, "src/b.c", "func b", usr2, "func_b", "func_b",
                 "function", 1, 1, 5, 1, "void func_b(void)", "", None,
                 0, 0, "", 0, "", 0, 0.0, "", 0),
            ],
        )
        conn.commit()

        removed = purge_missing_files_batch(
            conn, ch, [(f1, "src/a.c"), (f2, "src/b.c")], db_dir=tmp_path
        )
        assert removed == 2
        assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0

        conn.close()


# ── WP4 staleness tests ───────────────────────────────────────────────


class TestStalenessCheck:
    """WP5 test 5 — staleness checks treat missing files as stale."""

    def test_file_differs_missing_file(self):
        from fw_context_mcp.mcp.shared.stale import _file_differs

        result = _file_differs("/nonexistent/path_12345.c", 100.0)
        assert result is True, "Missing file must be reported as stale"

    def test_count_modified_files_counts_missing(self, tmp_path: Path):
        from fw_context_mcp.mcp.shared.stale import _count_modified_files

        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        # Insert a file record pointing to a path that doesn't exist
        conn.execute(
            "INSERT INTO files(config_hash, path, language, generated, mtime) "
            "VALUES (?,'/nonexistent/test_missing.c','c',0,100.0)",
            (ch,),
        )
        conn.commit()

        count = _count_modified_files(conn, ch, tmp_path)
        # The missing file should be counted as modified
        assert count >= 1, f"Expected at least 1 modified file (missing), got {count}"

        conn.close()


# ── WP2 integration test ──────────────────────────────────────────────


class TestStepPurgeMissingFiles:
    """WP5 test 6 — integration: delete file, same config_hash, run indexer, symbols gone."""

    def test_delete_file_ghost_records_purged(self, tmp_path: Path):
        """Index a project, delete a source file from disk (cc.json unchanged),
        reindex — ghost symbols must be gone."""

        proj = tmp_path / "demo"
        proj.mkdir()
        src = proj / "src"
        src.mkdir()

        # Write source files
        (src / "main.c").write_text("""\
#include "modem.h"
int main(void) { modem_init(115200); return 0; }
""", encoding="utf-8")
        (src / "modem.h").write_text("""\
#ifndef MODEM_H
#define MODEM_H
void modem_init(int baudrate);
#endif
""", encoding="utf-8")
        (src / "modem.c").write_text("""\
#include "modem.h"
void modem_init(int baudrate) { (void)baudrate; }
""", encoding="utf-8")
        (src / "extra.c").write_text("""\
int extra_func(void) { return 42; }
""", encoding="utf-8")

        cc = [
            {"directory": str(src), "file": "main.c",
             "arguments": ["gcc", "-std=c11", "-Isrc", "-c", "main.c", "-o", "build/main.o"]},
            {"directory": str(src), "file": "modem.c",
             "arguments": ["gcc", "-std=c11", "-Isrc", "-c", "modem.c", "-o", "build/modem.o"]},
            {"directory": str(src), "file": "extra.c",
             "arguments": ["gcc", "-std=c11", "-Isrc", "-c", "extra.c", "-o", "build/extra.o"]},
        ]
        cc_json = proj / "compile_commands.json"
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        # Init + index
        from fw_context_mcp.config import derive_project_id
        from fw_context_mcp.config import load as load_config

        _isolate_index_db(proj, proj.parent)
        from .test_incremental_reindex import _cli

        init_result = _cli(["init", "--project", str(proj)], cwd=proj, timeout=60)
        assert init_result.returncode == 0, f"Init failed: {init_result.stderr}"

        result = _cli(["index", "--no-refs", "--no-analyze", "--no-embeddings", str(cc_json)],
                      cwd=proj, timeout=120)
        assert result.returncode == 0, f"Index failed: {result.stderr}"

        cfg = load_config(project_root=proj)
        db_path = cfg.index.db_dir / derive_project_id(proj) / "index.db"
        assert db_path.exists()

        # Verify initial state
        conn = open_db(db_path)
        try:
            ch = conn.execute(
                "SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()["config_hash"]
            syms = {r["name"] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND is_definition=1", (ch,)
            ).fetchall()}
            assert "extra_func" in syms, f"extra_func not found: {syms}"
            assert "modem_init" in syms
            assert "main" in syms
        finally:
            conn.close()

        # Delete extra.c from disk (cc.json unchanged → same config_hash)
        (src / "extra.c").unlink()

        # Reindex
        from fw_context_mcp.indexer.runner import run

        run(
            compile_commands=cc_json,
            db_path=db_path,
            project_root=proj,
            vendor_paths=[],
            project_paths=[],
            index_refs=False,
            index_embeddings=False,
            analyze_symbols=False,
            analyze_overrides=False,
            purge_max_missing_percent=100,
        )

        # Verify ghost records gone
        conn = open_db(db_path)
        try:
            syms = {r["name"] for r in conn.execute(
                "SELECT name FROM symbols WHERE is_definition=1"
            ).fetchall()}
            assert "extra_func" not in syms, f"Ghost symbol extra_func still present: {syms}"
            assert "modem_init" in syms
            assert "main" in syms

            # files table clean
            files = {r["path"] for r in conn.execute("SELECT path FROM files").fetchall()}
            assert "extra.c" not in files
        finally:
            conn.close()

        shutil.rmtree(db_path.parent, ignore_errors=True)


# ── WP3 guard tests ───────────────────────────────────────────────────


class TestSafetyGuard:
    """WP5 tests 8 & 9 — safety guard aborts/permits the purge."""

    def test_guard_aborts_when_too_many_missing(self, tmp_path: Path):
        """Delete >20% of files → purge aborts, index unchanged."""
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        # Insert 10 files — all paths that don't exist on disk
        for i in range(10):
            _make_file(conn, ch, f"src/file{i}.c", file_id=i + 1)
        conn.commit()

        from fw_context_mcp.indexer._postprocess import _step_purge_missing_files

        ctx = {
            "config_hash": ch,
            "project_root": tmp_path,
            "db_dir": tmp_path,
            "build_dir_patterns": [],
            "purge_max_missing_percent": 20,
        }
        _step_purge_missing_files(conn, ctx)

        # All files should still be present (purge aborted)
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert count == 10, f"Expected 10 files, got {count}"

        conn.close()

    def test_guard_permits_when_configured_high(self, tmp_path: Path):
        """Configure purge_max_missing_percent=100 → purge runs."""
        conn = _shell_db(tmp_path)
        ch = "CCCC" * 8

        for i in range(5):
            _make_file(conn, ch, f"src/file{i}.c", file_id=i + 1)
        conn.commit()

        from fw_context_mcp.indexer._postprocess import _step_purge_missing_files

        ctx = {
            "config_hash": ch,
            "project_root": tmp_path,
            "db_dir": tmp_path,
            "build_dir_patterns": [],
            "purge_max_missing_percent": 100,
        }
        _step_purge_missing_files(conn, ctx)

        # All files should be purged (they don't exist on disk)
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert count == 0, f"Expected 0 files after purge, got {count}"

        conn.close()


# ── helper ────────────────────────────────────────────────────────────


def _isolate_index_db(project_root: Path, tmp_dir: Path) -> None:
    """Write local.toml to redirect db_dir into tmp_dir for test isolation."""
    local_config_dir = project_root / ".fw-context"
    local_config_dir.mkdir(parents=True, exist_ok=True)
    local_toml = local_config_dir / "local.toml"
    index_dir = tmp_dir / "index"
    local_toml.write_text(
        f'[index]\ndb_dir = "{index_dir}"\n',
        encoding="utf-8",
    )
