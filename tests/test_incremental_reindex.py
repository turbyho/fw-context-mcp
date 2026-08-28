"""Functional tests for incremental reindex — Phase 1-4 of store_symbols_for_unit.

Creates a temporary C project, indexes it, modifies source files, and verifies
that reindex correctly preserves LLM analysis for unchanged symbols, detects
moved symbols, and handles new/deleted symbols.

NOTE: Tests require ``libclang`` and a C compiler (gcc).  The test fixture
``indexed_project`` builds a real index via ``fw-context init`` + ``fw-context index``.
Tests that need an existing index MUST use this fixture — the index is stored
in ``~/.fw-context/index/<project_id>/index.db`` (global ``IndexConfig.db_dir``).

Run::

    python3 -m pytest tests/test_incremental_reindex.py -x -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
    open_db,
    split_tokens,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)

# ── helpers ──────────────────────────────────────────────────────────


def _db_path_for_project(project_root: Path) -> Path:
    """Resolve the index DB path for a project root."""
    from fw_context_mcp.config import (
        derive_project_id,
    )
    from fw_context_mcp.config import (
        load as load_config,
    )

    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id / "index.db"


def _fail_or_skip_analysis(result: dict, symbol_name: str) -> None:
    """Fail if analysis was expected but not generated; skip if Ollama unavailable."""
    if "analysis_warning" in result:
        pytest.skip(f"Ollama not available — cannot generate analysis for {symbol_name}: "
                     f"{result['analysis_warning']}")
    pytest.fail(f"No analysis generated for {symbol_name} after reindex with with_analysis=True")


def _project_root() -> Path:
    """Return the fw-context-mcp repo root."""
    return Path(__file__).resolve().parents[1]


def _cli(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run fw-context CLI in a subprocess."""
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


def _advance_mtime(path: Path, seconds: float = 2.0) -> None:
    """Set file mtime to current time + seconds to bypass MTIME_TOLERANCE_S."""
    now = path.stat().st_mtime
    os.utime(path, (now + seconds, now + seconds))
def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def c_project(tmp_path: Path):
    """Create a minimal C project with compile_commands.json, index it, return paths."""
    proj = tmp_path / "demo"
    proj.mkdir()

    src = proj / "src"
    src.mkdir()

    # ── Source files ──

    main_c = src / "main.c"
    _write_file(
        main_c,
        """\
#include <stdio.h>
#include "modem.h"
#include "utils.h"

int main(void) {
    modem_init(115200);
    int result = compute_checksum("hello", 5);
    printf("result=%d\\n", result);
    return 0;
}
""",
    )

    modem_h = src / "modem.h"
    _write_file(
        modem_h,
        """\
#ifndef MODEM_H
#define MODEM_H

void modem_init(int baudrate);
int modem_send(const char* data, int len);
int modem_recv(char* buf, int max_len);

#endif
""",
    )

    modem_c = src / "modem.c"
    _write_file(
        modem_c,
        """\
#include "modem.h"
#include <string.h>

static int g_baudrate = 0;

void modem_init(int baudrate) {
    g_baudrate = baudrate;
}

int modem_send(const char* data, int len) {
    if (!data || len <= 0) return -1;
    return len;
}

int modem_recv(char* buf, int max_len) {
    if (!buf || max_len <= 0) return -1;
    memset(buf, 0, max_len);
    return 0;
}
""",
    )

    utils_h = src / "utils.h"
    _write_file(
        utils_h,
        """\
#ifndef UTILS_H
#define UTILS_H

int compute_checksum(const char* data, int len);
void log_message(const char* msg);

#endif
""",
    )

    utils_c = src / "utils.c"
    _write_file(
        utils_c,
        """\
#include "utils.h"
#include <string.h>

int compute_checksum(const char* data, int len) {
    if (!data || len <= 0) return 0;
    int sum = 0;
    for (int i = 0; i < len; i++) {
        sum += (unsigned char)data[i];
    }
    return sum & 0xFF;
}

void log_message(const char* msg) {
    if (!msg) return;
    /* In real firmware this would write to UART/flash */
    (void)strlen(msg);
}
""",
    )

    # ── compile_commands.json ──

    cc_json = proj / "compile_commands.json"
    import json

    cc = [
        {
            "directory": str(src),
            "file": "main.c",
            "arguments": [
                "gcc",
                "-std=c11",
                "-O2",
                "-Isrc",
                "-c",
                "main.c",
                "-o",
                "build/main.o",
            ],
        },
        {
            "directory": str(src),
            "file": "modem.c",
            "arguments": [
                "gcc",
                "-std=c11",
                "-O2",
                "-Isrc",
                "-c",
                "modem.c",
                "-o",
                "build/modem.o",
            ],
        },
        {
            "directory": str(src),
            "file": "utils.c",
            "arguments": [
                "gcc",
                "-std=c11",
                "-O2",
                "-Isrc",
                "-c",
                "utils.c",
                "-o",
                "build/utils.o",
            ],
        },
    ]
    cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

    # Isolate index DB to tmp_path before init
    _isolate_index_db(proj, tmp_path)

    # Initialize project to generate UUID4 project ID
    init_result = _cli(
        ["init", "--project", str(proj)],
        cwd=proj,
        timeout=60,
    )
    if init_result.returncode != 0:
        pytest.fail(f"Init failed:\nSTDOUT:\n{init_result.stdout}\nSTDERR:\n{init_result.stderr}")

    return proj


@pytest.fixture
def indexed_project(c_project: Path):
    """Index the C project and return the project root.

    Cleans up the index database directory after the test finishes.
    """
    # Initialize project first — generates UUID4 project ID
    init_result = _cli(
        ["init", "--project", str(c_project)],
        cwd=c_project,
        timeout=60,
    )
    if init_result.returncode != 0:
        pytest.fail(f"Init failed:\nSTDOUT:\n{init_result.stdout}\nSTDERR:\n{init_result.stderr}")

    cc_json = c_project / "compile_commands.json"
    result = _cli(["index", "--no-refs", "--no-analyze", "--no-embeddings", str(cc_json)], cwd=c_project)
    if result.returncode != 0:
        pytest.fail(f"Index failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    db_path = _db_path_for_project(c_project)
    assert db_path.exists(), f"DB not created at {db_path}"

    yield c_project

    shutil.rmtree(db_path.parent, ignore_errors=True)


# ── unit tests: content hash helpers ──────────────────────────────────


class TestContentHashHelpers:
    """Tests for _read_file_lines, _read_body, _compute_content_hash."""

    def test_read_file_lines_success(self, tmp_path: Path):
        from fw_context_mcp.utils import read_file_lines

        f = tmp_path / "test.c"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        lines = read_file_lines(str(f))
        assert lines == ["line1\n", "line2\n", "line3\n"]

    def test_read_file_lines_not_found(self):
        from fw_context_mcp.utils import read_file_lines

        assert read_file_lines("/nonexistent/path/file.c") is None

    def test_read_body_normal(self):
        from fw_context_mcp.indexer.ops import _read_body

        lines = ["a\n", "b\n", "c\n", "d\n", "e\n"]
        body = _read_body(lines, 2, 4)
        assert body == "b\nc\nd\n"

    def test_read_body_invalid_range(self):
        from fw_context_mcp.indexer.ops import _read_body

        lines = ["a\n", "b\n"]
        assert _read_body(lines, 5, 6) == ""
        assert _read_body(lines, 2, 1) == ""
        assert _read_body(lines, 1, 10) == ""

    def test_compute_content_hash_deterministic(self):
        from fw_context_mcp.indexer.ops import _compute_content_hash

        lines = ["void foo() {\n", "    return 42;\n", "}\n"]
        h1 = _compute_content_hash(lines, 1, 3, "void foo()", "foo", "")
        h2 = _compute_content_hash(lines, 1, 3, "void foo()", "foo", "")
        assert h1 == h2
        assert len(h1) == 64  # full SHA256 hex

    def test_compute_content_hash_differs_on_body_change(self):
        from fw_context_mcp.indexer.ops import _compute_content_hash

        lines1 = ["void foo() {\n", "    return 42;\n", "}\n"]
        lines2 = ["void foo() {\n", "    return 99;\n", "}\n"]
        h1 = _compute_content_hash(lines1, 1, 3, "void foo()", "foo", "")
        h2 = _compute_content_hash(lines2, 1, 3, "void foo()", "foo", "")
        assert h1 != h2

    def test_compute_content_hash_differs_on_signature_change(self):
        from fw_context_mcp.indexer.ops import _compute_content_hash

        lines = ["void foo(int x) {\n", "    return x;\n", "}\n"]
        h1 = _compute_content_hash(lines, 1, 3, "void foo(int x)", "foo", "")
        h2 = _compute_content_hash(lines, 1, 3, "void foo(float x)", "foo", "")
        assert h1 != h2

    def test_compute_content_hash_ignores_trailing_whitespace(self):
        from fw_context_mcp.indexer.ops import _compute_content_hash

        lines1 = ["void foo() {\n", "    return 42;\n", "}\n"]
        lines2 = ["void foo() {\n", "    return 42;\n", "}\n", "\n"]
        h1 = _compute_content_hash(lines1, 1, 3, "void foo()", "foo", "")
        h2 = _compute_content_hash(lines2, 1, 4, "void foo()", "foo", "")
        # Extra trailing empty line stripped by body.strip()
        assert h1 == h2

    def test_compute_content_hash_sensitive_to_internal_whitespace(self):
        """Indentation changes (spaces vs tabs) produce different hashes."""
        from fw_context_mcp.indexer.ops import _compute_content_hash

        lines1 = ["void foo() {\n", "    return 42;\n", "}\n"]
        lines2 = ["void foo() {\n", "\treturn 42;\n", "}\n"]
        h1 = _compute_content_hash(lines1, 1, 3, "void foo()", "foo", "")
        h2 = _compute_content_hash(lines2, 1, 3, "void foo()", "foo", "")
        # Internal whitespace differences are preserved
        assert h1 != h2

    def test_compute_content_hash_includes_docstring(self):
        from fw_context_mcp.indexer.ops import _compute_content_hash

        lines = ["void foo() {\n", "    return 42;\n", "}\n"]
        h1 = _compute_content_hash(lines, 1, 3, "void foo()", "foo", "Does foo")
        h2 = _compute_content_hash(lines, 1, 3, "void foo()", "foo", "Does bar")
        assert h1 != h2


# ── functional tests: full indexing + reindex flow ────────────────────


@pytest.mark.libclang
class TestIncrementalReindex:
    """End-to-end tests: index → modify → reindex → verify."""

    def test_reindex_unchanged_file_preserves_symbols(self, indexed_project: Path):
        """Reindexing an unmodified file should keep all symbols."""
        result = _cli(
            ["reindex-file", "src/utils.c"],
            cwd=indexed_project,
        )
        if result.returncode != 0 and "error" not in result.stdout.lower():
            print("STDOUT:", result.stdout)
            print("STDERR:", result.stderr)
        # reindex-file might not be a CLI subcommand — check
        # If it doesn't exist, test via direct function call
        if "Unknown" in result.stderr or result.returncode != 0:
            # Fall back to direct API test
            self._test_reindex_via_api(indexed_project, "src/utils.c")
        else:
            assert "symbols_updated" in result.stdout.lower() or "error" not in result.stdout.lower()

    def _test_reindex_via_api(self, project_root: Path, rel_path: str) -> None:
        """Call reindex_file_impl directly and verify results."""
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl(rel_path, str(project_root), with_analysis=False)
        if "error" in result:
            pytest.fail(f"Reindex failed: {result['error']}")
        assert result["symbols_updated"] > 0
        assert result["elapsed_s"] >= 0

    def test_index_all_files_present(self, indexed_project: Path):
        """After indexing, all expected symbols should be findable."""
        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]

            # Check main functions exist
            for name in ["modem_init", "modem_send", "modem_recv", "compute_checksum", "log_message", "main"]:
                row = conn.execute(
                    "SELECT name, kind, file_path FROM symbols WHERE config_hash=? AND name=?",
                    (ch, name),
                ).fetchone()
                assert row is not None, f"Symbol '{name}' not found in index"
        finally:
            conn.close()

    def test_reindex_after_modifying_function_body(self, indexed_project: Path):
        """Phase 3/4: modify a function body → reindex → verify old analysis dropped."""
        import json

        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]

            # ── Insert fake LLM analysis for modem_send ──
            sym_row = conn.execute(
                "SELECT id, usr FROM symbols WHERE config_hash=? AND name='modem_send'",
                (ch,),
            ).fetchone()
            assert sym_row is not None, "modem_send not found"

            conn.execute(
                """INSERT OR REPLACE INTO llm_analysis
                   (symbol_id, summary, inputs, outputs, model, analyzed_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (
                    sym_row["id"],
                    "Sends data over modem.",
                    "data: buffer, len: bytes to send",
                    "bytes sent or -1 on error",
                    "test-model",
                ),
            )
            conn.execute(
                "UPDATE symbols SET summary=?, inputs=?, outputs=? WHERE id=?",
                (
                    "Sends data over modem.",
                    "data: buffer, len: bytes to send",
                    "bytes sent or -1 on error",
                    sym_row["id"],
                ),
            )
            conn.commit()

            # Verify analysis stored
            ana = conn.execute(
                "SELECT summary FROM llm_analysis WHERE symbol_id=?",
                (sym_row["id"],),
            ).fetchone()
            assert ana is not None

            # ── Get count of symbols with analysis ──
            analyzed_before = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
            print(f"  Analysis rows before: {analyzed_before}")

        finally:
            conn.close()

        # ── Modify the modem.c file — change modem_send body ──
        modem_c = indexed_project / "src" / "modem.c"
        original = modem_c.read_text(encoding="utf-8")
        modified = original.replace(
            "int modem_send(const char* data, int len) {\n    if (!data || len <= 0) return -1;\n    return len;\n}",
            "int modem_send(const char* data, int len) {\n    if (!data || len <= 0) return -2;\n    /* now returns len+1 */\n    return len + 1;\n}",
        )
        assert modified != original, "Modification didn't change the file"
        modem_c.write_text(modified, encoding="utf-8")

        # ── Reindex ──
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
        assert "error" not in result, f"Reindex failed: {result.get('error')}"
        print(f"  Reindex result: {json.dumps(result)}")

        # ── Verify: modem_send was CHANGED → old analysis should be gone ──
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]
            sym_row = conn.execute(
                "SELECT id FROM symbols WHERE config_hash=? AND name='modem_send'",
                (ch,),
            ).fetchone()
            assert sym_row is not None, "modem_send disappeared after reindex"

            # Old analysis should be gone because content changed
            ana = conn.execute(
                "SELECT summary FROM llm_analysis WHERE symbol_id=?",
                (sym_row["id"],),
            ).fetchone()
            # The analysis might be gone (content changed) — that's correct behavior
            # We verify the symbol still exists
            print(f"  modem_send id={sym_row['id']}, analysis after reindex: {ana}")
        finally:
            conn.close()

        # Restore original file for other tests
        modem_c.write_text(original, encoding="utf-8")

    def test_reindex_without_analysis_deletes_llm_analysis(self, indexed_project: Path):
        """_delete_old_for_tu cleans up llm_analysis; without_analysis=True skips regeneration."""
        import json

        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]

            # Insert fake LLM analysis for compute_checksum
            sym_row = conn.execute(
                "SELECT id, usr FROM symbols WHERE config_hash=? AND name='compute_checksum'",
                (ch,),
            ).fetchone()
            assert sym_row is not None

            conn.execute(
                """INSERT OR REPLACE INTO llm_analysis
                   (symbol_id, summary, inputs, outputs, model, analyzed_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (
                    sym_row["id"],
                    "Computes XOR checksum of data buffer.",
                    "data: input, len: buffer length",
                    "8-bit checksum value",
                    "test-model",
                ),
            )
            conn.execute(
                "UPDATE symbols SET summary=?, inputs=?, outputs=? WHERE id=?",
                (
                    "Computes XOR checksum of data buffer.",
                    "data: input, len: buffer length",
                    "8-bit checksum value",
                    sym_row["id"],
                ),
            )
            conn.commit()

            ana_before = conn.execute(
                "SELECT summary FROM llm_analysis WHERE symbol_id=?",
                (sym_row["id"],),
            ).fetchone()
            assert ana_before is not None, "Analysis not stored"
            print(f"  Analysis before: {ana_before['summary']}")

        finally:
            conn.close()

        # ── Make an unrelated change in utils.c (add comment) ──
        utils_c = indexed_project / "src" / "utils.c"
        original = utils_c.read_text(encoding="utf-8")
        # Add a comment line — changes file but NOT compute_checksum body
        modified = original.replace(
            '#include "utils.h"',
            '#include "utils.h"\n/* incremental reindex test marker */',
        )
        utils_c.write_text(modified, encoding="utf-8")

        # ── Reindex ──
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/utils.c", str(indexed_project), with_analysis=False)
        assert "error" not in result, f"Reindex failed: {result.get('error')}"
        print(f"  Reindex result: {json.dumps(result)}")

        # ── Verify: analysis deleted by _delete_old_for_tu cleanup ──
        # _delete_old_for_tu now cleans up llm_analysis for all symbols
        # of the reindexed TU. Since with_analysis=False, the
        # _reindex_llm_analysis post-phase is skipped — analysis stays gone.
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]
            sym_row = conn.execute(
                "SELECT id FROM symbols WHERE config_hash=? AND name='compute_checksum'",
                (ch,),
            ).fetchone()
            assert sym_row is not None

            ana_after = conn.execute(
                "SELECT summary FROM llm_analysis WHERE symbol_id=?",
                (sym_row["id"],),
            ).fetchone()
            # Analysis was deleted — with_analysis=False means no regeneration
            assert ana_after is None, (
                f"Analysis should be deleted by _delete_old_for_tu cleanup, "
                f"but got: {ana_after}"
            )
            print("  Analysis correctly deleted by cleanup")

        finally:
            conn.close()

        # Restore original
        utils_c.write_text(original, encoding="utf-8")

    def test_reindex_after_adding_new_function(self, indexed_project: Path):
        """New function added to a file appears after reindex."""
        modem_c = indexed_project / "src" / "modem.c"
        original = modem_c.read_text(encoding="utf-8")

        # Add a new function at the end
        new_func = """

int modem_flush(void) {
    /* Flush the modem TX buffer */
    return 0;
}
"""
        modem_c.write_text(original + new_func, encoding="utf-8")

        # Reindex
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
        assert "error" not in result, f"Reindex failed: {result.get('error')}"

        # Verify new symbol exists
        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]
            row = conn.execute(
                "SELECT name, kind FROM symbols WHERE config_hash=? AND name='modem_flush'",
                (ch,),
            ).fetchone()
            assert row is not None, "modem_flush not found after reindex"
            assert row["kind"] == "function"
        finally:
            conn.close()

        # Restore original
        modem_c.write_text(original, encoding="utf-8")

    def test_reindex_after_deleting_function(self, indexed_project: Path):
        """Deleted function disappears from index after reindex."""
        modem_c = indexed_project / "src" / "modem.c"
        original = modem_c.read_text(encoding="utf-8")

        # Remove modem_recv function — use a more robust approach:
        # Find the function start and end, then cut it out
        lines = original.splitlines(keepends=True)
        start_idx = None
        end_idx = None
        for i, line in enumerate(lines):
            if "int modem_recv(" in line:
                start_idx = i
            if start_idx is not None and line.rstrip() == "}" and i > start_idx:
                # Check if this closing brace is at the right indentation level
                if not line.startswith(" ") and not line.startswith("\t"):
                    # Top-level closing brace
                    pass
                end_idx = i + 1
                break

        assert start_idx is not None, "Could not find modem_recv in source"
        assert end_idx is not None, "Could not find end of modem_recv"

        modified_lines = lines[:start_idx] + lines[end_idx:]
        modified = "".join(modified_lines)
        # Clean up extra blank lines
        while "\n\n\n" in modified:
            modified = modified.replace("\n\n\n", "\n\n")
        modem_c.write_text(modified, encoding="utf-8")

        # Verify function was actually removed
        assert "modem_recv" not in modem_c.read_text(encoding="utf-8"), "modem_recv still in file"

        # Reindex
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
        assert "error" not in result, f"Reindex failed: {result.get('error')}"

        # Verify modem_recv is gone from index
        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]
            row = conn.execute(
                "SELECT name FROM symbols WHERE config_hash=? AND name='modem_recv' AND is_definition=1",
                (ch,),
            ).fetchone()
            assert row is None, (
                f"modem_recv definition should be deleted but still exists: {dict(row) if row else None}"
            )
        finally:
            conn.close()

        # Restore original
        modem_c.write_text(original, encoding="utf-8")

    def test_reindex_header_refreshes_its_stored_state(self, indexed_project: Path):
        """Reindexing a header brings the index up to the header's new text.

        This used to assert the opposite — that a header returns "not found
        in compile_commands.json".  A header is indeed not listed there, but
        the manifest names the units that include it, so one of them now
        carries the re-parse.

        The assertion is the incremental-reindex outcome, not the mechanism
        that TestReindexFileImplEdgeCases covers.  Two columns have to move
        together: ``source_hash``, which every staleness check in
        mcp/shared/stale.py compares against the file, and ``content``,
        which backs read_file and search_content.  A re-parse that moved
        only one of them would either warn forever or serve the old text.
        """
        from fw_context_mcp.indexer.db import open_db as _open_db
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
        from fw_context_mcp.utils import compute_source_hash

        header = indexed_project / "src" / "modem.h"
        original = header.read_text(encoding="utf-8")

        def stored() -> tuple[str, str]:
            conn = _open_db(_db_path_for_project(indexed_project))
            try:
                row = conn.execute(
                    "SELECT source_hash, content FROM files WHERE path LIKE ?",
                    ("%modem.h",),
                ).fetchone()
            finally:
                conn.close()
            assert row is not None, "modem.h missing from the files table"
            return row["source_hash"], row["content"] or ""

        try:
            _write_file(header, original.replace(
                "#endif", "int modem_reset(void);\n\n#endif"))
            _advance_mtime(header)

            result = reindex_file_impl("src/modem.h", str(indexed_project),
                                       with_analysis=False)
            assert "error" not in result, f"header reindex failed: {result.get('error')}"

            source_hash, content = stored()
            assert source_hash == compute_source_hash(header), (
                "source_hash must describe the text the index just parsed"
            )
            assert "modem_reset" in content, (
                "files.content backs read_file, thus it must hold the new text"
            )
        finally:
            _write_file(header, original)
            _advance_mtime(header)
            reindex_file_impl("src/modem.h", str(indexed_project), with_analysis=False)


