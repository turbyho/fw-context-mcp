"""Assembly reaches the index through the preprocessor, not through libclang.

libclang reads a translation unit as C or C++.  Handed a `.S` it parses it
as C and yields no cursor and one error diagnostic — measured on a Zephyr
startup.S.  So `runner.run()` keeps skipping assembly for libclang, and
this path picks it up instead.

`.S` with a capital letter means, in the gcc suffix table, "assembly that
must be preprocessed".  Running the preprocessor is what the build does.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer.asm import filtered_content, preprocess

pytestmark = pytest.mark.skipif(
    shutil.which("clang") is None, reason="the preprocessor is clang"
)


def _unit(tmp_path: Path, name: str, text: str, args: list[str] | None = None):
    src = tmp_path / name
    src.write_text(text, encoding="utf-8")
    return SimpleNamespace(file=src, directory=tmp_path, clang_args=args or [])


class TestPreprocess:
    def test_an_inactive_branch_does_not_reach_the_output(self, tmp_path: Path):
        """The first version of this plan called `#if` filtering impossible."""
        unit = _unit(tmp_path, "t.S",
                     "  .long Taken\n#if 0\n  .long NeverTaken\n#endif\n")

        src = preprocess(unit)

        assert src is not None
        assert "Taken" in src.text
        assert "NeverTaken" not in src.text

    def test_a_macro_definition_is_expanded(self, tmp_path: Path):
        """Zephyr writes GTEXT(sym), not `.globl sym`.

        A regex over the raw text finds nothing — measured, it returned
        zero on every real assembly file in the test projects.
        """
        unit = _unit(tmp_path, "t.S",
                     "#define GTEXT(s) .global s\nGTEXT(HardFault_Handler)\n")

        src = preprocess(unit)

        assert ".global HardFault_Handler" in src.text

    def test_comments_survive(self, tmp_path: Path):
        """Without -C the preprocessor strips them.

        Measured on irq_cm4f.S: 63 of 228 lines would have been blanked out
        of files.content, including the @brief block search_content is
        meant to find.
        """
        unit = _unit(tmp_path, "t.S", "/* @brief Fault handlers */\n  .long X\n")

        src = preprocess(unit)

        assert "@brief Fault handlers" in src.text

    def test_every_line_keeps_its_source_and_number(self, tmp_path: Path):
        unit = _unit(tmp_path, "t.S", "  .long A\n  .long B\n  .long C\n")

        src = preprocess(unit)

        placed = [m for m in src.line_map if m is not None]
        resolved = str((tmp_path / "t.S").resolve())
        assert (resolved, 1) in placed
        assert (resolved, 3) in placed

    def test_an_included_file_is_attributed_to_itself(self, tmp_path: Path):
        """FM's startup unit is five lines of #include; the table is elsewhere."""
        (tmp_path / "table.inc").write_text("  .long HardFault_Handler\n",
                                            encoding="utf-8")
        unit = _unit(tmp_path, "t.S", '#include "table.inc"\n')

        src = preprocess(unit)

        assert str((tmp_path / "table.inc").resolve()) in src.active

    def test_one_spelling_per_file(self, tmp_path: Path):
        """cpp echoes paths as the command line gave them.

        Measured before this was fixed: the same file arrived both as
        `./mbed-os/…/except.S` and absolute, and landed in the map twice —
        once unreadable.
        """
        unit = _unit(tmp_path, "t.S", "  .long A\n", args=["-I."])

        src = preprocess(unit)

        assert len(src.active) == len({Path(p).resolve() for p in src.active})
        assert all(Path(p).is_absolute() for p in src.active)

    def test_a_unit_that_cannot_be_preprocessed_returns_none(self, tmp_path: Path):
        """Never guessed at: half a file read without its macros is worse."""
        unit = _unit(tmp_path, "t.S", '#include "nowhere-at-all.h"\n')

        assert preprocess(unit) is None

    def test_a_missing_file_returns_none(self, tmp_path: Path):
        unit = SimpleNamespace(file=tmp_path / "absent.S", directory=tmp_path,
                               clang_args=[])

        assert preprocess(unit) is None


