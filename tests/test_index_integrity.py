"""Index integrity tests — verify indexed data matches source files.

Requires libclang and a C compiler (gcc). Uses a real C project fixture
indexed via the CLI, then reads the DB and compares against source files.

Fixtures are class-scoped to avoid repeated indexing.

NOTE: Tests use ``~/.fw-context/index/<project_id>/index.db`` (global
index directory from ``IndexConfig.db_dir``).  The index is NOT stored
in the project's local ``.fw-context/`` directory.  ``readiness.py`` cache
is per-project (keyed by root path) — multiple test projects coexist safely.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import open_db

pytestmark = pytest.mark.libclang

# ── Helpers ────────────────────────────────────────────────────────────────


def _db_path_for_project(project_root: Path) -> Path:
    from fw_context_mcp.config import derive_project_id
    from fw_context_mcp.config import load as load_config

    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _cleanup_index_db(project_root: Path) -> None:
    """Delete the index database directory for a project."""
    db_path = _db_path_for_project(project_root)
    shutil.rmtree(db_path.parent, ignore_errors=True)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _cli(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(sys.modules["os"].environ)
    env["PYTHONPATH"] = str(_project_root() / "src")
    return subprocess.run(
        [sys.executable, "-m", "fw_context_mcp.cli"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _config_hash(conn):
    return conn.execute(
        "SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()["config_hash"]


# ── Class-scoped fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="class")
def c_project_extended(tmp_path_factory):
    """Create a C project with structs, enums, and complex types (once per class)."""
    proj = tmp_path_factory.mktemp("proj")

    src = proj / "src"
    src.mkdir()

    _write_file(src / "main.c", """\
#include <stdio.h>
#include "driver.h"
#include "types.h"

int main(void) {
    struct Config cfg = {
        .baudrate = 115200,
        .mode = MODE_ACTIVE,
        .name = "demo",
    };
    driver_init(&cfg);
    int result = driver_send("hello", 5);
    printf("result=%d\\n", result);
    return 0;
}
""")

    _write_file(src / "types.h", """\
#ifndef TYPES_H
#define TYPES_H

enum OperationMode {
    MODE_IDLE = 0,
    MODE_ACTIVE = 1,
    MODE_SLEEP = 2,
    MODE_MAX = 255,
};

struct Config {
    int baudrate;
    int mode;
    const char* name;
};

typedef struct {
    int status;
    int length;
} Response;

#endif
""")

    _write_file(src / "driver.h", """\
#ifndef DRIVER_H
#define DRIVER_H

#include "types.h"

int driver_init(const struct Config* cfg);
int driver_send(const char* data, int len);
int driver_recv(char* buf, int max_len);
Response driver_get_status(void);

#endif
""")

    _write_file(src / "driver.c", """\
#include "driver.h"
#include <string.h>

static int g_initialized = 0;

int driver_init(const struct Config* cfg) {
    if (!cfg) return -1;
    g_initialized = 1;
    return 0;
}

int driver_send(const char* data, int len) {
    if (!g_initialized || !data || len <= 0) return -1;
    return len;
}

int driver_recv(char* buf, int max_len) {
    if (!g_initialized || !buf || max_len <= 0) return -1;
    memset(buf, 0, max_len);
    return 0;
}

