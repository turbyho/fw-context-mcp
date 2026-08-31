"""The linker-script pass, through `runner.run` end to end.

`tests/test_linker_script.py` covers the grammar, `test_linker_storage.py`
the writing, and `test_builders/test_linker_discovery.py` the discovery.
None of them proves that a real index run reaches the pass at all — and a
run of zbox-ecb-fw produced no `ld:` symbol while every part passed its own
tests, which is exactly the gap this file closes.

The project is synthetic and holds one small C file, so the run takes about
a second.  `build.ninja` names the script the way a CMake build does, thus
the `cmake` backend finds it with no vendor knowledge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import (
    get_entry_point,
    get_memory_regions,
    open_db,
)

pytest.importorskip("clang.cindex", reason="libclang not available")

SCRIPT = """\
MEMORY
{
    FLASH (rx) : ORIGIN = 0x8000000, LENGTH = 64K
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 8K
}
ENTRY(Reset_Handler)
SECTIONS
{
    .text : { *(.text) } > FLASH
    _etext = .;
    .data : { _sdata = .; *(.data) } > RAM AT > FLASH
    _estack = ORIGIN(RAM) + LENGTH(RAM);
    PROVIDE(__heap_start = _sdata);
}
"""

SOURCE = """\
int shared_with_the_script = 0;

