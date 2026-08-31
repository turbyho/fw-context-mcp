"""Storing what a linker script defines.

The reader is covered by `tests/test_linker_script.py`, rule by rule from
the GNU ld manual.  These tests cover the pass that writes the result: what
it stores, what it leaves to the compiled code, and what a re-index
corrects.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.db import (
    insert_symbols_batch,
    open_db,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
)
from fw_context_mcp.indexer.linker_script import store_scripts

SCRIPT = """\
MEMORY
{
    FLASH (rx) : ORIGIN = 0x10200, LENGTH = 0xefe00
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 0x40000
}
ENTRY(Reset_Handler)
SECTIONS
{
    .data : { _sdata = .; *(.data) } > RAM AT > FLASH
    __StackTop = ORIGIN(RAM) + LENGTH(RAM);
    PROVIDE(__stack = __StackTop);
    _limit = 0x400;
}
"""


def _db(tmp_path: Path):
    conn = open_db(tmp_path / "index.db")
    with transaction(conn):
        upsert_project(conn, "pid", "p", str(tmp_path))
        upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
    return conn


def _script(tmp_path: Path, text: str = SCRIPT, name: str = "app.ld") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _rows(conn) -> dict[str, dict]:
    return {
        row["name"]: row
        for row in conn.execute(
            "SELECT name, usr, kind, line, is_definition, is_weak, signature, "
            "file_path, is_project FROM symbols WHERE config_hash='ch'"
        )
    }


def _define_in_c(conn, name: str, line: int = 7) -> None:
    """Put a definition of *name* in the index, as a C unit would."""
    with transaction(conn):
        file_id = upsert_file(conn, "ch", "main.c", "c")
        insert_symbols_batch(conn, [(
            "ch", file_id, "main.c", name, f"c:@{name}", name, name,
            "varglobal", line, 1, line, 1,
            "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
        )])


class TestWhatItStores:
    def test_every_symbol_of_the_script(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(
                    conn, "ch", [_script(tmp_path)], tmp_path
                )
            rows = _rows(conn)
        finally:
            conn.close()
        assert result.files == 1
        assert set(rows) == {"_sdata", "__StackTop", "__stack", "_limit"}
        assert result.symbols == 4

    def test_the_line_and_the_file(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [_script(tmp_path)], tmp_path)
            rows = _rows(conn)
        finally:
            conn.close()
        # Line 10 of SCRIPT: MEMORY, {, FLASH, RAM, }, ENTRY, SECTIONS, {,
        # .data, __StackTop.  The assignment follows a `} > RAM AT > FLASH`
        # placement suffix on the line before, which is the case that an
        # anchored match loses.
        assert rows["__StackTop"]["line"] == 10
        assert rows["__StackTop"]["file_path"] == "app.ld"

    def test_the_expression_travels_as_the_signature(self, tmp_path):
        # `ORIGIN(RAM) + LENGTH(RAM)` says more about __StackTop than an
        # address would, and `get_symbol_context` shows a signature.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [_script(tmp_path)], tmp_path)
            rows = _rows(conn)
        finally:
            conn.close()
        assert rows["__StackTop"]["signature"] == "ORIGIN(RAM) + LENGTH(RAM)"
        assert rows["_limit"]["signature"] == "0x400"

    def test_a_symbol_is_a_definition_and_a_global_variable(self, tmp_path):
        # The manual: an assignment will "define the symbol and place it into
        # the symbol table with a global scope".
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [_script(tmp_path)], tmp_path)
            rows = _rows(conn)
        finally:
            conn.close()
        assert rows["_limit"]["is_definition"] == 1
        assert rows["_limit"]["kind"] == "varglobal"

    def test_a_provide_symbol_is_weak(self, tmp_path):
        # PROVIDE defines the symbol only when something references it and
        # nothing else defines it, which is what weak means to the linker.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [_script(tmp_path)], tmp_path)
            rows = _rows(conn)
        finally:
            conn.close()
        assert rows["__stack"]["is_weak"] == 1
        assert rows["__StackTop"]["is_weak"] == 0

    def test_the_usr_carries_the_namespace(self, tmp_path):
        # `_clear_previous_pass` deletes by this namespace, thus every row
        # this pass writes must be inside it.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [_script(tmp_path)], tmp_path)
            rows = _rows(conn)
        finally:
            conn.close()
        assert all(row["usr"].startswith("ld:") for row in rows.values())

    def test_the_entry_point_and_the_region_count(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(
                    conn, "ch", [_script(tmp_path)], tmp_path
                )
        finally:
            conn.close()
        assert result.entry == "Reset_Handler"
        assert result.regions == 2

    def test_the_script_path_travels_for_the_coverage_purge(self, tmp_path):
        # The purge counts a file no libclang unit covers as missing.  The
        # assembly pass lost every row it wrote to exactly that purge before
        # its paths were threaded through.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(
                    conn, "ch", [_script(tmp_path)], tmp_path
                )
        finally:
            conn.close()
        assert result.paths == {"app.ld"}

    def test_no_script_at_all(self, tmp_path):
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(conn, "ch", [], tmp_path)
        finally:
            conn.close()
        assert (result.files, result.symbols) == (0, 0)


class TestWhatItLeavesAlone:
    """A definition from the compiled code is the real one."""

    def test_a_name_the_c_code_defines(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _define_in_c(conn, "_limit")
            with transaction(conn):
                result = store_scripts(
                    conn, "ch", [_script(tmp_path)], tmp_path
                )
            rows = conn.execute(
                "SELECT usr, line FROM symbols WHERE config_hash='ch' "
                "AND name='_limit'"
            ).fetchall()
        finally:
            conn.close()
        assert result.skipped_defined == 1
        assert len(rows) == 1
        assert rows[0]["usr"] == "c:@_limit"

    def test_the_other_symbols_still_arrive(self, tmp_path):
        conn = _db(tmp_path)
        try:
            _define_in_c(conn, "_limit")
            with transaction(conn):
                store_scripts(conn, "ch", [_script(tmp_path)], tmp_path)
            names = set(_rows(conn))
        finally:
            conn.close()
        assert {"_sdata", "__StackTop", "__stack"} <= names

    def test_a_declaration_does_not_block_the_script(self, tmp_path):
        # Only a DEFINITION wins.  A declaration says the name exists
        # somewhere, which is exactly what the script answers.
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                file_id = upsert_file(conn, "ch", "startup.S", "c")
                insert_symbols_batch(conn, [(
                    "ch", file_id, "startup.S", "__StackTop",
                    "asm:@__StackTop", "__StackTop", "__StackTop",
                    "undefined", 3, 1, 3, 0,
                    "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
                )])
            with transaction(conn):
                result = store_scripts(
                    conn, "ch", [_script(tmp_path)], tmp_path
                )
            usrs = {
                row["usr"] for row in conn.execute(
                    "SELECT usr FROM symbols WHERE config_hash='ch' "
                    "AND name='__StackTop'"
                )
            }
        finally:
            conn.close()
        assert result.skipped_defined == 0
        assert any(usr.startswith("ld:") for usr in usrs)


class TestReindex:
    """A re-index must be able to correct what the pass wrote.

    `insert_symbols_batch` merges on `ON CONFLICT(config_hash, usr)` behind a
    `WHERE excluded.is_definition = 1 AND symbols.is_definition = 0` guard.
    Every symbol here is a definition, thus that guard never matches and an
    old row would survive forever without `_clear_previous_pass`.
    """

    def test_a_moved_symbol_gets_its_new_line(self, tmp_path):
        conn = _db(tmp_path)
        path = _script(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [path], tmp_path)
            first = _rows(conn)["_limit"]["line"]

            # Two more lines before the assignment.
            path.write_text(
                "/* one */\n/* two */\n" + SCRIPT, encoding="utf-8"
            )
            with transaction(conn):
                store_scripts(conn, "ch", [path], tmp_path)
            second = _rows(conn)["_limit"]["line"]
        finally:
            conn.close()
        assert second == first + 2

    def test_a_symbol_the_script_no_longer_defines_is_gone(self, tmp_path):
        conn = _db(tmp_path)
        path = _script(tmp_path)
        try:
            with transaction(conn):
                store_scripts(conn, "ch", [path], tmp_path)
            assert "_limit" in _rows(conn)

            path.write_text(SCRIPT.replace("_limit = 0x400;", ""),
                            encoding="utf-8")
            with transaction(conn):
                store_scripts(conn, "ch", [path], tmp_path)
            names = set(_rows(conn))
        finally:
            conn.close()
        assert "_limit" not in names

    def test_a_re_index_does_not_double_the_rows(self, tmp_path):
        conn = _db(tmp_path)
        path = _script(tmp_path)
        try:
            for _ in range(3):
                with transaction(conn):
                    store_scripts(conn, "ch", [path], tmp_path)
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash='ch'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 4

    def test_another_config_keeps_its_rows(self, tmp_path):
        # The delete is keyed on config_hash as well as the namespace, thus
        # one variant of a multi-build project cannot clear another.
        conn = _db(tmp_path)
        path = _script(tmp_path)
        try:
            with transaction(conn):
                upsert_build_config(conn, "other", "pid", str(tmp_path))
            with transaction(conn):
                store_scripts(conn, "other", [path], tmp_path)
            with transaction(conn):
                store_scripts(conn, "ch", [path], tmp_path)
            other = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash='other'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert other == 4


class TestSeveralScripts:
    """ESP-IDF passes about ten scripts, and Zephyr passes two."""

    def test_the_first_script_that_names_a_symbol_wins(self, tmp_path):
        first = _script(tmp_path, "_shared = 1;\n_only_a = 2;\n", "a.ld")
        second = _script(tmp_path, "_shared = 9;\n_only_b = 3;\n", "b.ld")
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(conn, "ch", [first, second], tmp_path)
            rows = _rows(conn)
        finally:
            conn.close()
        assert result.files == 2
        assert rows["_shared"]["file_path"] == "a.ld"
        assert rows["_shared"]["signature"] == "1"
        assert {"_only_a", "_only_b"} <= set(rows)

    def test_a_script_that_cannot_be_read(self, tmp_path):
        good = _script(tmp_path, "_x = 1;\n", "good.ld")
        conn = _db(tmp_path)
        try:
            with transaction(conn):
                result = store_scripts(
                    conn, "ch", [tmp_path / "gone.ld", good], tmp_path
                )
            names = set(_rows(conn))
        finally:
            conn.close()
        assert result.files == 1
        assert names == {"_x"}