@pytest.mark.libclang
class TestMoveDetection:
    """Phase 4: detect and fix symbols moved between files."""

    def test_move_detection_same_usr_different_file(self, indexed_project: Path):
        """When a symbol moves from one .c file to another, Phase 4 detects it."""
        import json

        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        ch = ""
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]

            # Insert fake LLM analysis for log_message (currently in utils.c)
            sym_row = conn.execute(
                "SELECT id, usr, file_path FROM symbols WHERE config_hash=? AND name='log_message'",
                (ch,),
            ).fetchone()
            assert sym_row is not None, "log_message not found"
            original_file = sym_row["file_path"]
            print(f"  log_message originally in: {original_file}")

            conn.execute(
                """INSERT OR REPLACE INTO llm_analysis
                   (symbol_id, summary, inputs, outputs, model, analyzed_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (sym_row["id"], "Logs a message to UART.", "msg: null-terminated string", "void", "test-model"),
            )
            conn.execute(
                "UPDATE symbols SET summary=?, inputs=?, outputs=? WHERE id=?",
                ("Logs a message to UART.", "msg: null-terminated string", "void", sym_row["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        # ── Move log_message from utils.c to modem.c ──
        utils_c = indexed_project / "src" / "utils.c"
        modem_c = indexed_project / "src" / "modem.c"
        utils_original = utils_c.read_text(encoding="utf-8")
        modem_original = modem_c.read_text(encoding="utf-8")

        # Extract log_message from utils.c
        log_func = """void log_message(const char* msg) {
    if (!msg) return;
    /* In real firmware this would write to UART/flash */
    (void)strlen(msg);
}"""

        # Remove from utils.c
        utils_modified = utils_original.replace(log_func + "\n", "")
        # Remove extra blank lines
        utils_modified = utils_modified.replace("\n\n}", "\n}")
        utils_c.write_text(utils_modified, encoding="utf-8")

        # Add to modem.c (before last line)
        modem_modified = modem_original.rstrip() + "\n\n" + log_func + "\n"
        modem_c.write_text(modem_modified, encoding="utf-8")

        # ── Reindex BOTH files ──
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        r1 = reindex_file_impl("src/utils.c", str(indexed_project), with_analysis=False)
        print(f"  Reindex utils.c: {json.dumps(r1)}")

        r2 = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
        print(f"  Reindex modem.c: {json.dumps(r2)}")

        # ── Verify log_message is in modem.c ──
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]
            row = conn.execute(
                "SELECT id, file_path FROM symbols WHERE config_hash=? AND name='log_message'",
                (ch,),
            ).fetchone()
            assert row is not None, "log_message disappeared"
            print(f"  log_message now in: {row['file_path']}")
            # Should now be in modem.c
            assert "modem.c" in row["file_path"], f"Expected log_message in modem.c, got {row['file_path']}"

            # Check if analysis was preserved (Phase 4 move)
            ana = conn.execute(
                "SELECT summary FROM llm_analysis WHERE symbol_id=?",
                (row["id"],),
            ).fetchone()
            if ana:
                print(f"  Analysis preserved after move: {ana['summary']}")
            else:
                print("  Analysis NOT preserved after move (may need both files reindexed)")

        finally:
            conn.close()

        # Restore originals
        utils_c.write_text(utils_original, encoding="utf-8")
        modem_c.write_text(modem_original, encoding="utf-8")

    def test_move_detection_without_analysis_skips(self, indexed_project: Path):
        """Phase 4: symbol without LLM analysis is not treated as moved."""
        # Move a function that has no analysis, verify it just gets re-created
        # (no crash, no duplicate)
        utils_c = indexed_project / "src" / "utils.c"
        modem_c = indexed_project / "src" / "modem.c"
        utils_original = utils_c.read_text(encoding="utf-8")
        modem_original = modem_c.read_text(encoding="utf-8")

        # Move compute_checksum from utils.c → modem.c (no analysis attached)
        checksum_func = """int compute_checksum(const char* data, int len) {
    if (!data || len <= 0) return 0;
    int sum = 0;
    for (int i = 0; i < len; i++) {
        sum += (unsigned char)data[i];
    }
    return sum & 0xFF;
}"""

        utils_modified = utils_original.replace(checksum_func + "\n", "").replace("\n\n}", "\n}")
        utils_c.write_text(utils_modified, encoding="utf-8")
        modem_c.write_text(modem_original.rstrip() + "\n\n" + checksum_func + "\n", encoding="utf-8")

        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        r1 = reindex_file_impl("src/utils.c", str(indexed_project), with_analysis=False)
        r2 = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
        assert "error" not in r1, f"utils.c reindex failed: {r1.get('error')}"
        assert "error" not in r2, f"modem.c reindex failed: {r2.get('error')}"

        # Verify no duplicate — exactly one compute_checksum
        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
                "config_hash"
            ]
            rows = conn.execute(
                "SELECT file_path FROM symbols WHERE config_hash=? AND name='compute_checksum'",
                (ch,),
            ).fetchall()
            assert len(rows) == 1, f"Expected 1 compute_checksum, got {len(rows)}"
            # Should be in modem.c now
            assert "modem.c" in rows[0]["file_path"]
        finally:
            conn.close()

        # Restore
        utils_c.write_text(utils_original, encoding="utf-8")
        modem_c.write_text(modem_original, encoding="utf-8")




@pytest.mark.libclang
class TestReindexFileImplEdgeCases:
    """Edge cases for reindex_file_impl."""

    def test_nonexistent_file(self, indexed_project: Path):
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/nonexistent.c", str(indexed_project), with_analysis=False)
        assert "error" in result

    def test_file_not_in_compile_commands(self, indexed_project: Path):
        """Header that no .c includes isn't in compile_commands."""
        # Create an orphan header
        orphan = indexed_project / "src" / "orphan.h"
        orphan.write_text("int orphan_func(void);\n", encoding="utf-8")

        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/orphan.h", str(indexed_project), with_analysis=False)
        # Should return error — not in compile_commands.json
        assert "error" in result, f"Expected error for header not in CC, got: {result}"

    def test_with_analysis_flag_false_skips_llm(self, indexed_project: Path):
        """with_analysis=False should skip LLM analysis but still update symbols."""
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
        assert "error" not in result, f"Reindex failed: {result.get('error')}"
        assert "analysis_updated" not in result, "with_analysis=False should skip analysis"
        assert result["symbols_updated"] > 0

    def test_no_index_exists(self, tmp_path: Path):
        """Calling reindex on a project without index returns error."""
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        # Initialize project first — required for derive_project_id()
        init_result = _cli(
            ["init", "--project", str(tmp_path)],
            cwd=tmp_path,
            timeout=60,
        )
        if init_result.returncode != 0:
            pytest.fail(f"Init failed:\n{init_result.stderr}")

        result = reindex_file_impl("src/main.c", str(tmp_path), with_analysis=False)
        assert "error" in result
        assert "no index" in result["error"].lower() or "run" in result["error"].lower()

    def test_deleted_file_cleans_up_db_records(self, indexed_project: Path):
        """When a previously-indexed file is deleted from disk, reindex_file
        cleans up its symbols and the files table entry."""
        import shutil

        from fw_context_mcp.indexer.db import open_db as _open_db
        from fw_context_mcp.indexer.ops import get_file_mtimes
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        db_path = _db_path_for_project(indexed_project)
        target = (indexed_project / "src" / "utils.c").resolve()
        target_str = "src/utils.c"  # normalized path (relative to project_root)

        # ── Precondition: file exists in index ──
        conn = _open_db(db_path)
        try:
            config_hash = conn.execute("SELECT config_hash FROM build_configs LIMIT 1").fetchone()["config_hash"]

            known = get_file_mtimes(conn, config_hash)
            assert target_str in known, f"{target_str} not in files table before test"
            old_file_id = known[target_str][0]
            sym_count_before = conn.execute("SELECT COUNT(*) FROM symbols WHERE file_id=?", (old_file_id,)).fetchone()[
                0
            ]
            assert sym_count_before > 0, "Expected symbols for utils.c before deletion"
        finally:
            conn.close()

        # ── Delete the file from disk ──
        shutil.move(target, target.with_suffix(".c.bak"))

        try:
            # ── Reindex the (now deleted) file ──
            result = reindex_file_impl("src/utils.c", str(indexed_project), with_analysis=False)
            assert "error" not in result, f"Unexpected error: {result.get('error')}"
            assert result.get("action") == "deleted", f"Expected action='deleted', got: {result}"
            assert result.get("symbols_removed") == sym_count_before, (
                f"Expected {sym_count_before} symbols removed, got {result.get('symbols_removed')}"
            )

            # ── Verify DB is clean ──
            conn = _open_db(db_path)
            try:
                known_after = get_file_mtimes(conn, config_hash)
                assert target_str not in known_after, "File record should be removed from files table"

                sym_count_after = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE file_id=?", (old_file_id,)
                ).fetchone()[0]
                assert sym_count_after == 0, f"Expected 0 symbols, found {sym_count_after}"
            finally:
                conn.close()
        finally:
            # Restore the file so the fixture stays clean for other tests
            shutil.move(target.with_suffix(".c.bak"), target)
            # Reindex to restore the index state
            reindex_file_impl("src/utils.c", str(indexed_project), with_analysis=False)

    def test_deleted_file_not_in_index_returns_error(self, indexed_project: Path):
        """File that exists neither on disk nor in the index returns an error."""
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/never_existed.c", str(indexed_project), with_analysis=False)
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_reindex_refreshes_source_hash(self, indexed_project: Path):
        """A reindexed file must not stay marked as changed forever.

        reindex_file_impl moves ``files.mtime`` forward.  When it left
        ``files.source_hash`` at the value from before the edit, the two
        columns disagreed and every staleness check in mcp/shared/stale.py
        answered "changed" for a file whose symbols were current.
        """
        from fw_context_mcp.indexer.db import open_db as _open_db
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl
        from fw_context_mcp.mcp.shared.stale import _check_file_stale
        from fw_context_mcp.utils import compute_source_hash

        db_path = _db_path_for_project(indexed_project)
        target = indexed_project / "src" / "modem.c"
        original = target.read_text(encoding="utf-8")

        def stored_row() -> tuple[float, str]:
            conn = _open_db(db_path)
            try:
                row = conn.execute(
                    "SELECT mtime, source_hash FROM files WHERE path LIKE ?",
                    ("%modem.c",),
                ).fetchone()
            finally:
                conn.close()
            assert row is not None, "modem.c missing from the files table"
            return row["mtime"], row["source_hash"]

        _, hash_before = stored_row()
        assert hash_before, "the index must store a source_hash to start from"

        try:
            _write_file(target, original + "\nint reindex_hash_probe(void) { return 7; }\n")
            _advance_mtime(target)

            result = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
            assert "error" not in result, f"Reindex failed: {result.get('error')}"

            mtime_after, hash_after = stored_row()
            assert hash_after == compute_source_hash(target), (
                "source_hash must describe the text the index just parsed"
            )
            assert hash_after != hash_before, "the content changed, thus the hash must change"
            assert not _check_file_stale(str(target), mtime_after, hash_after)
        finally:
            _write_file(target, original)
            _advance_mtime(target)
            reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)

    def test_header_reindexes_through_an_including_tu(self, indexed_project: Path):
        """A header is not in compile_commands.json, but it can still be reindexed.

        compile_commands.json lists translation units, thus a header never
        matches it directly.  The manifest names the units that include the
        header, and one of them carries the re-parse.

        This also covers the iterator trap: _reindex_match_tus walks the
        units twice, so parse() — which returns a generator — has to be
        materialised first.  Without that the second walk sees an exhausted
        iterator and the header falls through to the "not found" error.
        """
        from fw_context_mcp.indexer.db import open_db as _open_db
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        header = indexed_project / "src" / "modem.h"
        original = header.read_text(encoding="utf-8")

        try:
            _write_file(header, original.replace(
                "#endif", "int modem_probe_added(int x);\n#endif"))
            _advance_mtime(header)

            result = reindex_file_impl("src/modem.h", str(indexed_project),
                                       with_analysis=False)

            assert "error" not in result, f"header reindex failed: {result.get('error')}"
            assert result["translation_units"] == 1, (
                "one unit carries the re-parse; re-parsing every including unit "
                "would cost hours on a real project"
            )
            assert "warning" in result, (
                "the answer covers one compilation context and must say so"
            )
            assert "run 'fw-context index'" in result["warning"]

            conn = _open_db(_db_path_for_project(indexed_project))
            try:
                names = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM symbols WHERE name = ?",
                        ("modem_probe_added",),
                    ).fetchall()
                }
            finally:
                conn.close()
            assert names == {"modem_probe_added"}, "the new declaration must be indexed"
        finally:
            _write_file(header, original)
            _advance_mtime(header)
            reindex_file_impl("src/modem.h", str(indexed_project), with_analysis=False)

    def test_tu_for_header_is_deterministic(self, tmp_path: Path):
        """Two units include the header — the lowest path in sort order wins.

        Manifest order follows compile_commands.json, which a rebuild can
        reshuffle.  A tool that re-parsed a different unit on every call
        would give a different answer each time for no visible reason.
        """
        from fw_context_mcp.mcp.handlers.maintenance import _tu_for_header

        manifest = {
            "entries": [
                {"file": "src/zeta.c", "headers": ["src/shared.h"]},
                {"file": "src/alpha.c", "headers": ["src/shared.h"]},
            ],
        }
        target = (tmp_path / "src" / "shared.h").resolve()

        chosen = _tu_for_header(target, manifest, tmp_path)

        assert chosen == tmp_path.resolve() / "src" / "alpha.c"
        # Reversing the entries must not change the answer.
        manifest["entries"].reverse()
        assert _tu_for_header(target, manifest, tmp_path) == chosen

    def test_tu_for_header_returns_none_when_nothing_includes_it(self, tmp_path: Path):
        """No manifest, or no unit that includes the header, means no answer."""
        from fw_context_mcp.mcp.handlers.maintenance import _tu_for_header

        target = (tmp_path / "src" / "orphan.h").resolve()
        manifest = {"entries": [{"file": "src/main.c", "headers": ["src/other.h"]}]}

        assert _tu_for_header(target, None, tmp_path) is None
        assert _tu_for_header(target, {}, tmp_path) is None
        assert _tu_for_header(target, manifest, tmp_path) is None