int main(void) {
    return shared_with_the_script;
}
"""


@pytest.fixture
def ninja_project(tmp_path: Path) -> Path:
    """A project whose build.ninja names a linker script with `-T`."""
    (tmp_path / "main.c").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "app.ld").write_text(SCRIPT, encoding="utf-8")
    # The flag as a real ninja file writes it — TWO spaces after `-T`,
    # measured on zbox-ecb-fw-v5.
    (tmp_path / "build.ninja").write_text(
        "rule link\n"
        "  command = ld $in  -T  app.ld  -o $out\n",
        encoding="utf-8",
    )
    (tmp_path / "compile_commands.json").write_text(
        json.dumps([
            {
                "directory": str(tmp_path),
                "file": "main.c",
                "arguments": ["cc", "-c", "main.c", "-o", "main.o"],
            }
        ]),
        encoding="utf-8",
    )
    return tmp_path


def _index(project: Path, db_path: Path) -> str:
    from fw_context_mcp.indexer.runner import run

    return run(
        compile_commands=project / "compile_commands.json",
        db_path=db_path,
        project_root=project,
        project_id="testpid",
        project_name="ninja_project",
        # `cmake` is the config key of the generic CMake backend, which reads
        # `-T` from build.ninja.  Passed rather than detected: marker
        # detection would call this directory something else, and the point
        # here is the pass, not the detection.
        build_system="cmake",
        index_refs=False,
        index_embeddings=False,
        analyze_symbols=False,
        analyze_overrides=False,
    )


class TestTheRunReachesThePass:
    def test_the_symbols_of_the_script_are_in_the_index(
        self, ninja_project, tmp_path
    ):
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            rows = {
                row["name"]: row
                for row in conn.execute(
                    "SELECT name, usr, kind, signature, is_weak FROM symbols "
                    "WHERE config_hash=? AND usr LIKE 'ld:%'",
                    (config_hash,),
                )
            }
        finally:
            conn.close()
        assert set(rows) == {"_etext", "_sdata", "_estack", "__heap_start"}
        assert rows["_estack"]["signature"] == "ORIGIN(RAM) + LENGTH(RAM)"
        assert rows["__heap_start"]["is_weak"] == 1

    def test_the_memory_map_is_in_the_index(self, ninja_project, tmp_path):
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            regions = get_memory_regions(conn, config_hash)
            entry = get_entry_point(conn, config_hash)
        finally:
            conn.close()
        assert [r["name"] for r in regions] == ["FLASH", "RAM"]
        assert regions[0]["origin_value"] == 0x8000000
        assert regions[0]["length_value"] == 64 * 1024
        assert entry == "Reset_Handler"

    def test_the_c_definition_wins_over_the_script(
        self, ninja_project, tmp_path
    ):
        # The pass runs after the C units so a compiled definition keeps the
        # name.  The script assigns `shared_with_the_script`, which main.c
        # also defines.
        script = ninja_project / "app.ld"
        script.write_text(
            SCRIPT + "shared_with_the_script = 0x1234;\n", encoding="utf-8"
        )
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            usrs = {
                row["usr"]
                for row in conn.execute(
                    "SELECT usr FROM symbols WHERE config_hash=? AND name=?",
                    (config_hash, "shared_with_the_script"),
                )
            }
        finally:
            conn.close()
        assert usrs, "main.c defines the name, so a row must exist"
        assert not any(usr.startswith("ld:") for usr in usrs)

    def test_a_project_whose_build_names_no_script(
        self, ninja_project, tmp_path
    ):
        # No build.ninja: the pass must find nothing and the run must finish.
        (ninja_project / "build.ninja").unlink()
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? "
                "AND usr LIKE 'ld:%'",
                (config_hash,),
            ).fetchone()[0]
            regions = get_memory_regions(conn, config_hash)
        finally:
            conn.close()
        assert count == 0
        assert regions == []

    def test_a_second_run_does_not_double_the_symbols(
        self, ninja_project, tmp_path
    ):
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        _index(ninja_project, db_path)
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash=? "
                "AND usr LIKE 'ld:%'",
                (config_hash,),
            ).fetchone()[0]
            regions = get_memory_regions(conn, config_hash)
        finally:
            conn.close()
        assert count == 4
        assert len(regions) == 2

    def test_no_undefined_row_is_left_for_a_script_symbol(
        self, ninja_project, tmp_path
    ):
        """The reason the pass runs BEFORE assembly.

        A vector table names its initial stack pointer, and no compiled file
        defines it — the linker script does.  The assembly reader used to
        add a `kind="undefined"` placeholder for it, one per CMSIS project:
        `_estack` on FM and `__StackTop` on zbox.

        With the linker pass in front, `asm._declare_referenced_only` finds
        the name already in the index and adds nothing.  This is the one
        claim of the ordering, and no other test covers it: the storage test
        proves the reverse direction, that a declaration already in the
        index does not block the script.
        """
        (ninja_project / "startup.S").write_text(
            '  .section .isr_vector, "a"\n'
            "  .word _estack\n"
            "  .word Reset_Handler\n"
            "Reset_Handler:\n"
            "  b .\n",
            encoding="utf-8",
        )
        cc = ninja_project / "compile_commands.json"
        entries = json.loads(cc.read_text(encoding="utf-8"))
        entries.append({
            "directory": str(ninja_project),
            "file": "startup.S",
            "arguments": ["cc", "-x", "assembler-with-cpp", "-c", "startup.S",
                          "-o", "startup.o"],
        })
        cc.write_text(json.dumps(entries), encoding="utf-8")

        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            rows = conn.execute(
                "SELECT usr, kind, is_definition FROM symbols "
                "WHERE config_hash=? AND name='_estack'",
                (config_hash,),
            ).fetchall()
        finally:
            conn.close()
        kinds = {row["kind"] for row in rows}
        usrs = {row["usr"] for row in rows}
        assert "undefined" not in kinds, (
            f"the placeholder came back: {[dict(r) for r in rows]}"
        )
        assert any(usr.startswith("ld:") for usr in usrs), (
            "the script's definition of _estack is missing"
        )

    def test_the_script_survives_the_coverage_purge(
        self, ninja_project, tmp_path
    ):
        # The purge counts a file no libclang unit covers as missing.  The
        # assembly pass lost every row it wrote to exactly that purge before
        # its paths were threaded through, thus the script needs the same
        # treatment — see runner.run, `uncovered_paths`.
        db_path = tmp_path / "db" / "index.db"
        db_path.parent.mkdir()
        config_hash = _index(ninja_project, db_path)
        conn = open_db(db_path)
        try:
            row = conn.execute(
                "SELECT path FROM files WHERE config_hash=? AND path LIKE '%app.ld'",
                (config_hash,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "the script's file row was purged"