class TestFilteredContent:
    def test_line_numbers_are_preserved(self, tmp_path: Path):
        """Same convention as the C path: an inactive line becomes a newline."""
        f = tmp_path / "t.S"
        f.write_text("one\ntwo\nthree\n", encoding="utf-8")

        content = filtered_content(str(f), {1, 3})

        assert content.splitlines() == ["one", "", "three"]

    def test_an_unreadable_file_gives_none(self, tmp_path: Path):
        assert filtered_content(str(tmp_path / "absent.S"), {1}) is None

    def test_an_empty_file_gives_none(self, tmp_path: Path):
        f = tmp_path / "empty.S"
        f.write_text("", encoding="utf-8")

        assert filtered_content(str(f), set()) is None

    def test_an_inactive_branch_is_blanked_not_dropped(self, tmp_path: Path):
        f = tmp_path / "t.S"
        f.write_text("active\ninactive\nactive2\n", encoding="utf-8")

        content = filtered_content(str(f), {1, 3})

        assert "inactive" not in content
        assert content.splitlines()[2] == "active2", "line 3 must stay line 3"


class TestStoreUnits:
    """An assembly unit and its includes each get a files row with text.

    Rows and nothing else: libclang never saw these units, so they
    contribute no symbol, and a caller that finds a row here must not
    conclude the file was parsed.
    """

    @staticmethod
    def _db(tmp_path: Path):
        from fw_context_mcp.indexer.db import (
            open_db,
            transaction,
            upsert_build_config,
            upsert_project,
        )

        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
        return conn

    def test_the_unit_gets_a_row_with_its_text(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "startup.S",
                     "/* @brief vector table */\n  .long HardFault_Handler\n")
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                stored, failed = store_units(conn, "ch", [unit], tmp_path)
            row = conn.execute(
                "SELECT path, content FROM files WHERE config_hash='ch'"
            ).fetchone()
        finally:
            conn.close()

        assert (stored, failed) == (1, 0)
        assert row["path"] == "startup.S"
        assert "HardFault_Handler" in row["content"]
        assert "@brief vector table" in row["content"], "comments must survive"

    def test_an_included_file_gets_its_own_row(self, tmp_path: Path):
        """FM's startup unit is five lines; the 194-entry table is included."""
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        (tmp_path / "table.inc").write_text("  .long HardFault_Handler\n",
                                            encoding="utf-8")
        unit = _unit(tmp_path, "startup.S", '#include "table.inc"\n')
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            paths = {r["path"] for r in conn.execute(
                "SELECT path FROM files WHERE config_hash='ch'")}
        finally:
            conn.close()

        assert paths == {"startup.S", "table.inc"}

    def test_an_inactive_branch_is_not_in_the_stored_text(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "t.S",
                     "  .long Taken\n#if 0\n  .long NeverTaken\n#endif\n")
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            content = conn.execute(
                "SELECT content FROM files WHERE config_hash='ch' AND path='t.S'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert "Taken" in content
        assert "NeverTaken" not in content

    def test_a_failing_unit_is_counted_not_raised(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        good = _unit(tmp_path, "good.S", "  .long A\n")
        bad = _unit(tmp_path, "bad.S", '#include "nowhere.h"\n')
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                stored, failed = store_units(conn, "ch", [good, bad], tmp_path)
        finally:
            conn.close()

        assert (stored, failed) == (1, 1), "one unit must not lose the other"

    def test_the_row_carries_mtime_and_hash_together(self, tmp_path: Path):
        """The pair the staleness checks need; see db/_files.py upsert_file."""
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction
        from fw_context_mcp.utils import compute_source_hash

        unit = _unit(tmp_path, "t.S", "  .long A\n")
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            row = conn.execute(
                "SELECT mtime, source_hash FROM files WHERE config_hash='ch'"
            ).fetchone()
        finally:
            conn.close()

        assert row["mtime"] > 0
        assert row["source_hash"] == compute_source_hash(tmp_path / "t.S")

    def test_no_symbols_are_created(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "t.S", "  .global Foo\nFoo:\n  b .\n")
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            n = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash='ch'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert n == 0, "step 1 stores text; symbols are the next step"