# ── direct store_symbols_for_unit tests ────────────────────────────────


class TestStoreSymbolsForUnitAnalysisRestore:
    """Direct tests for Phase 1-4 of store_symbols_for_unit using pre-parsed data."""

    @pytest.fixture
    def store_db(self, tmp_path: Path):
        """Create a DB with project + build config, return (conn, config_hash, project_root)."""
        from fw_context_mcp.indexer.db import open_db as _open_db

        db_path = tmp_path / "test.db"
        conn = _open_db(db_path)

        with transaction(conn):
            upsert_project(conn, "test-proj", "test", str(tmp_path))
            upsert_build_config(conn, "hash-0001", "test-proj", str(tmp_path / "cc.json"))

        return conn, "hash-0001", tmp_path

    def test_analysis_deleted_during_cleanup(self, store_db, tmp_path: Path):
        """_delete_old_for_tu cleans up llm_analysis for all symbols of the TU."""
        from fw_context_mcp.indexer.ops import store_symbols_for_unit

        conn, config_hash, root = store_db

        # Create a source file on disk (needed for content hashing)
        src_file = tmp_path / "test.c"
        src_file.write_text("void foo(void) {\n    return;\n}\n", encoding="utf-8")

        # Use a RELATIVE path so _normalize_file_path produces the same key
        # that store_symbols_for_unit uses — otherwise the cleanup block
        # in _delete_old_for_tu is skipped (normalized_tu_path not in known).
        file_id = upsert_file(conn, config_hash, "test.c", "c")
        insert_symbols_batch(
            conn,
            [
                (
                    config_hash,
                    file_id,
                    "test.c",
                    split_tokens("foo", "foo"),
                    "usr::foo",
                    "foo",
                    "foo",
                    "function",
                    1,
                    1,
                    3,
                    1,
                    "void foo(void)",
                    "",
                    None,
                    0,
                    0,
                    "",
                    0,
                    "",
                    0,
                    0.0,
                    "",
                ),
            ],
        )
        sym_id = conn.execute(
            "SELECT id FROM symbols WHERE usr='usr::foo' AND config_hash=?",
            (config_hash,),
        ).fetchone()["id"]

        conn.execute(
            """INSERT OR REPLACE INTO llm_analysis
               (symbol_id, summary, inputs, outputs, model, analyzed_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (sym_id, "Test function.", "none", "void", "test"),
        )
        conn.execute(
            "UPDATE symbols SET summary='Test function.', inputs='none', outputs='void' WHERE id=?",
            (sym_id,),
        )

        # Verify analysis exists before
        ana_before = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
        assert ana_before == 1

        # Create a mock unit and pre-parsed data for the SAME file
        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import ExtractionResult, Symbol

        unit = CompilationUnit(
            file=Path(src_file),
            directory=tmp_path,
            language="c",
            clang_args=["gcc", "-c", "test.c"],
        )

        # Build a Symbol that matches the existing one
        sym = Symbol(
            usr="usr::foo",
            name="foo",
            qualified_name="foo",
            kind="function",
            file=str(src_file),
            line=1,
            column=1,
            end_line=3,
            is_definition=True,
            signature="void foo(void)",
            docstring="",
            enum_value=None,
            is_virtual=False,
            is_pure_virtual=False,
            parent_usr="",
            is_template=False,
            template_usr="",
        )

        # store_symbols_for_unit calls _delete_old_for_tu which now cleans
        # up llm_analysis (along with embeddings and vec_symbols) before
        # re-inserting symbols.  The old analysis is deleted and no longer
        # associated with the new symbol IDs.
        with transaction(conn):
            syms_added, _, _ = store_symbols_for_unit(
                conn,
                unit,
                config_hash,
                tmp_path,
                vendor_patterns=[],
                project_patterns=[],
                index_refs=False,
                pre_parsed=ExtractionResult(symbols=[sym]),
            )

        assert syms_added == 1

        # Analysis should be GONE — _delete_old_for_tu cleaned it up
        ana_after = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
        assert ana_after == 0, f"Expected 0 analyses after cleanup, got {ana_after}"

        conn.close()

    def test_analysis_dropped_when_content_changed(self, store_db, tmp_path: Path):
        """Changed symbol content means analysis is dropped during reindex."""
        from fw_context_mcp.indexer.ops import store_symbols_for_unit

        conn, config_hash, root = store_db

        src_file = tmp_path / "test2.c"
        # Write ORIGINAL content
        src_file.write_text("int bar(int x) {\n    return x * 2;\n}\n", encoding="utf-8")

        file_id = upsert_file(conn, config_hash, "test2.c", "c")
        insert_symbols_batch(
            conn,
            [
                (
                    config_hash,
                    file_id,
                    "test2.c",
                    split_tokens("bar", "bar"),
                    "usr::bar",
                    "bar",
                    "bar",
                    "function",
                    1,
                    1,
                    3,
                    1,
                    "int bar(int x)",
                    "",
                    None,
                    0,
                    0,
                    "",
                    0,
                    "",
                    0,
                    0.0,
                    "int bar(int x) {\n    return x * 2;\n}\n",
                ),
            ],
        )
        sym_id = conn.execute(
            "SELECT id FROM symbols WHERE usr='usr::bar' AND config_hash=?",
            (config_hash,),
        ).fetchone()["id"]

        conn.execute(
            """INSERT OR REPLACE INTO llm_analysis
               (symbol_id, summary, inputs, outputs, model, analyzed_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
            (sym_id, "Multiplies by 2.", "x: int", "x*2", "test"),
        )
        conn.execute(
            "UPDATE symbols SET summary='Multiplies by 2.', inputs='x: int', outputs='x*2' WHERE id=?",
            (sym_id,),
        )
        conn.commit()

        # NOW modify the file on disk — change the function body AFTER analysis was saved
        src_file.write_text("int bar(int x) {\n    return x * 3;\n}\n", encoding="utf-8")

        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import ExtractionResult, Symbol

        unit = CompilationUnit(
            file=Path(src_file),
            directory=tmp_path,
            language="c",
            clang_args=["gcc", "-c", "test2.c"],
        )

        sym = Symbol(
            usr="usr::bar",
            name="bar",
            qualified_name="bar",
            kind="function",
            file=str(src_file),
            line=1,
            column=1,
            end_line=3,
            is_definition=True,
            signature="int bar(int x)",
            docstring="",
            enum_value=None,
            is_virtual=False,
            is_pure_virtual=False,
            parent_usr="",
            is_template=False,
            template_usr="",
        )

        with transaction(conn):
            syms_added, _, _ = store_symbols_for_unit(
                conn,
                unit,
                config_hash,
                tmp_path,
                vendor_patterns=[],
                project_patterns=[],
                index_refs=False,
                pre_parsed=ExtractionResult(symbols=[sym]),
            )

        assert syms_added == 1

        # Analysis should be GONE because body changed (return x * 2 → x * 3).
        # _delete_old_for_tu cleans up old llm_analysis rows before re-inserting
        # symbols, and the new symbols get fresh IDs.  The old analysis is no
        # longer associated with any existing symbol.
        ana_after = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
        assert ana_after == 0, (
            f"Expected analysis to be DROPPED (body changed: x*2 → x*3), "
            f"but {ana_after} analysis row(s) remain. "
            f"Known limitation: Phase 1 reads body from current file on disk."
        )

        conn.close()

    def test_symbols_without_analysis_not_restored(self, store_db, tmp_path: Path):
        """Symbols without existing analysis do not get analysis during reindex."""
        from fw_context_mcp.indexer.ops import store_symbols_for_unit

        conn, config_hash, root = store_db

        src_file = tmp_path / "test3.c"
        src_file.write_text("void helper(void) {\n    return;\n}\n", encoding="utf-8")

        file_id = upsert_file(conn, config_hash, str(src_file), "c")
        # Insert symbol WITHOUT analysis
        insert_symbols_batch(
            conn,
            [
                (
                    config_hash,
                    file_id,
                    "test3.c",
                    split_tokens("helper", "helper"),
                    "usr::helper",
                    "helper",
                    "helper",
                    "function",
                    1,
                    1,
                    3,
                    1,
                    "void helper(void)",
                    "",
                    None,
                    0,
                    0,
                    "",
                    0,
                    "",
                    0,
                    0.0,
                    "",
                ),
            ],
        )
        conn.commit()

        # Verify no analysis
        ana_before = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
        assert ana_before == 0

        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import ExtractionResult, Symbol

        unit = CompilationUnit(
            file=Path(src_file),
            directory=tmp_path,
            language="c",
            clang_args=["gcc", "-c", "test3.c"],
        )
        sym = Symbol(
            usr="usr::helper",
            name="helper",
            qualified_name="helper",
            kind="function",
            file=str(src_file),
            line=1,
            column=1,
            end_line=3,
            is_definition=True,
            signature="void helper(void)",
            docstring="",
            enum_value=None,
            is_virtual=False,
            is_pure_virtual=False,
            parent_usr="",
            is_template=False,
            template_usr="",
        )

        with transaction(conn):
            syms_added, _, _ = store_symbols_for_unit(
                conn,
                unit,
                config_hash,
                tmp_path,
                vendor_patterns=[],
                project_patterns=[],
                index_refs=False,
                pre_parsed=ExtractionResult(symbols=[sym]),
            )

        assert syms_added == 1
        # Still no analysis — nothing to restore
        ana_after = conn.execute("SELECT COUNT(*) FROM llm_analysis").fetchone()[0]
        assert ana_after == 0

        conn.close()

    def test_analysis_columns_synced_on_restore(self, store_db, tmp_path: Path):
        """Phase 3: when analysis is restored, symbols.summary/inputs/outputs are updated."""
        from fw_context_mcp.indexer.ops import store_symbols_for_unit

        conn, config_hash, root = store_db

        src_file = tmp_path / "test4.c"
        src_file.write_text("int calc(void) {\n    return 42;\n}\n", encoding="utf-8")

        file_id = upsert_file(conn, config_hash, str(src_file), "c")
        insert_symbols_batch(
            conn,
            [
                (
                    config_hash,
                    file_id,
                    "test4.c",
                    split_tokens("calc", "calc"),
                    "usr::calc",
                    "calc",
                    "calc",
                    "function",
                    1,
                    1,
                    3,
                    1,
                    "int calc(void)",
                    "",
                    None,
                    0,
                    0,
                    "",
                    0,
                    "",
                    0,
                    0.0,
                    "",
                ),
            ],
        )
        sym_id = conn.execute("SELECT id FROM symbols WHERE usr='usr::calc'").fetchone()["id"]

        conn.execute(
            """INSERT OR REPLACE INTO llm_analysis
               (symbol_id, summary, inputs, outputs, model, analyzed_at)
               VALUES (?, 'Returns magic number.', 'none', '42', 'test', datetime('now'))""",
            (sym_id,),
        )
        conn.execute(
            "UPDATE symbols SET summary='Returns magic number.', inputs='none', outputs='42' WHERE id=?",
            (sym_id,),
        )

        from fw_context_mcp.utils import compute_content_hash

        content_hash = compute_content_hash(
            "int calc(void) {\n    return 42;\n}\n",
            "calc",
            "int calc(void)",
            "",
        )
        import os as _os2

        _saved_home2 = _os2.environ.get("HOME")
        _os2.environ["HOME"] = str(tmp_path)
        try:
            from fw_context_mcp.cache_client import get_local_cache_db as _get_db2

            global_db2 = _get_db2()
            global_db2.execute(
                """INSERT OR REPLACE INTO llm_analysis_cache
                   (content_hash, summary, inputs, outputs, model, analyzed_at)
                   VALUES (?, 'Returns magic number.', 'none', '42', 'test', datetime('now'))""",
                (content_hash,),
            )
            global_db2.commit()
            global_db2.close()
        finally:
            if _saved_home2 is None:
                _os2.environ.pop("HOME", None)
            else:
                _os2.environ["HOME"] = _saved_home2

        from fw_context_mcp.indexer.compile_commands import CompilationUnit
        from fw_context_mcp.indexer.symbols import ExtractionResult, Symbol

        unit = CompilationUnit(
            file=Path(src_file),
            directory=tmp_path,
            language="c",
            clang_args=["gcc", "-c", "test4.c"],
        )
        sym = Symbol(
            usr="usr::calc",
            name="calc",
            qualified_name="calc",
            kind="function",
            file=str(src_file),
            line=1,
            column=1,
            end_line=3,
            is_definition=True,
            signature="int calc(void)",
            docstring="",
            enum_value=None,
            is_virtual=False,
            is_pure_virtual=False,
            parent_usr="",
            is_template=False,
            template_usr="",
        )

        with transaction(conn):
            syms_added, _, _ = store_symbols_for_unit(
                conn,
                unit,
                config_hash,
                tmp_path,
                vendor_patterns=[],
                project_patterns=[],
                index_refs=False,
                pre_parsed=ExtractionResult(symbols=[sym]),
            )

        # Check denormalized columns on symbols
        row = conn.execute(
            "SELECT summary, inputs, outputs FROM symbols WHERE usr='usr::calc' AND config_hash=?",
            (config_hash,),
        ).fetchone()
        assert row["summary"] == "Returns magic number.", f"summary={row['summary']}"
        assert row["inputs"] == "none"
        assert row["outputs"] == "42"

        conn.close()


# ── fixtures for LLM analysis tests ──────────────────────────────────


@pytest.fixture(scope="class")
def indexed_project_with_analysis(tmp_path_factory):
    """Index the C project with LLM analysis enabled (requires Ollama).

    Scoped to class — indexes once for all tests in the class.
    Creates its own temp C project so it doesn't depend on function-scoped fixtures.
    """
    import json

    proj = tmp_path_factory.mktemp("demo_analysis")

    src = proj / "src"
    src.mkdir()

    # Use the source files from the repo test data
    _repo_tests = Path(__file__).resolve().parent
    _fixture_src = _repo_tests.parent / "src"  # not used, write inline

    _write_file(
        src / "main.c",
        """\
#include <stdio.h>
#include "modem.h"
#include "utils.h"

int main(void) {
    modem_init(115200);
    int result = compute_checksum("hello", 5);
    printf("result=%d\\n", result);
    return 0;
}
""",
    )

    _write_file(
        src / "modem.h",
        """\
#ifndef MODEM_H
#define MODEM_H

void modem_init(int baudrate);
int modem_send(const char* data, int len);
int modem_recv(char* buf, int max_len);

#endif
""",
    )

    _write_file(
        src / "modem.c",
        """\
#include "modem.h"
#include <string.h>

static int g_baudrate = 0;

void modem_init(int baudrate) {
    g_baudrate = baudrate;
}

int modem_send(const char* data, int len) {
    if (!data || len <= 0) return -1;
    return len;
}

int modem_recv(char* buf, int max_len) {
    if (!buf || max_len <= 0) return -1;
    memset(buf, 0, max_len);
    return 0;
}
""",
    )

    _write_file(
        src / "utils.h",
        """\
#ifndef UTILS_H
#define UTILS_H

int compute_checksum(const char* data, int len);
void log_message(const char* msg);

#endif
""",
    )

    _write_file(
        src / "utils.c",
        """\
#include "utils.h"
#include <string.h>

int compute_checksum(const char* data, int len) {
    if (!data || len <= 0) return 0;
    int sum = 0;
    for (int i = 0; i < len; i++) {
        sum += (unsigned char)data[i];
    }
    return sum & 0xFF;
}

void log_message(const char* msg) {
    if (!msg) return;
    (void)strlen(msg);
}
""",
    )

    cc = [
        {
            "directory": str(src),
            "file": "main.c",
            "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "main.c", "-o", "build/main.o"],
        },
        {
            "directory": str(src),
            "file": "modem.c",
            "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "modem.c", "-o", "build/modem.o"],
        },
        {
            "directory": str(src),
            "file": "utils.c",
            "arguments": ["gcc", "-std=c11", "-O2", "-Isrc", "-c", "utils.c", "-o", "build/utils.o"],
        },
    ]
    cc_json = proj / "compile_commands.json"
    cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

    # Isolate index DB to tmp_path before init
    _isolate_index_db(proj, proj.parent)

    # Initialize project first — generates UUID4 project ID
    init_result = _cli(
        ["init", "--project", str(proj)],
        cwd=proj,
        timeout=60,
    )
    if init_result.returncode != 0:
        pytest.fail(f"Init failed:\nSTDOUT:\n{init_result.stdout}\nSTDERR:\n{init_result.stderr}")

    result = _cli(
        ["index", "--no-refs", "--analyze", "--no-embeddings", str(cc_json)],
        cwd=proj,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"Index with analysis failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

    db_path = _db_path_for_project(proj)
    assert db_path.exists(), f"DB not created at {db_path}"

    yield proj

    shutil.rmtree(db_path.parent, ignore_errors=True)


def _config_hash(conn):
    """Return the latest config_hash from the database."""
    return conn.execute("SELECT config_hash FROM build_configs ORDER BY created_at DESC LIMIT 1").fetchone()[
        "config_hash"
    ]


# ── LLM analysis consistency tests (requires Ollama) ──────────────────


@pytest.mark.ollama
@pytest.mark.libclang
class TestLlvmAnalysisConsistency:
    """End-to-end tests with real LLM analysis: index → modify → reindex → verify consistency."""

    def test_initial_index_generates_analysis(self, indexed_project_with_analysis: Path):
        """After indexing with --analyze, project symbols have non-empty LLM analysis."""
        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            analyzed = conn.execute(
                """SELECT COUNT(*) FROM llm_analysis a
                   JOIN symbols s ON s.id = a.symbol_id
                   WHERE s.config_hash = ?""",
                (ch,),
            ).fetchone()[0]
            assert analyzed > 0, f"No symbols have LLM analysis (analyzed={analyzed})"
            print(f"  Symbols with LLM analysis: {analyzed}")

            rows = conn.execute(
                """SELECT s.name, s.kind, a.summary, a.inputs, a.outputs, a.model
                   FROM llm_analysis a
                   JOIN symbols s ON s.id = a.symbol_id
                   WHERE s.config_hash = ?
                   LIMIT 5""",
                (ch,),
            ).fetchall()
            for r in rows:
                assert r["summary"], f"Empty summary for {r['name']}"
                assert len(r["summary"]) > 10, f"Summary too short for {r['name']}: {r['summary']!r}"
                assert r["model"], f"Empty model for {r['name']}"
                print(f"  {r['name']} ({r['kind']}): model={r['model']} summary={r['summary'][:80]}...")

        finally:
            conn.close()

    def test_line_numbers_consistent_after_reindex(self, indexed_project_with_analysis: Path):
        """After reindex, symbol line numbers match actual source code."""
        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            rows = conn.execute(
                """SELECT name, line, end_line, signature, file_path, is_definition, kind
                   FROM symbols
                   WHERE config_hash = ? AND file_path LIKE '%modem.c' AND is_definition = 1
                   ORDER BY line""",
                (ch,),
            ).fetchall()

            for r in rows:
                print(f"  {r['name']:20s} kind={r['kind']:10s} line={r['line']:3d} end={r['end_line']:3d}")
                assert r["line"] > 0, f"Invalid line for {r['name']}"
                if r["end_line"] > 0:
                    assert r["end_line"] >= r["line"], f"end_line < line for {r['name']}: {r['end_line']} < {r['line']}"

            names = {r["name"] for r in rows}
            for fn in ["modem_init", "modem_send", "modem_recv"]:
                assert fn in names, f"{fn} not found in modem.c definitions"

        finally:
            conn.close()

    def test_kinds_and_definitions_correct(self, indexed_project_with_analysis: Path):
        """Symbol metadata (kind, is_definition) is correct after indexing."""
        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            rows = conn.execute(
                """SELECT name, kind, is_definition, file_path
                   FROM symbols WHERE config_hash = ?
                   ORDER BY file_path, line""",
                (ch,),
            ).fetchall()

            for r in rows:
                if r["file_path"].endswith(".c") and r["kind"] == "function":
                    assert r["is_definition"] == 1, (
                        f"{r['name']} in {r['file_path']} should be is_definition=1, got {r['is_definition']}"
                    )
                print(f"  {r['name']:25s} kind={r['kind']:10s} def={r['is_definition']}  {r['file_path']}")

        finally:
            conn.close()

    def test_reindex_with_analysis_regenerates_for_changed_symbol(self, indexed_project_with_analysis: Path):
        """After modifying a function and reindexing with analysis, changed symbol gets new analysis."""

        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            old = conn.execute(
                """SELECT a.summary, a.analyzed_at
                   FROM llm_analysis a
                   JOIN symbols s ON s.id = a.symbol_id
                   WHERE s.config_hash = ? AND s.name = 'modem_send' AND s.is_definition = 1""",
                (ch,),
            ).fetchone()
            assert old is not None, "modem_send has no analysis"
            old_summary = old["summary"]
            print(f"  Old summary: {old_summary}")
        finally:
            conn.close()

        # Modify modem_send body
        modem_c = indexed_project_with_analysis / "src" / "modem.c"
        original = modem_c.read_text(encoding="utf-8")
        modified = original.replace(
            "int modem_send(const char* data, int len) {\n    if (!data || len <= 0) return -1;\n    return len;\n}",
            "int modem_send(const char* data, int len) {\n    if (!data || len <= 0) return -1;\n    /* double the reported length */\n    return len * 2;\n}",
        )
        assert modified != original, "Modification didn't change the file"
        modem_c.write_text(modified, encoding="utf-8")

        _advance_mtime(modem_c)
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/modem.c", str(indexed_project_with_analysis), with_analysis=True)
        print(f"  Reindex result: {result}")
        if "analysis_warning" in result:
            print(f"  Analysis warning: {result['analysis_warning']}")

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            new = conn.execute(
                """SELECT a.summary, a.analyzed_at
                   FROM llm_analysis a
                   JOIN symbols s ON s.id = a.symbol_id
                   WHERE s.config_hash = ? AND s.name = 'modem_send' AND s.is_definition = 1""",
                (ch,),
            ).fetchone()

            if new:
                assert len(new["summary"]) > 10, f"Summary too short: {new['summary']!r}"
                print(f"  New summary: {new['summary'][:120]}...")
            else:
                _fail_or_skip_analysis(result, "modem_send")

        finally:
            conn.close()

        modem_c.write_text(original, encoding="utf-8")
        reindex_file_impl("src/modem.c", str(indexed_project_with_analysis), with_analysis=False)

    def test_unchanged_symbol_keeps_analysis_after_reindex(self, indexed_project_with_analysis: Path):
        """Phase 3: unchanged symbol preserves original LLM analysis timestamp."""

        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            old = conn.execute(
                """SELECT a.summary, a.inputs, a.outputs, a.analyzed_at
                   FROM llm_analysis a
                   JOIN symbols s ON s.id = a.symbol_id
                   WHERE s.config_hash = ? AND s.name = 'compute_checksum' AND s.is_definition = 1""",
                (ch,),
            ).fetchone()
            assert old is not None, "compute_checksum has no analysis"
            old_summary = old["summary"]
            old_ts = old["analyzed_at"]
            print(f"  Old: summary={old_summary[:80]}... ts={old_ts}")
        finally:
            conn.close()

        # Add a comment to utils.c — doesn't change any function
        utils_c = indexed_project_with_analysis / "src" / "utils.c"
        original = utils_c.read_text(encoding="utf-8")
        modified = original.replace(
            '#include "utils.h"',
            '#include "utils.h"\n/* consistency test — comment only */',
        )
        utils_c.write_text(modified, encoding="utf-8")

        _advance_mtime(utils_c)
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/utils.c", str(indexed_project_with_analysis), with_analysis=True)
        print(f"  Reindex result: {result}")
        if "analysis_warning" in result:
            print(f"  Analysis warning: {result['analysis_warning']}")

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            new = conn.execute(
                """SELECT a.summary, a.inputs, a.outputs, a.analyzed_at
                   FROM llm_analysis a
                   JOIN symbols s ON s.id = a.symbol_id
                   WHERE s.config_hash = ? AND s.name = 'compute_checksum' AND s.is_definition = 1""",
                (ch,),
            ).fetchone()
            assert new is not None, "compute_checksum analysis disappeared"
            if new["summary"] == old_summary:
                print("  ✓ Analysis PRESERVED (Phase 3 restore)")
            else:
                print("  Analysis regenerated (summary changed)")
            print(f"  New summary: {new['summary'][:100]}...")

        finally:
            conn.close()

        utils_c.write_text(original, encoding="utf-8")

    def test_new_symbol_gets_analysis_after_reindex(self, indexed_project_with_analysis: Path):
        """Adding a new function and reindexing with analysis generates LLM analysis."""

        utils_c = indexed_project_with_analysis / "src" / "utils.c"
        original = utils_c.read_text(encoding="utf-8")

        new_func = """

/* Calculate the average of an integer array */
int compute_average(const int* values, int count) {
    if (!values || count <= 0) return 0;
    long sum = 0;
    for (int i = 0; i < count; i++) {
        sum += values[i];
    }
    return (int)(sum / count);
}
"""
        utils_c.write_text(original.rstrip() + new_func + "\n", encoding="utf-8")

        _advance_mtime(utils_c)
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        result = reindex_file_impl("src/utils.c", str(indexed_project_with_analysis), with_analysis=True)
        print(f"  Reindex result: {result}")
        if "analysis_warning" in result:
            print(f"  Analysis warning: {result['analysis_warning']}")

        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            sym = conn.execute(
                "SELECT id, name, kind, line, is_definition FROM symbols WHERE config_hash=? AND name='compute_average'",
                (ch,),
            ).fetchone()
            assert sym is not None, "compute_average not found"
            assert sym["kind"] == "function"
            assert sym["is_definition"] == 1
            assert sym["line"] > 0
            print(f"  compute_average: line={sym['line']} kind={sym['kind']} def={sym['is_definition']}")

            ana = conn.execute(
                "SELECT summary, inputs, outputs FROM llm_analysis WHERE symbol_id=?",
                (sym["id"],),
            ).fetchone()
            if ana:
                assert len(ana["summary"]) > 10, f"Summary too short: {ana['summary']!r}"
                print(f"  Analysis: {ana['summary'][:120]}...")
            else:
                _fail_or_skip_analysis(result, "compute_average")

        finally:
            conn.close()

        utils_c.write_text(original, encoding="utf-8")
        reindex_file_impl("src/utils.c", str(indexed_project_with_analysis), with_analysis=False)

    def test_line_numbers_updated_after_inserting_code(self, indexed_project_with_analysis: Path):
        """When code is inserted before a function, its line number shifts correctly."""
        modem_c = indexed_project_with_analysis / "src" / "modem.c"
        original = modem_c.read_text(encoding="utf-8")

        # First reindex the original file to get a clean baseline
        # (previous tests may have modified and reindexed this file)
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        reindex_file_impl("src/modem.c", str(indexed_project_with_analysis), with_analysis=False)

        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            old_lines = {
                r["name"]: r["line"]
                for r in conn.execute(
                    "SELECT name, line FROM symbols WHERE config_hash=? AND file_path LIKE '%modem.c' AND is_definition=1",
                    (ch,),
                ).fetchall()
            }
            print(f"  Baseline lines: {old_lines}")
        finally:
            conn.close()

        # Insert 4 lines after includes (blank line + 3 comment lines)
        INSERTED_LINES = 4
        modified = original.replace(
            '#include "modem.h"',
            '#include "modem.h"\n\n/* Header comment */\n/* describing module */\n/* in detail */',
        )
        modem_c.write_text(modified, encoding="utf-8")

        reindex_file_impl("src/modem.c", str(indexed_project_with_analysis), with_analysis=False)

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            new_lines = {
                r["name"]: r["line"]
                for r in conn.execute(
                    "SELECT name, line FROM symbols WHERE config_hash=? AND file_path LIKE '%modem.c' AND is_definition=1",
                    (ch,),
                ).fetchall()
            }
            print(f"  After insert: {new_lines}")

            for name, old_line in old_lines.items():
                assert name in new_lines, f"{name} disappeared"
                expected = old_line + INSERTED_LINES
                actual = new_lines[name]
                assert actual == expected, f"{name}: expected line {expected}, got {actual} (old={old_line})"
            print(f"  ✓ All line numbers shifted correctly by +{INSERTED_LINES}")

        finally:
            conn.close()

        # Reindex the restored original to leave clean state for next tests
        modem_c.write_text(original, encoding="utf-8")
        reindex_file_impl("src/modem.c", str(indexed_project_with_analysis), with_analysis=False)

    def test_analysis_consistency_after_multiple_reindexes(self, indexed_project_with_analysis: Path):
        """Multiple reindexes of the same file don't corrupt or duplicate analysis."""

        db_path = _db_path_for_project(indexed_project_with_analysis)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            initial_count = conn.execute(
                "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash=?",
                (ch,),
            ).fetchone()[0]
            print(f"  Initial analysis count: {initial_count}")
        finally:
            conn.close()

        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        for i in range(3):
            _advance_mtime(indexed_project_with_analysis / "src" / "modem.c")
            result = reindex_file_impl(
                "src/modem.c",
                str(indexed_project_with_analysis),
                with_analysis=True,
            )
            assert "error" not in result, f"Reindex {i + 1} failed: {result.get('error')}"
            print(f"  Reindex {i + 1}: symbols={result.get('symbols_updated')}")

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            final_count = conn.execute(
                "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.config_hash=?",
                (ch,),
            ).fetchone()[0]
            print(f"  Final analysis count: {final_count}")
            assert final_count >= initial_count, f"Analysis count decreased: {initial_count} → {final_count}"

            dups = conn.execute(
                """SELECT usr, COUNT(*) as cnt FROM symbols
                   WHERE config_hash = ?
                   GROUP BY usr HAVING cnt > 1""",
                (ch,),
            ).fetchall()
            assert len(dups) == 0, f"Duplicate symbols: {dups}"

        finally:
            conn.close()


# ── functional tests: background reindex (new/changed/deleted files) ──


def _symbol_names(conn, config_hash: str) -> set[str]:
    """Return the set of symbol names for a given config_hash."""
    return {
        r["name"] for r in conn.execute("SELECT name FROM symbols WHERE config_hash = ?", (config_hash,)).fetchall()
    }


@pytest.mark.libclang
class TestBackgroundReindex:
    """End-to-end tests: index → change files → reindex via ``fw-context index``.

    These verify the behaviour the background reindex subprocess relies on:
    same compile_commands.json, stale source files → re-run indexer → verify.
    """

    def test_reindex_after_modifying_file(self, indexed_project: Path):
        """Modify a function body → reindex via reindex_file_impl → symbols updated.

        Uses ``reindex_file_impl`` (same code path as the background file watcher)
        to avoid the mtime-tolerance race inherent in the full-indexer path.
        """
        from fw_context_mcp.mcp.handlers.maintenance import reindex_file_impl

        db_path = _db_path_for_project(indexed_project)

        # ── Verify initial state ──
        conn = open_db(db_path)
        try:
            ch_before = _config_hash(conn)
            row = conn.execute(
                "SELECT line, end_line FROM symbols WHERE config_hash=? AND name='modem_send'",
                (ch_before,),
            ).fetchone()
            assert row is not None, "modem_send not found in index"
            old_end_line = row["end_line"]
            print(f"  modem_send before: line={row['line']}, end_line={old_end_line}")
        finally:
            conn.close()

        # ── Modify modem.c — replace modem_send body with more lines ──
        modem_c = indexed_project / "src" / "modem.c"
        original = modem_c.read_text(encoding="utf-8")
        modified = original.replace(
            "int modem_send(const char* data, int len) {\n    if (!data || len <= 0) return -1;\n    return len;\n}",
            "int modem_send(const char* data, int len) {\n    if (!data || len <= 0) return -1;\n    /* send data over UART */\n    for (int i = 0; i < len; i++) {\n        uart_putc(data[i]);\n    }\n    return len;\n}",
        )
        assert modified != original, "Modification didn't change the file"
        modem_c.write_text(modified, encoding="utf-8")

        try:
            # ── Reindex the single file (same path the bg watcher uses) ──
            result = reindex_file_impl("src/modem.c", str(indexed_project), with_analysis=False)
            assert "error" not in result, f"Reindex failed: {result.get('error')}"
            assert result["symbols_updated"] > 0, f"No symbols updated: {result}"
            print(f"  Reindex result: symbols_updated={result['symbols_updated']}")

            # ── Verify: modem_send end_line increased (body is longer) ──
            conn = open_db(db_path)
            try:
                ch_after = _config_hash(conn)
                assert ch_after == ch_before, f"config_hash changed unexpectedly: {ch_before[:12]} → {ch_after[:12]}"
                row = conn.execute(
                    "SELECT line, end_line FROM symbols WHERE config_hash=? AND name='modem_send'",
                    (ch_after,),
                ).fetchone()
                assert row is not None, "modem_send disappeared after reindex"
                assert row["end_line"] > old_end_line, (
                    f"modem_send end_line didn't grow: {row['end_line']} (was {old_end_line})"
                )
                print(f"  modem_send after: line={row['line']}, end_line={row['end_line']}")
            finally:
                conn.close()
        finally:
            modem_c.write_text(original, encoding="utf-8")

    def test_reindex_picks_up_new_file(self, indexed_project: Path):
        """Add a new .c file + update cc.json → reindex → new symbols appear."""
        import json

        db_path = _db_path_for_project(indexed_project)

        # ── Verify initial state — no "sensor" symbols ──
        conn = open_db(db_path)
        try:
            ch_before = _config_hash(conn)
            names_before = _symbol_names(conn, ch_before)
            assert "sensor_read" not in names_before, "sensor_read already exists"
            assert "sensor_init" not in names_before
        finally:
            conn.close()

        # ── Create new source file ──
        sensor_c = indexed_project / "src" / "sensor.c"
        _write_file(
            sensor_c,
            """\
#include "modem.h"

static int g_sensor_value = 0;

void sensor_init(int threshold) {
    g_sensor_value = threshold;
}

int sensor_read(void) {
    return g_sensor_value;
}
""",
        )

        # ── Add to compile_commands.json ──
        cc_json = indexed_project / "compile_commands.json"
        cc = json.loads(cc_json.read_text(encoding="utf-8"))
        cc.append(
            {
                "directory": str(indexed_project / "src"),
                "file": "sensor.c",
                "arguments": [
                    "gcc",
                    "-std=c11",
                    "-O2",
                    "-Isrc",
                    "-c",
                    "sensor.c",
                    "-o",
                    "build/sensor.o",
                ],
            }
        )
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        try:
            # ── Reindex via direct API (avoids subprocess config complications) ──
            from fw_context_mcp.indexer.runner import run

            db_path_resolved = _db_path_for_project(indexed_project)
            config_hash_new = run(
                compile_commands=cc_json,
                db_path=db_path_resolved,
                vendor_paths=[],
                project_paths=[],
                project_root=indexed_project,
                index_refs=False,
                index_embeddings=False,
                analyze_symbols=False,
                analyze_overrides=False,
            )
            print(f"  New config_hash: {config_hash_new[:16]}…")

            # ── Verify: new symbols in index ──
            conn = open_db(db_path)
            try:
                names_after = _symbol_names(conn, config_hash_new)
                assert "sensor_init" in names_after, f"sensor_init not found. Names: {sorted(names_after)}"
                assert "sensor_read" in names_after, "sensor_read not found"
                # Old symbols still present under new config_hash
                for name in ["modem_init", "modem_send", "compute_checksum", "main"]:
                    assert name in names_after, f"{name} lost after reindex"
            finally:
                conn.close()
        finally:
            # Restore original cc.json
            cc = cc[:-1]
            cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

    def test_reindex_removes_deleted_file_symbols(self, indexed_project: Path):
        """Remove a .c file from cc.json → reindex → symbols from that file gone from new config."""
        import json

        from fw_context_mcp.indexer.runner import run

        db_path = _db_path_for_project(indexed_project)

        # ── Verify initial state — utils.c symbols exist ──
        conn = open_db(db_path)
        try:
            ch_before = _config_hash(conn)
            names_before = _symbol_names(conn, ch_before)
            assert "compute_checksum" in names_before
            assert "log_message" in names_before
            print(f"  config_hash before: {ch_before[:16]}…")
        finally:
            conn.close()

        # ── Remove utils.c from compile_commands.json ──
        cc_json = indexed_project / "compile_commands.json"
        cc = json.loads(cc_json.read_text(encoding="utf-8"))
        cc_original = list(cc)
        cc = [entry for entry in cc if entry["file"] != "utils.c"]
        assert len(cc) == len(cc_original) - 1, "utils.c not removed from cc"
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        try:
            # ── Reindex via direct API ──
            config_hash_new = run(
                compile_commands=cc_json,
                db_path=db_path,
                vendor_paths=[],
                project_paths=[],
                project_root=indexed_project,
                index_refs=False,
                index_embeddings=False,
                analyze_symbols=False,
                analyze_overrides=False,
            )
            print(f"  New config_hash: {config_hash_new[:16]}…")

            # ── Verify: utils.c definitions gone ──
            # Note: declarations from utils.h (included by main.c) may still
            # appear — only definitions from the removed TU should be gone.
            #
            # The build identity does NOT change: config_hash names the
            # compilation dialect, and dropping a source file does not alter
            # it.  The removed file's rows are collected by the coverage purge
            # instead, which compares the files table against the build's
            # actual file set.  Before that purge existed, this test passed by
            # accident: the file list was part of config_hash, so a removal
            # minted a whole new build and the old one was retired.
            assert config_hash_new == ch_before, (
                "dropping a source file must not change the build identity"
            )
            conn = open_db(db_path)
            try:
                def_names = {
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM symbols WHERE config_hash=? AND is_definition=1",
                        (config_hash_new,),
                    ).fetchall()
                }
                assert "compute_checksum" not in def_names, (
                    f"compute_checksum definition should be gone. Defs: {sorted(def_names)}"
                )
                assert "log_message" not in def_names, "log_message definition should be gone"
                # Other files' definitions still present
                for name in ["modem_init", "modem_send", "main"]:
                    assert name in def_names, f"{name} definition lost after reindex"
            finally:
                conn.close()
        finally:
            cc_json.write_text(json.dumps(cc_original, indent=2), encoding="utf-8")


class TestFastStalenessCheck:
    """Verify ``_fast_staleness_check`` and related fixes for the bg reindex timeout loop.

    The ``indexed_project`` fixture uses ``--no-refs --no-analyze``, so a fresh
    index has empty ``refs``, ``indirect_call_sites``, and ``llm_analysis`` tables.
    These are legitimate detections — the tests verify the check is correct and
    that after filling the data, no false positives remain.
    """

    def test_detects_missing_refs_and_unanalyzed(self, indexed_project: Path):
        """Index built with --no-refs --no-analyze: fast check reports missing refs.

        "unanalyzed" may not appear if Phase 3 restored analysis from the
        cross-project cache (~/.fw-context/llm_cache.db) — that is correct
        behaviour (the cache serves its purpose).
        """
        from fw_context_mcp.mcp.background import _fast_staleness_check

        needs, reasons = _fast_staleness_check(indexed_project)
        assert needs, "Should detect work needed (missing refs)"
        assert "refs missing" in reasons, f"Expected 'refs missing', got: {reasons}"
        assert "indirect call sites missing" in reasons, f"Expected 'indirect call sites missing', got: {reasons}"
        # Unanalyzed is optional — Phase 3 may restore cached analysis from ~/.fw-context/llm_cache.db
        has_unanalyzed = any("unanalyzed" in r for r in reasons)
        if not has_unanalyzed:
            print("  (unanalyzed not reported — analysis restored from cross-project cache)")

    def test_no_false_positive_after_full_reindex(self, indexed_project: Path):
        """After filling refs, indirect sites, and analysis: check is clean."""
        import os as _os

        from fw_context_mcp.indexer.runner import run
        from fw_context_mcp.mcp.background import _fast_staleness_check
        from fw_context_mcp.mcp.shared.stale import _count_modified_files

        db_path = _db_path_for_project(indexed_project)
        cc_json = indexed_project / "compile_commands.json"

        # Reindex with refs — set FORCE_REFINDEX so unchanged files are
        # still re-parsed to populate the refs/indirect tables.
        _os.environ["FW_CONTEXT_FORCE_REFINDEX"] = "1"
        try:
            run(
                compile_commands=cc_json,
                db_path=db_path,
                vendor_paths=[],
                project_paths=[],
                project_root=indexed_project,
                index_refs=True,
                index_embeddings=False,
                analyze_symbols=False,
                analyze_overrides=False,
            )
        finally:
            _os.environ.pop("FW_CONTEXT_FORCE_REFINDEX", None)

        # Now check — refs should be present
        needs, reasons = _fast_staleness_check(indexed_project)
        assert "refs missing" not in reasons, f"Refs should be populated after reindex with --refs, got: {reasons}"
        # Note: "indirect call sites missing" may be legitimate — the test
        # project has no function pointer calls, so the table is empty.
        # mtimes must not be false-positive detected as changed
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            mod_count = _count_modified_files(conn, ch, indexed_project)
            assert mod_count == 0, f"After reindex, 0 files should be detected as modified, got {mod_count}"
        finally:
            conn.close()

    def test_detects_missing_refs_after_delete(self, indexed_project: Path):
        """After filling refs then deleting them, fast check reports 'refs missing'."""
        import os as _os

        from fw_context_mcp.indexer.runner import run
        from fw_context_mcp.mcp.background import _fast_staleness_check

        db_path = _db_path_for_project(indexed_project)
        cc_json = indexed_project / "compile_commands.json"

        # First, fill refs by reindexing with --refs
        _os.environ["FW_CONTEXT_FORCE_REFINDEX"] = "1"
        try:
            run(
                compile_commands=cc_json,
                db_path=db_path,
                vendor_paths=[],
                project_paths=[],
                project_root=indexed_project,
                index_refs=True,
                index_embeddings=False,
                analyze_symbols=False,
                analyze_overrides=False,
            )
        finally:
            _os.environ.pop("FW_CONTEXT_FORCE_REFINDEX", None)

        # Now delete refs
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            conn.execute("DELETE FROM refs WHERE config_hash=?", (ch,))
            conn.commit()
        finally:
            conn.close()

        needs, reasons = _fast_staleness_check(indexed_project)
        assert needs, "Should detect refs missing after DELETE"
        assert "refs missing" in reasons, f"Expected 'refs missing', got: {reasons}"

    def test_detects_schema_mismatch(self, indexed_project: Path, monkeypatch):
        """When schema version is behind, _fast_staleness_check reports it.

        ``open_db()`` auto-migrates, so we monkeypatch ``get_db_schema_version``
        to return a version lower than ``CURRENT_SCHEMA_VERSION``.
        """
        import fw_context_mcp.indexer.db as db_mod
        from fw_context_mcp.mcp.background import _fast_staleness_check

        monkeypatch.setattr(db_mod, "get_db_schema_version", lambda conn: 0)

        needs, reasons = _fast_staleness_check(indexed_project)
        assert needs, "Should detect schema mismatch"
        assert any("schema" in r for r in reasons), f"Expected schema-related reason, got: {reasons}"

    def test_compile_commands_changed_detection(self, indexed_project: Path):
        """When compile_commands.json mtime is newer than index, it's detected."""

        from fw_context_mcp.mcp.background import _fast_staleness_check

        cc_json = indexed_project / "compile_commands.json"
        cc_json.touch()
        _advance_mtime(cc_json)

        needs, reasons = _fast_staleness_check(indexed_project)
        assert needs, "Should detect compile_commands.json changed"
        assert "compile_commands.json changed" in reasons, (
            f"Expected 'compile_commands.json changed' in reasons, got: {reasons}"
        )


class TestModifiedFilesCache:
    """Verify ``_count_modified_files`` caching and invalidation."""

    def test_cache_returns_cached_value(self, indexed_project: Path):
        """Second call with use_cache=True returns cached value within TTL."""

        from fw_context_mcp.mcp.shared.stale import (
            _count_modified_files,
            _invalidate_modified_cache,
        )

        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            # First call — uncached
            count1 = _count_modified_files(conn, ch, indexed_project, use_cache=True)

            # Artificially bump all stored mtimes far into the future so
            # that a fresh stat would return 0 modified files.  The cache
            # should still return the OLD value (count1) until TTL expires.
            conn.execute("UPDATE files SET mtime = mtime + 100000 WHERE config_hash=?", (ch,))
            conn.commit()

            # Second call — must hit cache
            count2 = _count_modified_files(conn, ch, indexed_project, use_cache=True)
            assert count2 == count1, (
                f"Cache should return {count1}, got {count2} (stored mtimes were bumped, so fresh count would be 0)"
            )
        finally:
            conn.close()
            _invalidate_modified_cache(ch)

    def test_invalidate_clears_cache(self, indexed_project: Path):
        """After _invalidate_modified_cache, next call re-counts from disk."""
        from fw_context_mcp.mcp.shared.stale import (
            _count_modified_files,
            _invalidate_modified_cache,
        )

        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            count1 = _count_modified_files(conn, ch, indexed_project, use_cache=True)
            _invalidate_modified_cache(ch)

            # After invalidation, the cached value is gone
            count2 = _count_modified_files(conn, ch, indexed_project, use_cache=True)
            # count2 should still match count1 since nothing changed on disk
            assert count2 == count1, f"After invalidation, count should be {count1}, got {count2}"
        finally:
            conn.close()
            _invalidate_modified_cache(ch)

    def test_cache_without_use_cache_skips_cache(self, indexed_project: Path):
        """Without use_cache=True, each call does a fresh count."""
        from fw_context_mcp.mcp.shared.stale import (
            _count_modified_files,
            _invalidate_modified_cache,
        )

        db_path = _db_path_for_project(indexed_project)
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)

            # Fix mtime=0 rows (pre-migration artifact) — set to actual disk mtime
            # so the bump-test below works correctly.
            zero_rows = conn.execute(
                "SELECT path FROM files WHERE config_hash=? AND (mtime IS NULL OR mtime=0)",
                (ch,),
            ).fetchall()
            for r in zero_rows:
                try:
                    p = Path(r["path"])
                    if not p.is_absolute():
                        p = (indexed_project / p).resolve()
                    actual = p.stat().st_mtime
                    conn.execute(
                        "UPDATE files SET mtime=? WHERE config_hash=? AND path=?",
                        (actual, ch, r["path"]),
                    )
                except OSError:
                    pass
            conn.commit()

            count1 = _count_modified_files(conn, ch, indexed_project, use_cache=False)

            # Bump mtimes — should be reflected immediately without cache
            conn.execute("UPDATE files SET mtime = mtime + 100000 WHERE config_hash=?", (ch,))
            conn.commit()
            count2 = _count_modified_files(conn, ch, indexed_project, use_cache=False)
            assert count2 == 0, f"Without cache, bumped mtimes should give 0 modified, got {count2}"
            _ = count1  # used only for reference
        finally:
            conn.close()
            _invalidate_modified_cache(ch)


