"""The stored text of an assembly unit and of each file that it includes.

The text follows the record that the preprocessor keeps of the branches it
skipped, as on the C path.  The output of ``clang -E`` cannot give it: the
preprocessor emits no line for a directive, and it emits blank lines for a
short skipped block.  Measured over 58 assembly units of 13 real builds, the
stored text of the ``-E`` path dropped about 1.19 million directive lines
(``#define`` and ``#include`` among them) and showed 5289 lines of skipped
branches as live code.  The record of libclang agreed with ``-E`` on every
line that ``-E`` emitted as text, in the two directions.

A header that a C unit also reads keeps the text of the C path.  Before
this, the assembly pass wrote its own view over it: ``search_content`` found
no ``DT_N_CHILD_NUM`` in the devicetree header of a Zephyr build, and
``read_file`` gave a header of the architecture as blank lines below its
copyright block.

The views of two assembly units join in one run only.  A view of an earlier
run is never read back: it holds the lines of the branches that the build
took then, and a join with it cannot remove a line.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer import asm
from fw_context_mcp.indexer.asm import store_units
from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_project
from fw_context_mcp.indexer.runner import _files_read_by_c_units

pytestmark = pytest.mark.skipif(shutil.which("clang") is None, reason="the preprocessor is clang")

CH = "ch"


def _unit(tmp_path: Path, name: str, text: str | None = None, args: list[str] | None = None):
    src = tmp_path / name
    if text is not None:
        src.write_text(text, encoding="utf-8")
    return SimpleNamespace(file=src, directory=tmp_path, clang_args=args or [])


@pytest.fixture
def conn(tmp_path: Path):
    connection = open_db(tmp_path / "index.db")
    with transaction(connection):
        upsert_project(connection, "pid", "p", str(tmp_path))
        upsert_build_config(connection, CH, "pid", str(tmp_path / "cc.json"))
    yield connection
    connection.close()


def _store(conn, tmp_path: Path, *units, c_read: frozenset[Path] = frozenset()):
    with transaction(conn):
        return store_units(conn, CH, list(units), tmp_path, c_read=c_read)


def _lines(conn, path: str) -> list[str]:
    row = conn.execute("SELECT content FROM files WHERE config_hash=? AND path=?", (CH, path)).fetchone()
    assert row is not None, f"no row for {path}"
    return row["content"].split("\n")


def _store_c_text(conn, path: str, content: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO files (config_hash, path, language, content) VALUES (?, ?, 'c', ?)",
            (CH, path, content),
        )


def test_a_directive_stays_in_the_text_of_the_unit_and_of_its_header(conn, tmp_path: Path):
    """A directive compiles: it is part of the answer, as on the C path."""
    (tmp_path / "defs.h").write_text("/* offsets */\n#define TCB_SP 56\n", encoding="utf-8")
    unit = _unit(tmp_path, "t.S", '#include "defs.h"\n#define TWICE(x) x, x\n  .long TWICE(TCB_SP)\n')

    _store(conn, tmp_path, unit)

    assert _lines(conn, "t.S")[:3] == ['#include "defs.h"', "#define TWICE(x) x, x", "  .long TWICE(TCB_SP)"]
    assert _lines(conn, "defs.h")[:2] == ["/* offsets */", "#define TCB_SP 56"]


def test_a_short_skipped_branch_is_blank(conn, tmp_path: Path):
    """cpp emits blank lines for a short skipped block, not a linemarker.

    The -E path took those blank lines for emitted lines and stored the
    text of the skipped block as live code.
    """
    unit = _unit(tmp_path, "t.S", "  .long Taken\n#if 0\n  .long NeverTaken\n#endif\n  .long After\n")

    _store(conn, tmp_path, unit)

    assert _lines(conn, "t.S")[:5] == ["  .long Taken", "", "", "", "  .long After"]


def test_a_header_that_c_reads_keeps_the_text_of_the_c_path(conn, tmp_path: Path):
    header = tmp_path / "shared.h"
    header.write_text("#ifndef __ASSEMBLER__\nint c_side;\n#else\n.equ ASM_SIDE, 1\n#endif\n", encoding="utf-8")
    c_view = "#ifndef __ASSEMBLER__\nint c_side;\n\n\n\n"
    _store_c_text(conn, "shared.h", c_view)
    unit = _unit(tmp_path, "t.S", '#include "shared.h"\n')

    _store(conn, tmp_path, unit, c_read=frozenset({header.resolve()}))

    assert "\n".join(_lines(conn, "shared.h")) == c_view


def test_a_header_that_c_reads_gets_no_text_from_this_pass_even_when_c_left_it_empty(conn, tmp_path: Path):
    """C keeps a header empty when it compiled no line of it.

    A rule "write when the row is empty" filled such a header with the
    assembly view.  The next run took that text for the C text and kept it,
    with a branch that the build had stopped to take.
    """
    header = tmp_path / "shared.h"
    header.write_text("#ifdef __ASSEMBLER__\n.equ X, 1\n#endif\n", encoding="utf-8")
    _store_c_text(conn, "shared.h", "")
    unit = _unit(tmp_path, "t.S", '#include "shared.h"\n')

    result = _store(conn, tmp_path, unit, c_read=frozenset({header.resolve()}))

    assert _lines(conn, "shared.h") == [""]
    assert "shared.h" in result.paths, "the row must stay covered"


def test_a_header_that_only_assembly_reads_takes_the_view_of_this_run(conn, tmp_path: Path):
    """A stored text of an earlier run must not add its lines."""
    header = tmp_path / "only_asm.h"
    header.write_text("#if 0\nold_live_line\n#endif\n", encoding="utf-8")
    _store_c_text(conn, "only_asm.h", "\nold_live_line\n\n")
    unit = _unit(tmp_path, "t.S", '#include "only_asm.h"\n')

    _store(conn, tmp_path, unit)

    assert _lines(conn, "only_asm.h")[:3] == ["", "", ""]


def test_a_branch_that_the_build_stops_to_take_goes_blank_in_the_next_run(conn, tmp_path: Path):
    """The same text, other flags: the view of the earlier run must go.

    One config hash can cover this: a Kconfig change reaches the unit
    through an `-imacros` file, and the path of that file is all that the
    flags carry.
    """
    unit_text = "#ifdef CONFIG_FPU\n  .long FpuOnly\n#endif\n"
    with_fpu = _unit(tmp_path, "t.S", unit_text, args=["-DCONFIG_FPU"])
    _store(conn, tmp_path, with_fpu)
    assert _lines(conn, "t.S")[1] == "  .long FpuOnly"

    _store(conn, tmp_path, _unit(tmp_path, "t.S", args=[]))

    assert _lines(conn, "t.S")[:3] == ["", "", ""]


def test_an_edit_of_the_unit_between_two_runs_gives_the_view_of_the_new_text(conn, tmp_path: Path):
    """The pass writes the hash of the file when it stores a symbol of it.

    A join guarded by that hash took the old text for the new one and kept
    the old live lines.
    """
    _store(conn, tmp_path, _unit(tmp_path, "t.S", "  nop\n  nop\n  nop\n"))

    _store(conn, tmp_path, _unit(tmp_path, "t.S", "#if 0\n  .long NeverTaken\n#endif\n"))

    assert _lines(conn, "t.S")[:3] == ["", "", ""]


def test_two_units_that_read_one_header_join_their_live_lines(conn, tmp_path: Path):
    """A line is live when one assembly unit of the run compiled it."""
    (tmp_path / "both.h").write_text("#ifdef SIDE_A\n.equ A, 1\n#else\n.equ B, 2\n#endif\n", encoding="utf-8")
    first = _unit(tmp_path, "a.S", '#include "both.h"\n', args=["-DSIDE_A"])
    second = _unit(tmp_path, "b.S", '#include "both.h"\n')

    _store(conn, tmp_path, first, second)

    assert _lines(conn, "both.h")[:5] == ["#ifdef SIDE_A", ".equ A, 1", "", ".equ B, 2", "#endif"]


def test_a_unit_that_libclang_cannot_load_keeps_its_files_covered(conn, tmp_path: Path, monkeypatch):
    """The coverage purge deletes each row that no unit covers.

    The symbols of the unit are stored, thus its files must stay covered.
    """
    monkeypatch.setattr(asm, "read_unit_texts", lambda unit: None)
    unit = _unit(tmp_path, "t.S", "  .global Foo\nFoo:\n  b .\n")

    result = _store(conn, tmp_path, unit)

    assert "t.S" in result.paths


def test_a_second_run_gives_the_same_text(conn, tmp_path: Path):
    """The pass runs on each index run, thus its result must be stable."""
    unit = _unit(tmp_path, "t.S", "  .long A\n#if 0\n  .long B\n#endif\n")

    _store(conn, tmp_path, unit)
    first = _lines(conn, "t.S")
    _store(conn, tmp_path, unit)

    assert _lines(conn, "t.S") == first


class TestFilesReadByCUnits:
    def test_a_parsed_unit_gives_its_headers_and_itself(self, tmp_path: Path):
        unit = SimpleNamespace(file=tmp_path / "main.c")
        headers = {"main.c": {"inc/api.h", "/opt/sdk/include/core.h"}}

        read = _files_read_by_c_units([unit], headers, None, tmp_path)

        assert read == {
            (tmp_path / "main.c").resolve(),
            (tmp_path / "inc" / "api.h").resolve(),
            Path("/opt/sdk/include/core.h").resolve(),
        }

    def test_a_unit_that_this_run_did_not_parse_gives_the_headers_of_its_manifest_entry(self, tmp_path: Path):
        unit = SimpleNamespace(file=tmp_path / "drv.c")
        manifest_lookup = {"drv.c": [{"file": "drv.c", "headers": ["inc/drv.h"]}]}

        read = _files_read_by_c_units([unit], {}, manifest_lookup, tmp_path)

        assert (tmp_path / "inc" / "drv.h").resolve() in read