Response driver_get_status(void) {
    Response r = { .status = g_initialized ? 1 : 0, .length = 0 };
    return r;
}
""")

    cc = [
        {"directory": str(src), "file": "main.c",
         "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "main.c", "-o", "build/main.o"]},
        {"directory": str(src), "file": "driver.c",
         "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "driver.c", "-o", "build/driver.o"]},
    ]
    cc_json = proj / "compile_commands.json"
    cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

    # Initialize project to generate UUID4 project ID
    init_result = _cli(
        ["init", "--project", str(proj)],
        cwd=proj,
        timeout=60,
    )
    if init_result.returncode != 0:
        pytest.fail(f"Init failed:\nSTDOUT:\n{init_result.stdout}\nSTDERR:\n{init_result.stderr}")

    return proj


@pytest.fixture(scope="class")
def indexed_extended(c_project_extended: Path):
    """Index the extended C project (once per class) and return the project root."""
    # Initialize project first — generates UUID4 project ID
    init_result = _cli(
        ["init", "--project", str(c_project_extended)],
        cwd=c_project_extended,
        timeout=60,
    )
    if init_result.returncode != 0:
        pytest.fail(f"Init failed:\nSTDOUT:\n{init_result.stdout}\nSTDERR:\n{init_result.stderr}")

    cc_json = c_project_extended / "compile_commands.json"
    result = _cli(
        ["index", "--no-refs", "--no-analyze", "--no-embeddings", str(cc_json)],
        cwd=c_project_extended,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.fail(f"Index failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    db_path = _db_path_for_project(c_project_extended)
    assert db_path.exists(), f"DB not created at {db_path}"
    yield c_project_extended
    _cleanup_index_db(c_project_extended)


# ── Tests: Symbol counts ───────────────────────────────────────────────────


class TestIndexedSymbolCounts:
    """Verify indexed symbol counts match expected declarations in source files."""

    def test_project_and_config_exist(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert ch is not None
            proj = conn.execute("SELECT name, root_path FROM projects LIMIT 1").fetchone()
            assert proj is not None
            assert proj["name"] is not None
        finally:
            conn.close()

    def test_files_table_has_entries(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            files = conn.execute(
                "SELECT path FROM files WHERE config_hash=? AND path LIKE '%.c'",
                (ch,),
            ).fetchall()
            assert len(files) >= 2
        finally:
            conn.close()

    def test_function_definitions_indexed(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            functions = conn.execute(
                """SELECT name FROM symbols
                   WHERE config_hash=? AND kind='function' AND is_definition=1""",
                (ch,),
            ).fetchall()
            fn_names = {r["name"] for r in functions}
            assert "main" in fn_names
            assert "driver_init" in fn_names
            assert "driver_send" in fn_names
            assert "driver_recv" in fn_names
            assert "driver_get_status" in fn_names
        finally:
            conn.close()

    def test_header_declarations_indexed(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            decls = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='function' AND is_definition=0",
                (ch,),
            ).fetchall()
            assert isinstance(decls, list)
        finally:
            conn.close()


# ── Tests: Symbol kinds ────────────────────────────────────────────────────


class TestSymbolKindsCorrect:
    """Verify that each symbol has the correct kind."""

    def test_enum_kind(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            enums = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='enum'",
                (ch,),
            ).fetchall()
            enum_names = {r["name"] for r in enums}
            assert "OperationMode" in enum_names
        finally:
            conn.close()

    def test_enum_constant_values(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            constants = conn.execute(
                """SELECT name, enum_value FROM symbols
                   WHERE config_hash=? AND kind='enum_constant'
                   ORDER BY name""",
                (ch,),
            ).fetchall()
            const_dict = {r["name"]: r["enum_value"] for r in constants}
            assert const_dict.get("MODE_IDLE") == 0
            assert const_dict.get("MODE_ACTIVE") == 1
            assert const_dict.get("MODE_SLEEP") == 2
            assert const_dict.get("MODE_MAX") == 255
        finally:
            conn.close()

    def test_struct_kind(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            structs = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='struct'",
                (ch,),
            ).fetchall()
            struct_names = {r["name"] for r in structs}
            assert "Config" in struct_names
            assert "Response" in struct_names
        finally:
            conn.close()

    def test_struct_fields_indexed(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            fields = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND kind='field'",
                (ch,),
            ).fetchall()
            field_names = {r["name"] for r in fields}
            assert "baudrate" in field_names
            assert "mode" in field_names
            assert "name" in field_names or "status" in field_names
        finally:
            conn.close()

    def test_no_wrong_kinds(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            row = conn.execute(
                "SELECT kind FROM symbols WHERE config_hash=? AND name='driver_init'",
                (ch,),
            ).fetchone()
            assert row is not None
            assert row["kind"] == "function"
        finally:
            conn.close()


# ── Tests: Data consistency ────────────────────────────────────────────────


class TestDataSourceConsistency:
    """Verify indexed data consistency against source file content."""

    def test_line_numbers_positive(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            rows = conn.execute(
                "SELECT name, line FROM symbols WHERE config_hash=? AND is_definition=1",
                (ch,),
            ).fetchall()
            for r in rows:
                assert r["line"] > 0, f"{r['name']} has non-positive line {r['line']}"
        finally:
            conn.close()

    def test_signatures_not_empty_for_functions(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            rows = conn.execute(
                """SELECT name, signature FROM symbols
                   WHERE config_hash=? AND kind='function' AND is_definition=1""",
                (ch,),
            ).fetchall()
            for r in rows:
                assert r["signature"] is not None, f"{r['name']} has null signature"
                assert len(r["signature"]) > 0, f"{r['name']} has empty signature"
        finally:
            conn.close()

    def test_config_hash_consistent(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash!=?", (ch,),
            ).fetchone()[0]
            assert count == 0, f"Found {count} symbols with different config_hash"
        finally:
            conn.close()

    def test_file_paths_not_empty(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND file_path=''",
                (ch,),
            ).fetchone()[0]
            assert count == 0, "Found symbols with empty file_path"
        finally:
            conn.close()


# ── Tests: Data integrity (no nulls, no orphans) ───────────────────────────


class TestDataIntegrity:
    """Verify data integrity — no nulls in required fields, no orphan records."""

    def test_no_null_names(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND name IS NULL",
                (ch,),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_no_null_usrs(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND usr IS NULL",
                (ch,),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_no_null_kinds(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND kind IS NULL",
                (ch,),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_no_null_file_paths(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND file_path IS NULL",
                (ch,),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_files_table_consistent_with_symbols(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            orphans = conn.execute(
                """SELECT DISTINCT s.file_id FROM symbols s
                   WHERE s.config_hash=?
                   AND s.file_id NOT IN (SELECT id FROM files)""",
                (ch,),
            ).fetchall()
            assert len(orphans) == 0, f"Found {len(orphans)} orphan file_ids"
        finally:
            conn.close()


# ── Tests: Reindex integrity (separate class because it mutates state) ─────


class TestReindexIntegrity:
    """Verify reindex preserves data integrity.

    Note: These tests modify source files and should run after read-only tests.
    """

    def test_reindex_no_duplicate_usrs(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

            driver_c = indexed_extended / "src" / "driver.c"
            result = reindex_file_impl(str(driver_c), str(indexed_extended))
            assert "error" not in result, f"Reindex failed: {result}"

            dupes = conn.execute(
                "SELECT usr, COUNT(*) as cnt FROM symbols WHERE config_hash=? GROUP BY usr HAVING cnt > 1",
                (ch,),
            ).fetchall()
            assert len(dupes) == 0, f"Found duplicate USRs: {[d['usr'] for d in dupes]}"
        finally:
            conn.close()

    def test_reindex_preserves_symbols(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,),
            ).fetchone()[0]

            from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
            main_c = indexed_extended / "src" / "main.c"
            result = reindex_file_impl(str(main_c), str(indexed_extended))
            assert "error" not in result, f"Reindex failed: {result}"

            after = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,),
            ).fetchone()[0]
            assert after >= before - 2, f"Lost symbols: {before} → {after}"
            assert after <= before + 2, f"Gained symbols: {before} → {after}"
        finally:
            conn.close()

    def test_reindex_updates_line_numbers_after_edit(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            old = conn.execute(
                "SELECT line FROM symbols WHERE config_hash=? AND name='driver_send' AND is_definition=1",
                (ch,),
            ).fetchone()
            assert old is not None

            driver_c = indexed_extended / "src" / "driver.c"
            original = driver_c.read_text(encoding="utf-8")
            modified = "// Added comment 1\n// Added comment 2\n// Added comment 3\n// Added comment 4\n// Added comment 5\n" + original
            driver_c.write_text(modified, encoding="utf-8")

            from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
            result = reindex_file_impl(str(driver_c), str(indexed_extended))
            assert "error" not in result, f"Reindex failed: {result}"

            new = conn.execute(
                "SELECT line FROM symbols WHERE config_hash=? AND name='driver_send' AND is_definition=1",
                (ch,),
            ).fetchone()
            assert new is not None
            assert new["line"] == old["line"] + 5, \
                f"Expected line {old['line'] + 5}, got {new['line']}"
        finally:
            conn.close()

    def test_reindex_after_adding_function(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? AND name='new_helper'",
                (ch,),
            ).fetchone()[0]
            assert before == 0

            driver_c = indexed_extended / "src" / "driver.c"
            original = driver_c.read_text(encoding="utf-8")
            modified = original + "\nint new_helper(int x) {\n    return x * 2;\n}\n"
            driver_c.write_text(modified, encoding="utf-8")

            from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
            result = reindex_file_impl(str(driver_c), str(indexed_extended))
            assert "error" not in result, f"Reindex failed: {result}"

            after = conn.execute(
                "SELECT name, kind, is_definition FROM symbols WHERE config_hash=? AND name='new_helper'",
                (ch,),
            ).fetchall()
            assert len(after) >= 1
            assert after[0]["kind"] == "function"
            assert after[0]["is_definition"] == 1
        finally:
            conn.close()

    def test_reindex_after_removing_function(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            # Count definitions specifically (not declarations from headers)
            before = conn.execute(
                """SELECT COUNT(*) FROM symbols
                   WHERE config_hash=? AND name='driver_get_status' AND is_definition=1""",
                (ch,),
            ).fetchone()[0]
            assert before >= 1

            driver_c = indexed_extended / "src" / "driver.c"
            original = driver_c.read_text(encoding="utf-8")

            # Remove the function definition — match exact text from fixture
            target = (
                "Response driver_get_status(void) {\n"
                "    Response r = { .status = g_initialized ? 1 : 0, .length = 0 };\n"
                "    return r;\n"
                "}\n"
            )
            if target in original:
                modified = original.replace(target, "")
            else:
                # Fallback: remove by line-based approach
                lines = original.splitlines(keepends=True)
                # Find the function definition start and end
                start_idx = None
                for i, line in enumerate(lines):
                    if "driver_get_status" in line and "Response" in line:
                        start_idx = i
                        break
                if start_idx is not None:
                    # Find the closing brace
                    depth = 0
                    end_idx = start_idx
                    for i in range(start_idx, len(lines)):
                        depth += lines[i].count("{") - lines[i].count("}")
                        if depth == 0 and i > start_idx:
                            end_idx = i + 1
                            break
                    modified = "".join(lines[:start_idx] + lines[end_idx:])
                else:
                    modified = original

            driver_c.write_text(modified, encoding="utf-8")

            from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
            result = reindex_file_impl(str(driver_c), str(indexed_extended))
            assert "error" not in result, f"Reindex failed: {result}"

            after = conn.execute(
                """SELECT COUNT(*) FROM symbols
                   WHERE config_hash=? AND name='driver_get_status' AND is_definition=1""",
                (ch,),
            ).fetchone()[0]
            assert after == 0, "Function definition still in index after removal"
        finally:
            conn.close()

    def test_idempotent_reindex(self, indexed_extended: Path):
        db_path = _db_path_for_project(indexed_extended)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,),
            ).fetchone()[0]

            from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
            main_c = indexed_extended / "src" / "main.c"

            r1 = reindex_file_impl(str(main_c), str(indexed_extended))
            assert "error" not in r1
            r2 = reindex_file_impl(str(main_c), str(indexed_extended))
            assert "error" not in r2

            after = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=?", (ch,),
            ).fetchone()[0]
            assert abs(after - before) <= 2, \
                f"Idempotent reindex changed count: {before} → {after}"
        finally:
            conn.close()


# ── Tests: Edge cases for reindex ─────────────────────────────────────────


class TestReindexEdgeCases:
    """Verify reindex handles edge cases gracefully."""

    def test_reindex_deleted_file(self, indexed_extended: Path):
        nonexistent = indexed_extended / "src" / "nonexistent.c"
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
        result = reindex_file_impl(str(nonexistent), str(indexed_extended))
        assert "error" in result

    def test_reindex_file_not_in_compile_commands(self, indexed_extended: Path):
        temp_file = indexed_extended / "src" / "unlisted.c"
        temp_file.write_text("int x;\n", encoding="utf-8")
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
        result = reindex_file_impl(str(temp_file), str(indexed_extended))
        assert "error" in result


# ── Tests: Edge cases for indexing ────────────────────────────────────────


class TestIndexingEdgeCases:
    """Verify full indexing handles edge cases — empty files, preprocessor-only files.

    Uses a separate project fixture since these tests modify compile_commands.json.
    """

    def test_empty_source_file_indexed(self, c_project_extended: Path):
        """Indexing an empty source file should not crash."""
        empty_file = c_project_extended / "src" / "empty.c"
        empty_file.write_text("", encoding="utf-8")

        cc_json = c_project_extended / "compile_commands.json"
        cc = json.loads(cc_json.read_text(encoding="utf-8"))
        cc.append({
            "directory": str(c_project_extended / "src"),
            "file": "empty.c",
            "arguments": ["gcc", "-std=c11", "-c", "empty.c", "-o", "build/empty.o"],
        })
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        try:
            result = _cli(["index", "--no-refs", "--no-analyze", "--no-embeddings", str(cc_json)],
                          cwd=c_project_extended, timeout=180)
            assert result.returncode == 0, f"Index with empty file failed:\n{result.stderr}"
        finally:
            _cleanup_index_db(c_project_extended)

    def test_file_with_only_preprocessor(self, c_project_extended: Path):
        """A source file with only preprocessor directives should not crash indexing."""
        pp_file = c_project_extended / "src" / "config_defs.c"
        pp_file.write_text(
            "#define FOO 1\n"
            "#define BAR 2\n"
            "#ifdef SOMETHING\n"
            "#undef OTHER\n"
            "#endif\n",
            encoding="utf-8",
        )

        cc_json = c_project_extended / "compile_commands.json"
        cc = json.loads(cc_json.read_text(encoding="utf-8"))
        cc.append({
            "directory": str(c_project_extended / "src"),
            "file": "config_defs.c",
             "arguments": ["gcc", "-std=c11", "-c", "config_defs.c", "-o", "build/config_defs.o"],
        })
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        try:
            result = _cli(["index", "--no-refs", "--no-analyze", "--no-embeddings", str(cc_json)],
                          cwd=c_project_extended, timeout=180)
            assert result.returncode == 0
        finally:
            _cleanup_index_db(c_project_extended)


# ── Anonymous struct / union fixtures and tests ────────────────────────────


@pytest.fixture(scope="class")
def c_project_anon(tmp_path_factory):
    """C project with anonymous structs and unions — tests field-name resolution."""
    proj = tmp_path_factory.mktemp("proj_anon")

    src = proj / "src"
    src.mkdir()

    _write_file(src / "main.c", """\