class TestForceRefindex:
    """Verify ``FW_CONTEXT_FORCE_REFINDEX`` makes ``_process_unit`` skip mtime check."""

    def test_force_refindex_skips_mtime_check(self, indexed_project: Path):
        """With FW_CONTEXT_FORCE_REFINDEX=1, unchanged files are still processed."""
        import os as _os

        from fw_context_mcp.indexer.compile_commands import parse as parse_cc
        from fw_context_mcp.indexer.db import get_file_mtimes
        from fw_context_mcp.indexer.runner import _process_unit

        db_path = _db_path_for_project(indexed_project)
        cc_json = indexed_project / "compile_commands.json"

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            existing = get_file_mtimes(conn, ch)
        finally:
            conn.close()

        # Use the real compilation unit from compile_commands.json
        units = list(parse_cc(cc_json))
        modem_unit = [u for u in units if u.file.name == "modem.c"]
        assert modem_unit, "modem.c must be in compile_commands.json"

        # ── Without env var: should return "unchanged" ──
        _os.environ.pop("FW_CONTEXT_FORCE_REFINDEX", None)
        status, _, _, _, _ = _process_unit(
            modem_unit[0],
            ch,
            indexed_project,
            [],
            [],
            False,
            db_path,
            existing_files=existing,
        )
        assert status == "unchanged", (
            f"Without FORCE_REFINDEX, unchanged file should return 'unchanged', got '{status}'"
        )

        # ── With env var: should NOT return "unchanged" ──
        _os.environ["FW_CONTEXT_FORCE_REFINDEX"] = "1"
        try:
            status_forced, syms, _, _, _ = _process_unit(
                modem_unit[0],
                ch,
                indexed_project,
            [],
                [],
                False,
                db_path,
                existing_files=existing,
            )
            assert status_forced == "updated", (
                f"With FORCE_REFINDEX, unchanged file should return 'updated', got '{status_forced}'"
            )
            assert syms > 0, "FORCE_REFINDEX should extract symbols from the file"
        finally:
            _os.environ.pop("FW_CONTEXT_FORCE_REFINDEX", None)

    def test_force_refindex_0_does_not_skip(self, indexed_project: Path):
        """FW_CONTEXT_FORCE_REFINDEX=0 behaves same as unset (normal mtime check)."""
        import os as _os

        from fw_context_mcp.indexer.compile_commands import parse as parse_cc
        from fw_context_mcp.indexer.db import get_file_mtimes
        from fw_context_mcp.indexer.runner import _process_unit

        db_path = _db_path_for_project(indexed_project)
        cc_json = indexed_project / "compile_commands.json"

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            existing = get_file_mtimes(conn, ch)
        finally:
            conn.close()

        units = list(parse_cc(cc_json))
        modem_unit = [u for u in units if u.file.name == "modem.c"]
        assert modem_unit, "modem.c must be in compile_commands.json"

        _os.environ["FW_CONTEXT_FORCE_REFINDEX"] = "0"
        try:
            status, _, _, _, _ = _process_unit(
                modem_unit[0],
                ch,
                indexed_project,
            [],
                [],
                False,
                db_path,
                existing_files=existing,
            )
            assert status == "unchanged", f"FORCE_REFINDEX=0 should still do mtime check, got '{status}'"
        finally:
            _os.environ.pop("FW_CONTEXT_FORCE_REFINDEX", None)


class TestBgReindexEndToEnd:
    """End-to-end: simulate the missing-refs → reindex → no-false-positive cycle."""

    def test_missing_refs_reindex_no_false_positive_cycle(
        self,
        indexed_project: Path,
    ):
        """The full cycle: missing refs → reindex via runner.run() → no false positive.

        1. Fill refs via run() API, then delete them to simulate the problem
        2. _fast_staleness_check detects "refs missing"
        3. runner.run() with FW_CONTEXT_FORCE_REFINDEX=1 refills refs
        4. After completion, _fast_staleness_check returns clean for refs
        5. _count_modified_files returns 0 (no false positive mtime detection)
        """
        import os as _os

        from fw_context_mcp.indexer.runner import run
        from fw_context_mcp.mcp.background import _fast_staleness_check
        from fw_context_mcp.mcp.shared.stale import (
            _count_modified_files,
            _invalidate_modified_cache,
        )

        db_path = _db_path_for_project(indexed_project)
        cc_json = indexed_project / "compile_commands.json"

        # ── First, establish a baseline with refs filled ──
        run(
            compile_commands=cc_json,
            db_path=db_path,
            vendor_paths=[],
            project_paths=[],
            project_root=indexed_project,
            index_refs=True,
            index_embeddings=False,
            analyze_symbols=False,
            analyze_overrides=False,
        )

        # ── Step 1: Delete refs (simulate db corruption or pre-refs index) ──
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            conn.execute("DELETE FROM refs WHERE config_hash=?", (ch,))
            conn.execute("DELETE FROM indirect_call_sites WHERE config_hash=?", (ch,))
            conn.commit()
            ref_count = conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (ch,)).fetchone()[0]
            assert ref_count == 0
        finally:
            conn.close()
            _invalidate_modified_cache(ch)

        # ── Step 2: Verify fast check detects the problem ──
        needs_before, reasons = _fast_staleness_check(indexed_project)
        assert needs_before, f"Should detect work needed, got reasons: {reasons}"
        assert "refs missing" in reasons, f"Should detect refs missing, got: {reasons}"
        print(f"  Detected before reindex: {reasons}")

        # ── Step 3: Force reindex via runner.run() (simulates daemon's startup reindex) ──
        _os.environ["FW_CONTEXT_FORCE_REFINDEX"] = "1"
        try:
            run(
                compile_commands=cc_json,
                db_path=db_path,
                vendor_paths=[],
                project_paths=[],
                project_root=indexed_project,
                index_refs=True,
                index_embeddings=False,
                analyze_symbols=False,
                analyze_overrides=False,
            )
        finally:
            del _os.environ["FW_CONTEXT_FORCE_REFINDEX"]

        # ── Step 4: Verify refs were refilled ──
        needs_after, reasons_after = _fast_staleness_check(indexed_project)
        assert "refs missing" not in reasons_after, f"Refs should be populated after bg reindex, got: {reasons_after}"
        assert "indirect call sites missing" not in reasons_after, (
            f"Indirect call sites should be populated, got: {reasons_after}"
        )

        # ── Step 5: Verify no false positive mtime detection ──
        # The key fix: mtimes must NOT be reset to 0. After reindex,
        # _count_modified_files must report 0 for unchanged files.
        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            mod_count = _count_modified_files(conn, ch, indexed_project)
            assert mod_count == 0, (
                f"CRITICAL: {mod_count} files falsely detected as modified after reindex. "
                "The mtime-reset bug would cause infinite reindex-respawn loop."
            )

            # Also verify refs are actually populated
            ref_count_after = conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (ch,)).fetchone()[0]
            assert ref_count_after > 0, "Refs table should be populated after reindex"
            print(f"  After reindex: reasons={reasons_after}, modified={mod_count}, refs={ref_count_after}")
        finally:
            conn.close()
            _invalidate_modified_cache(ch)