#include "data.h"

SensorData g_sensor;

int main(void) {
    init_sensor(&g_sensor);
    return g_sensor._payload.x + g_sensor._data.a;
}
""")

    _write_file(src / "data.h", """\
#ifndef DATA_H
#define DATA_H

typedef struct {
    struct {
        int x;
        int y;
    } _payload;
    union {
        int a;
        float b;
    } _data;
    int id;
} SensorData;

void init_sensor(SensorData* s);

#endif
""")

    _write_file(src / "data.c", """\
#include "data.h"

void init_sensor(SensorData* s) {
    s->_payload.x = 0;
    s->_payload.y = 0;
    s->_data.a = 0;
    s->id = -1;
}
""")

    cc = [
        {"directory": str(src), "file": "main.c",
         "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "main.c", "-o", "build/main.o"]},
        {"directory": str(src), "file": "data.c",
         "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "data.c", "-o", "build/data.o"]},
    ]
    cc_json = proj / "compile_commands.json"
    cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

    # Initialize project to generate UUID4 project ID
    init_result = _cli(
        ["init", "--project", str(proj)],
        cwd=proj,
        timeout=60,
    )
    if init_result.returncode != 0:
        pytest.fail(f"Init failed:\nSTDOUT:\n{init_result.stdout}\nSTDERR:\n{init_result.stderr}")

    return proj


@pytest.fixture(scope="class")
def indexed_anon(c_project_anon: Path):
    """Index the anonymous-struct project and return the project root."""
    cc_json = c_project_anon / "compile_commands.json"
    result = _cli(
        ["index", "--no-refs", "--no-analyze", "--no-embeddings", str(cc_json)],
        cwd=c_project_anon,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.fail(f"Index failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    db_path = _db_path_for_project(c_project_anon)
    assert db_path.exists(), f"DB not created at {db_path}"
    yield c_project_anon
    _cleanup_index_db(c_project_anon)


class TestAnonymousStructUnionIndexing:
    """Verify that anonymous structs/unions are indexed with correct field names."""

    def test_anonymous_struct_uses_field_name(self, indexed_anon: Path):
        """`struct { int x; int y; } _payload;` is indexed as `_payload`, not unnamed."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            symbols = conn.execute(
                """SELECT name, qualified_name, kind FROM symbols
                   WHERE config_hash=? AND name='_payload'""",
                (ch,),
            ).fetchall()
            assert len(symbols) >= 1, (
                "Expected symbol '_payload' (anonymous struct via field name)"
            )
            assert symbols[0]["kind"] == "struct"
            assert "_payload" in symbols[0]["qualified_name"]
        finally:
            conn.close()

    def test_anonymous_union_uses_field_name(self, indexed_anon: Path):
        """`union { int a; float b; } _data;` is indexed as `_data`, not unnamed."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            symbols = conn.execute(
                """SELECT name, qualified_name, kind FROM symbols
                   WHERE config_hash=? AND name='_data'""",
                (ch,),
            ).fetchall()
            assert len(symbols) >= 1, (
                "Expected symbol '_data' (anonymous union via field name)"
            )
            assert symbols[0]["kind"] == "union"
        finally:
            conn.close()

    def test_no_unnamed_or_anonymous_in_names(self, indexed_anon: Path):
        """No symbol name should contain '(unnamed' or '(anonymous'."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            unnamed = conn.execute(
                """SELECT name FROM symbols
                   WHERE config_hash=? AND (name LIKE '%(unnamed%' OR name LIKE '%(anonymous%')""",
                (ch,),
            ).fetchall()
            assert len(unnamed) == 0, f"Found unnamed symbols: {[r['name'] for r in unnamed]}"
        finally:
            conn.close()

    def test_union_declarations_exist(self, indexed_anon: Path):
        """Union symbols are present in the index with kind='union'."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            unions = conn.execute(
                """SELECT name, qualified_name, kind FROM symbols
                   WHERE config_hash=? AND kind='union'""",
                (ch,),
            ).fetchall()
            union_names = {r["name"] for r in unions}
            assert "_data" in union_names, f"Union '_data' not in index. Found: {union_names}"
        finally:
            conn.close()

    def test_anonymous_struct_fields_are_indexed(self, indexed_anon: Path):
        """Fields inside anonymous structs are still indexed (x, y inside _payload)."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            fields = conn.execute(
                """SELECT name, qualified_name, kind FROM symbols
                   WHERE config_hash=? AND name IN ('x', 'y') AND kind='field'""",
                (ch,),
            ).fetchall()
            field_names = {r["name"] for r in fields}
            assert "x" in field_names, f"Field 'x' not indexed. Fields: {field_names}"
            assert "y" in field_names, f"Field 'y' not indexed. Fields: {field_names}"
            for f in fields:
                assert "_payload" in f["qualified_name"], (
                    f"Field {f['name']} should be under _payload, got: {f['qualified_name']}"
                )
        finally:
            conn.close()

    def test_union_kind_filter_works(self, indexed_anon: Path):
        """Querying symbols with kind='union' returns union declarations."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            rows = conn.execute(
                """SELECT name, kind FROM symbols
                   WHERE config_hash=? AND kind='union'""",
                (ch,),
            ).fetchall()
            for r in rows:
                assert r["kind"] == "union"
        finally:
            conn.close()

    def test_named_struct_alongside_anonymous_still_works(self, indexed_anon: Path):
        """Named structs (SensorData) coexist correctly with anonymous ones."""
        db_path = _db_path_for_project(indexed_anon)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            sensor = conn.execute(
                """SELECT name, kind FROM symbols
                   WHERE config_hash=? AND name='SensorData'""",
                (ch,),
            ).fetchall()
            assert len(sensor) >= 1, "Named struct SensorData not found"
        finally:
            conn.close()


class TestToolIntegrationWithAnonymousSymbols:
    """Verify that MCP tools return correct results for anonymous symbols."""

    def test_lookup_symbol_finds_anonymous_struct(self, indexed_anon: Path):
        """lookup_symbol finds anonymous struct by field name."""
        from fw_context_mcp.mcp.handlers.search import lookup_symbol

        results = lookup_symbol(name="_payload", project_root=str(indexed_anon), exact=True)
        errors = [r for r in results if "error" in r]
        assert not errors, f"lookup_symbol returned errors: {errors}"
        assert len(results) >= 1
        names = [r["name"] for r in results if "name" in r]
        assert "_payload" in names

    def test_lookup_symbol_finds_anonymous_union(self, indexed_anon: Path):
        """lookup_symbol finds anonymous union by field name."""
        from fw_context_mcp.mcp.handlers.search import lookup_symbol

        results = lookup_symbol(name="_data", project_root=str(indexed_anon), exact=True)
        errors = [r for r in results if "error" in r]
        assert not errors, f"lookup_symbol returned errors: {errors}"
        matches = [r for r in results if r.get("name") == "_data"]
        assert len(matches) >= 1
        assert any(r.get("kind") == "union" for r in matches), (
            f"_data should have kind='union', got: {matches}"
        )

    def test_get_source_returns_anonymous_struct_body(self, indexed_anon: Path):
        """get_source returns the body of the anonymous struct."""
        from fw_context_mcp.mcp.handlers.source import get_source

        result = get_source(name="_payload", project_root=str(indexed_anon))
        assert "error" not in result, f"get_source returned error: {result}"
        source = result.get("source", "")
        assert "x" in source or "int" in source, f"Source missing struct body: {source}"

    def test_get_source_returns_anonymous_union_body(self, indexed_anon: Path):
        """get_source returns the body of the anonymous union."""
        from fw_context_mcp.mcp.handlers.source import get_source

        result = get_source(name="_data", project_root=str(indexed_anon))
        assert "error" not in result, f"get_source returned error: {result}"
        source = result.get("source", "")
        assert "a" in source or "float" in source or "int" in source, (
            f"Source missing union body: {source}"
        )

    def test_get_file_map_includes_union_kind(self, indexed_anon: Path):
        """get_file_map returns union symbols grouped under their kind."""
        from fw_context_mcp.mcp.handlers.source import get_file_map

        result = get_file_map(file_path="data.h", project_root=str(indexed_anon))
        assert "error" not in result, f"get_file_map returned error: {result}"
        _symbols = result.get("symbols", {})

    def test_get_symbol_context_for_anonymous_struct(self, indexed_anon: Path):
        """get_symbol_context returns context for anonymous struct symbol."""
        from fw_context_mcp.mcp.handlers.source import get_symbol_context

        result = get_symbol_context(name="_payload", project_root=str(indexed_anon))
        assert "error" not in result, f"get_symbol_context returned error: {result}"
        assert result.get("kind") == "struct"

    def test_get_symbol_context_for_anonymous_union(self, indexed_anon: Path):
        """get_symbol_context returns context for anonymous union symbol."""
        from fw_context_mcp.mcp.handlers.source import get_symbol_context

        result = get_symbol_context(name="_data", project_root=str(indexed_anon))
        assert "error" not in result, f"get_symbol_context returned error: {result}"
        assert result.get("kind") == "union"

    def test_search_code_finds_union_by_kind_filter(self, indexed_anon: Path):
        """search_code with kind='union' returns union symbols."""
        from fw_context_mcp.mcp.handlers.search import search_code

        results = search_code(query="_data", project_root=str(indexed_anon), kind="union")
        errors = [r for r in results if "error" in r]
        assert not errors, f"search_code returned errors: {errors}"
        found = [r for r in results if r.get("kind") == "union"]
        assert len(found) >= 1

    def test_search_code_finds_struct_by_kind_filter(self, indexed_anon: Path):
        """search_code with kind='struct' returns anonymous struct symbols."""
        from fw_context_mcp.mcp.handlers.search import search_code

        results = search_code(query="_payload", project_root=str(indexed_anon), kind="struct")
        errors = [r for r in results if "error" in r]
        assert not errors, f"search_code returned errors: {errors}"
        found = [r for r in results if r.get("kind") == "struct"]
        assert len(found) >= 1