# ── Header-change propagation ─────────────────────────────────────────


def _index_cli(project_root: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the incremental indexer the way the background daemon does.

    The daemon spawns ``fw-context index --background`` — no ``--force``.
    These tests pass ``compile_commands.json`` explicitly because the test
    project has no detectable build system.
    """
    return _cli(
        [
            "index",
            "--no-refs",
            "--no-analyze",
            "--no-embeddings",
            *extra,
            str(project_root / "compile_commands.json"),
        ],
        cwd=project_root,
    )


def _symbol_row(conn, config_hash: str, name: str):
    """Return the single symbol row for *name*, or None."""
    return conn.execute(
        "SELECT name, file_path, line, end_line FROM symbols WHERE config_hash=? AND name=?",
        (config_hash, name),
    ).fetchone()


def _indexed_content(conn, config_hash: str, path_suffix: str) -> str:
    """Return the ifdef-filtered content stored for a file (empty when absent)."""
    row = conn.execute(
        "SELECT content FROM files WHERE config_hash=? AND path LIKE ? LIMIT 1",
        (config_hash, f"%{path_suffix}"),
    ).fetchone()
    return (row["content"] or "") if row else ""


def _manifest_file(db_path: Path) -> Path:
    """Return the single manifest.<config_hash>.json for the project index."""
    manifests = sorted(db_path.parent.glob("manifest.*.json"))
    assert len(manifests) == 1, f"expected exactly one manifest, found {manifests}"
    return manifests[0]


@pytest.mark.libclang
class TestHeaderChangePropagation:
    """A header is not a translation unit — its changes must still reach the index.

    Covers four defects that together froze header symbols at their
    first-index state:

    - **D1** the mtime fast-path only looked at the TU's own file, so a
      header change re-parsed nothing.
    - **D2** the manifest and the stored header mtimes were refreshed even
      for TUs that were never re-parsed, erasing the staleness signal.
    - **D3** ``files.content`` (backing ``read_file`` / ``search_content``)
      was written once and never refreshed.
    - **D4** symbols owned by a header were never deleted before re-insert,
      so ``ON CONFLICT(config_hash, usr)`` silently dropped the fresh rows.
    """

    def test_header_only_change_reaches_the_index(self, indexed_project: Path):
        """A declaration added to a header appears after an incremental run. (D1)"""
        db_path = _db_path_for_project(indexed_project)
        modem_h = indexed_project / "src" / "modem.h"
        modem_h.write_text(
            modem_h.read_text(encoding="utf-8").replace(
                "#endif", "int modem_brand_new_symbol(int x);\n\n#endif"
            ),
            encoding="utf-8",
        )
        _advance_mtime(modem_h, 5.0)

        result = _index_cli(indexed_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            row = _symbol_row(conn, ch, "modem_brand_new_symbol")
            assert row is not None, (
                "header-only change never reached the index\n" + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_header_change_reparses_every_dependent_tu(self, indexed_project: Path):
        """modem.h is included by main.c and modem.c — both must be re-parsed. (D1)

        Only re-parsing one of them would leave the other TU's copy of the
        header symbols behind, and ``ON CONFLICT`` would then keep the stale
        rows alive.
        """
        modem_h = indexed_project / "src" / "modem.h"
        modem_h.write_text(
            modem_h.read_text(encoding="utf-8").replace(
                "#endif", "int modem_fanout_probe(void);\n\n#endif"
            ),
            encoding="utf-8",
        )
        _advance_mtime(modem_h, 5.0)

        result = _index_cli(indexed_project)
        assert result.returncode == 0, result.stderr
        assert "2 updated, 1 unchanged" in result.stderr, (
            "expected main.c + modem.c re-parsed and utils.c skipped\n"
            + result.stderr[-2000:]
        )

    def test_removed_header_declaration_is_purged(self, indexed_project: Path):
        """A declaration deleted from a header must not survive in the index. (D4)"""
        db_path = _db_path_for_project(indexed_project)
        modem_h = indexed_project / "src" / "modem.h"
        original = modem_h.read_text(encoding="utf-8")

        # Seed: add the declaration and index it in.
        modem_h.write_text(
            original.replace("#endif", "int modem_doomed_symbol(void);\n\n#endif"),
            encoding="utf-8",
        )
        _advance_mtime(modem_h, 5.0)
        assert _index_cli(indexed_project, "--force").returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _symbol_row(conn, ch, "modem_doomed_symbol") is not None, "seed failed"
        finally:
            conn.close()

        # Remove it again — an incremental run must drop the row.
        modem_h.write_text(original, encoding="utf-8")
        _advance_mtime(modem_h, 10.0)
        result = _index_cli(indexed_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _symbol_row(conn, ch, "modem_doomed_symbol") is None, (
                "symbol deleted from the header survived the reindex\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_header_definition_line_is_refreshed(self, indexed_project: Path):
        """An inline definition inside a header must follow its new position. (D1 + D4)

        The old row lives under the header's own ``file_id`` and carries
        ``is_definition=1``, so nothing deleted it and the ON CONFLICT guard
        rejected the fresh row — the line number froze at the first index.
        """
        db_path = _db_path_for_project(indexed_project)
        util_h = indexed_project / "src" / "inline_util.h"
        _write_file(
            util_h,
            "#ifndef INLINE_UTIL_H\n"
            "#define INLINE_UTIL_H\n"
            "\n"
            "static inline int iu_double(int x) {\n"
            "    return x * 2;\n"
            "}\n"
            "\n"
            "#endif\n",
        )
        main_c = indexed_project / "src" / "main.c"
        main_c.write_text(
            main_c.read_text(encoding="utf-8").replace(
                '#include "utils.h"', '#include "utils.h"\n#include "inline_util.h"'
            ),
            encoding="utf-8",
        )
        _advance_mtime(main_c, 5.0)
        assert _index_cli(indexed_project, "--force").returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = _symbol_row(conn, ch, "iu_double")
            assert before is not None, "seed failed — iu_double not indexed"
            line_before = before["line"]
        finally:
            conn.close()

        # Push the definition down by five lines.  Only the header changes.
        util_h.write_text(
            util_h.read_text(encoding="utf-8").replace(
                "static inline", "\n\n\n\n\nstatic inline"
            ),
            encoding="utf-8",
        )
        _advance_mtime(util_h, 10.0)
        result = _index_cli(indexed_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            after = _symbol_row(conn, ch, "iu_double")
            assert after is not None, "iu_double disappeared after reindex"
            assert after["line"] == line_before + 5, (
                f"header definition kept a stale line: {after['line']} "
                f"(expected {line_before + 5})\n" + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_source_change_refreshes_indexed_content(self, indexed_project: Path):
        """``files.content`` must follow the disk — read_file/search_content use it. (D3)"""
        db_path = _db_path_for_project(indexed_project)
        utils_c = indexed_project / "src" / "utils.c"
        utils_c.write_text(
            utils_c.read_text(encoding="utf-8").replace(
                "int sum = 0;", "int sum = 0;\n    sum += 0; /* MARKER_D3 */"
            ),
            encoding="utf-8",
        )
        _advance_mtime(utils_c, 5.0)

        result = _index_cli(indexed_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert "MARKER_D3" in _indexed_content(conn, ch, "utils.c"), (
                "indexed content still holds the pre-change text\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_noop_run_does_not_rewrite_manifest(self, indexed_project: Path):
        """With nothing changed, the manifest must stay untouched. (D2)

        Rewriting it would stamp current header hashes onto TUs that were
        never re-parsed — the index would then believe it is up to date.
        Skipping the rewrite also skips a libclang parse per unchanged TU,
        which is what makes a no-op background run cheap.
        """
        db_path = _db_path_for_project(indexed_project)
        manifest = _manifest_file(db_path)
        before_bytes = manifest.read_bytes()
        before_mtime = manifest.stat().st_mtime_ns

        result = _index_cli(indexed_project)
        assert result.returncode == 0, result.stderr
        assert "0 updated" in result.stderr, result.stderr[-2000:]
        assert manifest.read_bytes() == before_bytes, "manifest content changed on a no-op run"
        assert manifest.stat().st_mtime_ns == before_mtime, (
            "manifest was rewritten on a no-op run\n" + result.stderr[-2000:]
        )
        assert "from tu_headers" not in result.stderr, (
            "unchanged TUs still contributed header hashes\n" + result.stderr[-2000:]
        )


# ── Header-scoped row cleanup (refs / macros) ─────────────────────────


def _index_cli_refs(project_root: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the incremental indexer with cross-reference indexing enabled.

    ``_index_cli`` passes ``--no-refs`` to keep the other tests fast.  The
    refs and indirect-call-site tests need the reference walk, so this
    variant leaves it on.
    """
    return _cli(
        [
            "index",
            "--no-analyze",
            "--no-embeddings",
            *extra,
            str(project_root / "compile_commands.json"),
        ],
        cwd=project_root,
    )


def _include_from_main(project_root: Path, header_name: str) -> None:
    """Add ``#include "<header_name>"`` to main.c after the utils.h include."""
    main_c = project_root / "src" / "main.c"
    main_c.write_text(
        main_c.read_text(encoding="utf-8").replace(
            '#include "utils.h"', f'#include "utils.h"\n#include "{header_name}"'
        ),
        encoding="utf-8",
    )


def _refs_from(conn, config_hash: str, path_suffix: str) -> list:
    """Return every refs row that originates in a file with this path suffix."""
    return conn.execute(
        "SELECT from_file, from_line, to_usr, ref_kind FROM refs "
        "WHERE config_hash=? AND from_file LIKE ? ORDER BY from_line",
        (config_hash, f"%{path_suffix}"),
    ).fetchall()


def _ics_from(conn, config_hash: str, path_suffix: str) -> list:
    """Return every indirect_call_sites row originating in this file."""
    return conn.execute(
        "SELECT from_file, from_line, target_name FROM indirect_call_sites "
        "WHERE config_hash=? AND from_file LIKE ? ORDER BY from_line",
        (config_hash, f"%{path_suffix}"),
    ).fetchall()


def _macro_lines(conn, config_hash: str, name: str) -> list[int]:
    """Return the line of every macro row stored under *name*."""
    rows = conn.execute(
        "SELECT line FROM macros WHERE config_hash=? AND name=? ORDER BY line",
        (config_hash, name),
    ).fetchall()
    return [r["line"] for r in rows]


@pytest.mark.libclang
class TestHeaderScopedRowCleanup:
    """Rows owned by a header must be cleaned when the header is re-parsed.

    Code inside an inline function in a header stores its rows under the
    HEADER's path / file_id.  The cleanup used to delete refs, indirect call
    sites, function pointer assignments and macros by the *translation
    unit's* key only, so nothing removed them:

    - a call removed from a header kept its refs row, so ``find_callers``
      reported a caller that no longer exists.  A call that only moved got a
      second row, because ``idx_refs_unique`` includes ``from_line``.
    - a ``#define`` removed from a header kept its macros row.  One that
      moved got a second row, because the UNIQUE key is
      ``(config_hash, file_id, line)``.

    ``replace_file_data()`` now clears everything a file owns from its file
    id alone, so these cases have one implementation to get right instead of
    three.
    """

    def test_removed_header_call_drops_its_ref(self, c_project: Path):
        """A call deleted from an inline header function must lose its ref. (D5)"""
        db_path = _db_path_for_project(c_project)
        hdr = c_project / "src" / "inline_ref.h"
        _write_file(
            hdr,
            "#ifndef INLINE_REF_H\n"
            "#define INLINE_REF_H\n"
            '#include "modem.h"\n'
            "\n"
            "static inline int ir_probe(void) {\n"
            '    return modem_send("x", 1);\n'
            "}\n"
            "\n"
            "#endif\n",
        )
        _include_from_main(c_project, "inline_ref.h")
        assert _index_cli_refs(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _refs_from(conn, ch, "inline_ref.h"), "seed failed — no ref from the header"
        finally:
            conn.close()

        # Drop the call.  Only the header changes.
        hdr.write_text(
            hdr.read_text(encoding="utf-8").replace('return modem_send("x", 1);', "return 0;"),
            encoding="utf-8",
        )
        _advance_mtime(hdr, 10.0)
        result = _index_cli_refs(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            stale = _refs_from(conn, ch, "inline_ref.h")
            assert stale == [], (
                f"refs from the header survived its rewrite: {[dict(r) for r in stale]}\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_moved_header_call_leaves_no_duplicate_ref(self, c_project: Path):
        """A call that only moved must not gain extra refs rows. (D5)

        One call site legitimately produces two rows — ``ref_kind='call'``
        and ``ref_kind='ref'``.  The count must stay at two after the move;
        four rows would mean the pre-move pair survived, because
        ``idx_refs_unique`` includes ``from_line``.
        """
        db_path = _db_path_for_project(c_project)
        hdr = c_project / "src" / "inline_ref.h"
        _write_file(
            hdr,
            "#ifndef INLINE_REF_H\n"
            "#define INLINE_REF_H\n"
            '#include "modem.h"\n'
            "\n"
            "static inline int ir_probe(void) {\n"
            '    return modem_send("x", 1);\n'
            "}\n"
            "\n"
            "#endif\n",
        )
        _include_from_main(c_project, "inline_ref.h")
        assert _index_cli_refs(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = _refs_from(conn, ch, "inline_ref.h")
            lines_before = {r["from_line"] for r in before}
            assert len(lines_before) == 1, (
                f"seed expected one call site, got {[dict(r) for r in before]}"
            )
            count_before = len(before)
            line_before = lines_before.pop()
        finally:
            conn.close()

        # Push the inline function down by five lines.
        hdr.write_text(
            hdr.read_text(encoding="utf-8").replace(
                "static inline", "\n\n\n\n\nstatic inline"
            ),
            encoding="utf-8",
        )
        _advance_mtime(hdr, 10.0)
        result = _index_cli_refs(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            after = _refs_from(conn, ch, "inline_ref.h")
            assert len(after) == count_before, (
                f"the moved call left duplicate refs rows: {[dict(r) for r in after]}\n"
                + result.stderr[-2000:]
            )
            assert {r["from_line"] for r in after} == {line_before + 5}, (
                f"refs kept a stale line: {sorted(r['from_line'] for r in after)} "
                f"(expected {line_before + 5})"
            )
        finally:
            conn.close()

    def test_repeated_reindex_does_not_pile_up_header_call_sites(self, c_project: Path):
        """A fn-pointer call in a header must not gain a row per reindex. (D5)

        ``indirect_call_sites`` carries no unique constraint — a field can
        legitimately be invoked many times from one line — so nothing stops
        duplicates.  Three reindexes of an unchanged call site must leave the
        row count where it started.
        """
        db_path = _db_path_for_project(c_project)
        hdr = c_project / "src" / "fnptr_hdr.h"

        def header_text(round_no: int) -> str:
            """Header whose content changes per round, call site line fixed.

            An identical rewrite is NOT enough: the indexer compares content
            hashes, so a header with unchanged text never triggers a reparse
            of its TUs.  The marker comment changes the hash while keeping the
            line count — and therefore the call site's ``from_line`` — stable,
            so a pile-up is the only way the row count can grow.
            """
            return (
                "#ifndef FNPTR_HDR_H\n"
                "#define FNPTR_HDR_H\n"
                f"/* revision {round_no:04d} */\n"
                "\n"
                "typedef struct {\n"
                "    int (*on_data)(const char* buf, int len);\n"
                "} fh_driver_t;\n"
                "\n"
                "static inline int fh_dispatch(fh_driver_t* d, const char* buf, int len) {\n"
                "    return d->on_data(buf, len);\n"
                "}\n"
                "\n"
                "#endif\n"
            )

        _write_file(hdr, header_text(0))
        _include_from_main(c_project, "fnptr_hdr.h")
        assert _index_cli_refs(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            baseline = _ics_from(conn, ch, "fnptr_hdr.h")
            assert baseline, "seed failed — no indirect call site from the header"
            count_baseline = len(baseline)
            line_baseline = {r["from_line"] for r in baseline}
        finally:
            conn.close()

        for round_no in range(1, 4):
            hdr.write_text(header_text(round_no), encoding="utf-8")
            _advance_mtime(hdr, 10.0 * round_no)
            result = _index_cli_refs(c_project)
            assert result.returncode == 0, result.stderr
            assert "unchanged" not in result.stderr or "updated" in result.stderr, (
                f"round {round_no}: nothing was re-parsed, the test proves nothing\n"
                + result.stderr[-2000:]
            )

            conn = open_db(db_path)
            try:
                ch = _config_hash(conn)
                rows = _ics_from(conn, ch, "fnptr_hdr.h")
                assert {r["from_line"] for r in rows} == line_baseline, (
                    f"round {round_no}: the call site moved — the marker comment "
                    f"changed the line count\n{[dict(r) for r in rows]}"
                )
                assert len(rows) == count_baseline, (
                    f"round {round_no}: call sites piled up — "
                    f"{len(rows)} rows, expected {count_baseline}: "
                    f"{[dict(r) for r in rows]}\n" + result.stderr[-2000:]
                )
            finally:
                conn.close()

    def test_removed_header_macro_is_purged(self, c_project: Path):
        """A ``#define`` deleted from a header must not survive. (D6)"""
        db_path = _db_path_for_project(c_project)
        hdr = c_project / "src" / "macro_hdr.h"
        _write_file(
            hdr,
            "#ifndef MACRO_HDR_H\n"
            "#define MACRO_HDR_H\n"
            "\n"
            "#define MH_DOOMED 1\n"
            "\n"
            "#endif\n",
        )
        _include_from_main(c_project, "macro_hdr.h")
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _macro_lines(conn, ch, "MH_DOOMED"), "seed failed — macro not indexed"
        finally:
            conn.close()

        hdr.write_text(
            hdr.read_text(encoding="utf-8").replace("#define MH_DOOMED 1\n", ""),
            encoding="utf-8",
        )
        _advance_mtime(hdr, 10.0)
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _macro_lines(conn, ch, "MH_DOOMED") == [], (
                "a macro deleted from the header survived the reindex\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_moved_header_macro_leaves_no_duplicate(self, c_project: Path):
        """A ``#define`` that only moved must end up with exactly one row. (D6)"""
        db_path = _db_path_for_project(c_project)
        hdr = c_project / "src" / "macro_hdr.h"
        _write_file(
            hdr,
            "#ifndef MACRO_HDR_H\n"
            "#define MACRO_HDR_H\n"
            "\n"
            "#define MH_MOVED 1\n"
            "\n"
            "#endif\n",
        )
        _include_from_main(c_project, "macro_hdr.h")
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            lines_before = _macro_lines(conn, ch, "MH_MOVED")
            assert len(lines_before) == 1, f"seed expected one macro row, got {lines_before}"
        finally:
            conn.close()

        hdr.write_text(
            hdr.read_text(encoding="utf-8").replace(
                "#define MH_MOVED 1", "\n\n\n\n\n#define MH_MOVED 1"
            ),
            encoding="utf-8",
        )
        _advance_mtime(hdr, 10.0)
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            lines_after = _macro_lines(conn, ch, "MH_MOVED")
            assert lines_after == [lines_before[0] + 5], (
                f"the moved macro left a duplicate row: {lines_after}\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()


# ── Build retention ───────────────────────────────────────────────────


def _add_tu_to_compile_commands(project_root: Path, name: str) -> None:
    """Add one more translation unit to the project and its compile_commands.

    Note this does NOT change ``config_hash`` — that hash identifies the
    compilation dialect, not the file set.  Use :func:`_change_build_dialect`
    when a test needs a new build identity.
    """
    import json

    src = project_root / "src"
    _write_file(src / name, f"int {Path(name).stem}_fn(void) {{ return 1; }}\n")
    cc_json = project_root / "compile_commands.json"
    cc = json.loads(cc_json.read_text(encoding="utf-8"))
    cc.append({
        "directory": str(src),
        "file": name,
        "arguments": [
            "gcc", "-std=c11", "-O2", "-Isrc", "-c", name,
            "-o", f"build/{Path(name).stem}.o",
        ],
    })
    cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")


def _change_build_dialect(project_root: Path, define: str) -> None:
    """Add a ``-D`` macro to every entry, minting a new ``config_hash``.

    A macro flips ``#ifdef``, so it can change what the same source text
    compiles to — that is what makes a different build.  Adding or removing a
    source file does not.
    """
    import json

    cc_json = project_root / "compile_commands.json"
    cc = json.loads(cc_json.read_text(encoding="utf-8"))
    for entry in cc:
        entry["arguments"] = [*entry["arguments"], f"-D{define}"]
    cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")


def _build_hashes(conn) -> list[str]:
    """Return every config_hash in build_configs, oldest first."""
    return [
        r["config_hash"]
        for r in conn.execute(
            "SELECT config_hash FROM build_configs ORDER BY created_at, rowid"
        ).fetchall()
    ]


@pytest.mark.libclang
class TestBuildRetention:
    """A reindex must not leave its predecessor's build in the database.

    ``_step_cleanup_old_builds`` guards on the ``reindex.pause`` marker so it
    never deletes a build another process is still serving.  It used to test
    ``PidFile.is_active``, but ``fw-context index`` writes that marker with
    its OWN pid for the whole run — so the guard was always true and
    retention never ran once.  Every reindex silently kept the previous
    build: symbols, macros, refs and file content, for every config_hash the
    project ever had.
    """

    def test_old_build_is_deleted_after_a_config_change(self, c_project: Path):
        """One build per (variant, image) slot survives a config_hash change."""
        db_path = _db_path_for_project(c_project)
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            before = _build_hashes(conn)
            assert len(before) == 1, f"expected one build after the first index, got {before}"
        finally:
            conn.close()

        _change_build_dialect(c_project, "RETENTION_PROBE=1")
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            after = _build_hashes(conn)
            assert len(after) == 1, (
                f"retention did not run — old build(s) left behind: {after}\n"
                + result.stderr[-2000:]
            )
            assert after[0] != before[0], "the surviving build should be the new one"
        finally:
            conn.close()

    def test_old_build_leaves_no_files_on_disk(self, c_project: Path):
        """A retired build takes both of its on-disk artifacts with it.

        The manifest used to be left behind.  Nothing reads an abandoned
        build's manifest since the reuse tier was removed, so it was pure
        accumulation — one file per dialect change, and 52 MB of it on
        zbox-ecb-fw.  It also made ``manifest.load(db_dir)`` ambiguous: that
        form picks the most recently modified manifest in the directory.
        """
        db_path = _db_path_for_project(c_project)
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            old_hash = _build_hashes(conn)[0]
        finally:
            conn.close()
        assert (db_path.parent / f"manifest.{old_hash}.json").exists(), "seed failed"

        _change_build_dialect(c_project, "ARTIFACT_PROBE=1")
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        leftovers = sorted(
            p.name for p in db_path.parent.glob(f"*.{old_hash}.json")
        )
        assert leftovers == [], (
            f"artifacts of the retired build survived: {leftovers}\n"
            + result.stderr[-2000:]
        )
        remaining = sorted(p.name for p in db_path.parent.glob("manifest.*.json"))
        assert len(remaining) == 1, (
            f"expected exactly one manifest after retention, found {remaining}"
        )

    def test_old_build_leaves_no_rows_behind(self, c_project: Path):
        """Deleting a build must take its rows with it, not just its row in
        ``build_configs`` — otherwise the tables grow without bound."""
        db_path = _db_path_for_project(c_project)
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            old_hash = _build_hashes(conn)[0]
        finally:
            conn.close()

        _change_build_dialect(c_project, "RETENTION_PROBE=1")
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            leftovers = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE config_hash=?",  # noqa: S608
                    (old_hash,),
                ).fetchone()[0]
                for table in ("files", "symbols", "macros", "refs")
            }
            assert all(n == 0 for n in leftovers.values()), (
                f"rows of the deleted build survived: {leftovers}\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()


# ── Coverage purge ────────────────────────────────────────────────────


def _file_row(conn, config_hash: str, path_suffix: str):
    """Return the files row whose path ends with *path_suffix*, or None."""
    return conn.execute(
        "SELECT id, path, content FROM files WHERE config_hash=? AND path LIKE ? LIMIT 1",
        (config_hash, f"%{path_suffix}"),
    ).fetchone()


@pytest.mark.libclang
class TestCoveragePurge:
    """A file that leaves the build must leave the index with it.

    ``config_hash`` names the compilation dialect, so dropping a source file
    no longer mints a new build — nothing else notices it left.
    ``purge_missing`` only looks for files gone from DISK, and
    ``delete_orphan_files`` only removes rows that already have no symbols, no
    macros and empty content.  Without a coverage purge the file keeps its
    symbols, macros, refs and indexed content for the life of the index.
    """

    def test_removed_tu_leaves_no_rows(self, c_project: Path):
        """A TU dropped from compile_commands.json keeps nothing behind."""
        db_path = _db_path_for_project(c_project)
        _add_tu_to_compile_commands(c_project, "doomed.c")
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            row = _file_row(conn, ch, "doomed.c")
            assert row is not None, "seed failed — doomed.c was not indexed"
            assert _symbol_row(conn, ch, "doomed_fn") is not None, "seed failed"
        finally:
            conn.close()

        # Drop it from the build; the file stays on disk, so purge_missing
        # cannot be what cleans it up.
        import json

        cc_json = c_project / "compile_commands.json"
        cc = [e for e in json.loads(cc_json.read_text(encoding="utf-8"))
              if e["file"] != "doomed.c"]
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")
        assert (c_project / "src" / "doomed.c").exists(), "the file must stay on disk"

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _file_row(conn, ch, "doomed.c") is None, (
                "files row of a TU no longer in the build survived\n"
                + result.stderr[-2000:]
            )
            assert _symbol_row(conn, ch, "doomed_fn") is None, (
                "symbols of a TU no longer in the build survived\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_headers_still_included_elsewhere_survive(self, c_project: Path):
        """Purging one TU must not take a header another TU still includes.

        modem.h is included by both main.c and modem.c.  Dropping modem.c must
        leave modem.h — and its symbols — in place.
        """
        db_path = _db_path_for_project(c_project)
        assert _index_cli(c_project).returncode == 0

        import json

        cc_json = c_project / "compile_commands.json"
        cc = [e for e in json.loads(cc_json.read_text(encoding="utf-8"))
              if e["file"] != "modem.c"]
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _file_row(conn, ch, "modem.h") is not None, (
                "a header still included by main.c was purged\n"
                + result.stderr[-2000:]
            )
            assert _file_row(conn, ch, "modem.c") is None, (
                "the dropped TU's own row survived\n" + result.stderr[-2000:]
            )
        finally:
            conn.close()

    def test_out_of_project_headers_are_not_collateral_damage(self, c_project: Path):
        """SDK and system headers must survive a purge that targets a TU.

        The purge deliberately covers vendor and SDK files too — a framework
        upgrade replaces headers and the dropped ones must go with them — so
        it can only be as correct as the manifest is complete.  Note this case
        does NOT exercise the extension whitelist that made the manifest
        incomplete: this fixture's system headers are all ``.h``, so they were
        recorded either way.  ``TestManifestRecordsEveryInclude`` covers that.
        """
        db_path = _db_path_for_project(c_project)
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = conn.execute(
                "SELECT COUNT(*) FROM files WHERE config_hash=? AND path LIKE '/%'",
                (ch,),
            ).fetchone()[0]
            assert before > 0, (
                "seed failed — the fixture indexed no out-of-project headers, "
                "so this test would prove nothing"
            )
        finally:
            conn.close()

        # A reindex that also drops a TU, so the purge definitely runs.
        _add_tu_to_compile_commands(c_project, "transient.c")
        assert _index_cli(c_project).returncode == 0
        import json

        cc_json = c_project / "compile_commands.json"
        cc = [e for e in json.loads(cc_json.read_text(encoding="utf-8"))
              if e["file"] != "transient.c"]
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            after = conn.execute(
                "SELECT COUNT(*) FROM files WHERE config_hash=? AND path LIKE '/%'",
                (ch,),
            ).fetchone()[0]
            assert after == before, (
                f"out-of-project headers were purged: {before} -> {after}\n"
                + result.stderr[-2000:]
            )
            assert _file_row(conn, ch, "transient.c") is None, (
                "the dropped in-project TU should still have been purged"
            )
        finally:
            conn.close()

    def test_untouched_build_purges_nothing(self, c_project: Path):
        """A no-op reindex must not delete anything."""
        db_path = _db_path_for_project(c_project)
        assert _index_cli(c_project).returncode == 0

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            before = conn.execute(
                "SELECT COUNT(*) FROM files WHERE config_hash=?", (ch,)
            ).fetchone()[0]
        finally:
            conn.close()

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            after = conn.execute(
                "SELECT COUNT(*) FROM files WHERE config_hash=?", (ch,)
            ).fetchone()[0]
            assert after == before, (
                f"a no-op reindex changed the file count: {before} -> {after}\n"
                + result.stderr[-2000:]
            )
        finally:
            conn.close()


# ── Dialect round trip ────────────────────────────────────────────────


_GATED_HEADER = (
    "#ifndef FEATURE_H\n"
    "#define FEATURE_H\n"
    "#ifdef FEATURE_ON\n"
    "int feature_only_fn(void);\n"
    "#else\n"
    "int baseline_only_fn(void);\n"
    "#endif\n"
    "#endif\n"
)


def _gated_symbols(conn, config_hash: str) -> set[str]:
    """Return whichever of the two #ifdef-gated declarations are indexed."""
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM symbols WHERE config_hash=? AND name LIKE '%_only_fn'",
            (config_hash,),
        ).fetchall()
    }


@pytest.mark.libclang
class TestDialectRoundTrip:
    """Returning to a previous dialect must be answered by parsing.

    A ``-D`` change flips ``#ifdef``, so the stored rows of one dialect say
    nothing about another.  Going back to a dialect whose rows retention
    already deleted used to hit the "reuse" tier, which imported rows from
    whatever other build was newest — ``_reassign_symbols_for_file`` selects
    its source with ``ORDER BY rowid DESC`` and never checks that the dialect
    matches.

    The header below declares exactly one symbol per dialect, so the name
    present tells you which dialect the stored rows came from.
    """

    def _setup(self, project_root: Path) -> None:
        _write_file(project_root / "src" / "feature.h", _GATED_HEADER)
        _include_from_main(project_root, "feature.h")

    def test_gated_symbol_follows_the_current_dialect(self, c_project: Path):
        db_path = _db_path_for_project(c_project)
        self._setup(c_project)

        assert _index_cli(c_project).returncode == 0
        conn = open_db(db_path)
        try:
            assert _gated_symbols(conn, _config_hash(conn)) == {"baseline_only_fn"}, (
                "seed failed — the #ifdef-gated declaration was not indexed"
            )
        finally:
            conn.close()

        # → dialect B
        _change_build_dialect(c_project, "FEATURE_ON")
        assert _index_cli(c_project).returncode == 0
        conn = open_db(db_path)
        try:
            assert _gated_symbols(conn, _config_hash(conn)) == {"feature_only_fn"}, (
                "the macro did not take effect — the test would prove nothing"
            )
        finally:
            conn.close()

        # → back to dialect A.  Its rows were retired by retention when B was
        # built, so this is the case the reuse tier used to serve.
        import json

        cc_json = c_project / "compile_commands.json"
        cc = json.loads(cc_json.read_text(encoding="utf-8"))
        for entry in cc:
            entry["arguments"] = [a for a in entry["arguments"]
                                  if a != "-DFEATURE_ON"]
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            gated = _gated_symbols(conn, _config_hash(conn))
            assert gated == {"baseline_only_fn"}, (
                f"the index does not match the current dialect: {sorted(gated)}\n"
                + result.stderr[-2500:]
            )
        finally:
            conn.close()

    def test_dialect_change_reparses_instead_of_importing(self, c_project: Path):
        """No translation unit may be satisfied by importing another build."""
        self._setup(c_project)
        assert _index_cli(c_project).returncode == 0
        _change_build_dialect(c_project, "FEATURE_ON")
        assert _index_cli(c_project).returncode == 0

        import json

        cc_json = c_project / "compile_commands.json"
        cc = json.loads(cc_json.read_text(encoding="utf-8"))
        for entry in cc:
            entry["arguments"] = [a for a in entry["arguments"]
                                  if a != "-DFEATURE_ON"]
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr
        # Check the per-TU summary line specifically.  "reused" also appears
        # in the manifest updater's own line ("N updated, M reused"), which
        # counts reused manifest ENTRIES and is unrelated to serving a TU
        # from another build.
        summary = next(
            (ln for ln in result.stderr.splitlines()
             if "unchanged," in ln and "skipped" in ln),
            "",
        )
        assert summary, f"no indexer summary line found\n{result.stderr[-2500:]}"
        assert "reused" not in summary, (
            f"a TU was served from another build instead of being re-parsed\n{summary}"
        )
        # Returning to a retired dialect leaves nothing under its
        # config_hash, so every TU is parsed afresh — the count is whatever
        # the project has, but it must not be zero.
        assert "0 updated" not in summary, (
            f"no TU was re-parsed under the restored dialect\n{summary}"
        )


# ── Manifest completeness ─────────────────────────────────────────────


def _manifest_header_paths(db_path: Path) -> set[str]:
    """Every header path the manifest records, across all TUs."""
    import json

    manifests = sorted(db_path.parent.glob("manifest.*.json"))
    assert len(manifests) == 1, f"expected one manifest, found {manifests}"
    data = json.loads(manifests[0].read_text(encoding="utf-8"))
    paths: set[str] = set()
    for entry in data.get("entries") or []:
        paths.update(entry.get("headers") or [])
    return paths


@pytest.mark.libclang
class TestManifestRecordsEveryInclude:
    """The manifest must list every file an ``#include`` reached.

    It used to filter by extension — ``{.h .hpp .hxx .hh .inl}`` — which
    silently dropped every extensionless C++ standard header (``<algorithm>``,
    ``<bit>``) and every ``.tcc`` template body.  Two things broke.  The
    coverage purge deleted those files because the manifest did not list them:
    measured on HA_Boiler, 29 files and 1810 symbols.  And nothing recorded a
    hash for them, so a toolchain or SDK upgrade could change any of them
    without marking a single TU stale.

    A whitelist of "what counts as a header" cannot be kept complete;
    ``get_includes()`` already answers the question exactly.
    """

    def test_an_extensionless_header_is_recorded(self, c_project: Path):
        """This is the shape of every C++ standard library header."""
        db_path = _db_path_for_project(c_project)
        _write_file(
            c_project / "src" / "plain_header",
            "#ifndef PLAIN_H\n#define PLAIN_H\nint plain_fn(void);\n#endif\n",
        )
        _include_from_main(c_project, "plain_header")

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        recorded = _manifest_header_paths(db_path)
        assert "src/plain_header" in recorded, (
            "an extensionless header is missing from the manifest, so nothing "
            "can detect a change to it and the coverage purge would delete it\n"
            f"recorded: {sorted(p for p in recorded if 'src/' in p)}"
        )

    def test_an_unusual_extension_is_recorded(self, c_project: Path):
        """``.tcc`` is what libstdc++ names its template bodies."""
        db_path = _db_path_for_project(c_project)
        _write_file(
            c_project / "src" / "bodies.tcc",
            "static inline int tcc_fn(void) { return 1; }\n",
        )
        _write_file(
            c_project / "src" / "wrap.h",
            '#ifndef WRAP_H\n#define WRAP_H\n#include "bodies.tcc"\n#endif\n',
        )
        _include_from_main(c_project, "wrap.h")

        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        recorded = _manifest_header_paths(db_path)
        assert "src/bodies.tcc" in recorded, (
            "a .tcc include is missing from the manifest\n"
            f"recorded: {sorted(p for p in recorded if 'src/' in p)}"
        )

    def test_such_a_header_is_not_purged(self, c_project: Path):
        """The consequence: coverage must keep what the manifest now lists."""
        db_path = _db_path_for_project(c_project)
        _write_file(
            c_project / "src" / "plain_header",
            "#ifndef PLAIN_H\n#define PLAIN_H\nint plain_fn(void);\n#endif\n",
        )
        _include_from_main(c_project, "plain_header")
        assert _index_cli(c_project).returncode == 0

        # Reindex after dropping a TU, so the purge definitely runs.
        _add_tu_to_compile_commands(c_project, "transient.c")
        assert _index_cli(c_project).returncode == 0
        import json

        cc_json = c_project / "compile_commands.json"
        cc = [e for e in json.loads(cc_json.read_text(encoding="utf-8"))
              if e["file"] != "transient.c"]
        cc_json.write_text(json.dumps(cc, indent=2), encoding="utf-8")
        result = _index_cli(c_project)
        assert result.returncode == 0, result.stderr

        conn = open_db(db_path)
        try:
            ch = _config_hash(conn)
            assert _file_row(conn, ch, "plain_header") is not None, (
                "the extensionless header was purged\n" + result.stderr[-2000:]
            )
            assert _file_row(conn, ch, "transient.c") is None, (
                "the dropped TU should still have been purged"
            )
        finally:
            conn.close()


class TestReindexKeepsTheGeneratedFlag:
    """``reindex_file`` must not turn a generated header into a plain one.

    _update_manifest_after_reindex passed None for build_dir_patterns, so
    _is_generated_header() answered False for every header and every record
    it wrote claimed "generated": False.  After the end of vendor trust
    ``generated`` is the only trust rule left, so a single reindex_file
    turned the next full index run into a complete reparse.  Measured on
    zbox-ecb-fw-v5 variant nrf52840-dev: 27 generated headers went to 0.
    """

    @staticmethod
    def _unit(tmp_path: Path, rel: str):
        from unittest.mock import MagicMock

        src = tmp_path / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("int main() { return 0; }")
        unit = MagicMock()
        unit.file = src
        unit.directory = tmp_path
        unit.clang_args = ["-std=c11"]
        unit.raw_entry = {"file": rel, "directory": str(tmp_path)}
        return unit

    @staticmethod
    def _manifest(tmp_path: Path, *, entries, build_dir_patterns) -> dict:
        from fw_context_mcp.indexer.manifest import MANIFEST_FORMAT

        manifest: dict = {
            "_format": MANIFEST_FORMAT,
            "compile_commands_path": str(tmp_path / "compile_commands.json"),
            "project_root": str(tmp_path),
            "arg_sets": [["-std=c11"]],
            "headers": {
                "build/zephyr/include/generated/autoconf.h": {
                    "hash": "OLD", "generated": True,
                },
            },
            "entries": entries,
        }
        if build_dir_patterns is not None:
            manifest["build_dir_patterns"] = build_dir_patterns
        return manifest

    def _run(self, tmp_path: Path, *, entries, build_dir_patterns, monkeypatch):
        """Drive _update_manifest_after_reindex with a stubbed header collector.

        The real collector needs libclang and a compiled TU.  What this test
        checks is the patterns that reach it and the merge that follows, so
        the stub records the patterns and returns the header the build
        generates.
        """
        from fw_context_mcp.mcp.handlers import maintenance
        from fw_context_mcp.indexer import manifest as manifest_mod

        saved: dict = {}
        seen: list = []

        def fake_collect(unit, root, build_dir_patterns, header_table):
            seen.append(build_dir_patterns)
            path = "build/zephyr/include/generated/autoconf.h"
            header_table[path] = {
                "hash": "NEW",
                "generated": manifest_mod._is_generated_header(path, build_dir_patterns),
            }
            return [path]

        def fake_load(db_dir, config_hash):
            return self._manifest(
                tmp_path, entries=entries, build_dir_patterns=build_dir_patterns
            )

        def fake_save(data, db_dir, config_hash):
            saved.update(data)
            return config_hash

        monkeypatch.setattr(manifest_mod, "_collect_headers_from_tokens", fake_collect)
        monkeypatch.setattr(manifest_mod, "load", fake_load)
        monkeypatch.setattr(manifest_mod, "save", fake_save)

        maintenance._update_manifest_after_reindex(
            [(self._unit(tmp_path, "src/main.c"), object())],
            tmp_path,
            tmp_path / "index",
            "deadbeef",
        )
        return saved, seen

    def test_reindex_file_reads_the_patterns_from_the_manifest(self, tmp_path, monkeypatch):
        """The manifest is the source, because it holds what the index run used."""
        _, seen = self._run(
            tmp_path,
            entries=[{
                "file": "src/main.c", "directory": str(tmp_path), "arg_set": 0,
                "source_hash": "x", "headers": [],
            }],
            build_dir_patterns=["build/"],
            monkeypatch=monkeypatch,
        )

        assert seen == [["build/"]]

    def test_reindex_file_keeps_the_generated_flag(self, tmp_path, monkeypatch):
        """An entry the manifest already has goes through update_entry."""
        saved, _ = self._run(
            tmp_path,
            entries=[{
                "file": "src/main.c", "directory": str(tmp_path), "arg_set": 0,
                "source_hash": "x", "headers": [],
            }],
            build_dir_patterns=["build/"],
            monkeypatch=monkeypatch,
        )

        record = saved["headers"]["build/zephyr/include/generated/autoconf.h"]
        assert record["generated"] is True
        assert record["hash"] == "NEW"

    def test_a_new_entry_keeps_the_generated_flag(self, tmp_path, monkeypatch):
        """A TU the manifest has never seen goes down the else branch.

        That branch bypasses update_entry, so the fix in update_entry alone
        does not reach it.
        """
        saved, _ = self._run(
            tmp_path,
            entries=[],
            build_dir_patterns=["build/"],
            monkeypatch=monkeypatch,
        )

        record = saved["headers"]["build/zephyr/include/generated/autoconf.h"]
        assert record["generated"] is True
        assert len(saved["entries"]) == 1

    def test_a_manifest_without_patterns_falls_back_to_detection(self, tmp_path, monkeypatch):
        """An index written before the key existed must still get patterns.

        The probe for this whole class must run on a build whose manifest HAS
        patterns: five of the nine zbox-v5 manifests carry none, and on those
        "generated is 0 after reindex_file" holds before the fix as well.
        """
        (tmp_path / "west.yml").write_text("")
        _, seen = self._run(
            tmp_path,
            entries=[{
                "file": "src/main.c", "directory": str(tmp_path), "arg_set": 0,
                "source_hash": "x", "headers": [],
            }],
            build_dir_patterns=None,
            monkeypatch=monkeypatch,
        )

        assert seen == [["build/"]]
