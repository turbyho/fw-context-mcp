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


# A vector table lives in its own section, because a linker script places
# that section at the reset address — the section is part of the contract
# between the startup file and the linker, not a matter of style.  A
# `.word` outside one is a constant: the STM32 startup writes five for
# its copy loop, and taking those for slots made the index claim the
# vector table reached `_sidata`.
#
# Fixtures that mean a TABLE therefore have to say so, exactly as a real
# startup does.
_VECTOR_SECTION = '    .section .vectors, "a"\n'


def _table(body: str) -> str:
    """Wrap *body* in a vector section, for a fixture that means a table."""
    return _VECTOR_SECTION + body


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
                result = store_units(conn, "ch", [unit], tmp_path)
            row = conn.execute(
                "SELECT path, content FROM files WHERE config_hash='ch'"
            ).fetchone()
        finally:
            conn.close()

        assert (result.files, result.failed) == (1, 0)
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
                result = store_units(conn, "ch", [good, bad], tmp_path)
        finally:
            conn.close()

        assert (result.files, result.failed) == (1, 1), (
            "one unit must not lose the other"
        )

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

    def test_the_symbols_come_from_the_preprocessor_not_libclang(self, tmp_path: Path):
        """libclang never saw this unit — it cannot read assembly.

        The symbols are real all the same, and their USR namespace says
        where they came from.
        """
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "t.S", "  .global Foo\nFoo:\n  b .\n")
        conn = self._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            rows = conn.execute(
                "SELECT name, usr FROM symbols WHERE config_hash='ch'"
            ).fetchall()
        finally:
            conn.close()

        assert [r["name"] for r in rows] == ["Foo"]
        assert all(r["usr"].startswith("asm:") for r in rows)


class TestExtractSymbols:
    """A label defines; `.global` and `.weak` say how, `.type` says what."""

    @staticmethod
    def _syms(tmp_path: Path, text: str):
        from fw_context_mcp.indexer.asm import extract_symbols

        src = preprocess(_unit(tmp_path, "t.S", text))
        assert src is not None
        return {s.name: s for s in extract_symbols(src)}

    def test_a_label_defines_a_symbol(self, tmp_path: Path):
        syms = self._syms(tmp_path, ".global Foo\nFoo:\n  b .\n")

        assert syms["Foo"].is_global
        assert syms["Foo"].kind == "function"
        assert syms["Foo"].line == 2, "the label is the definition, not the .global"

    def test_a_declaration_without_a_label_is_not_a_definition(self, tmp_path: Path):
        """`.global X` alone says X exists elsewhere."""
        syms = self._syms(tmp_path, ".global Elsewhere\n")

        assert "Elsewhere" not in syms

    def test_statements_separated_by_semicolons_are_read(self, tmp_path: Path):
        """The regression that returned nothing at all on Zephyr assembly.

        SECTION_FUNC expands to
        `.section .text.sym, "ax"; .thumb; .balign 4; sym :` — anchoring on
        the start of the line finds `.section` and never sees the label.
        """
        syms = self._syms(
            tmp_path,
            '#define SECTION_FUNC(s, sym) .section .text.sym, "ax"; .thumb; sym :\n'
            ".global z_SysNmiOnReset\n"
            "SECTION_FUNC(TEXT, z_SysNmiOnReset)\n"
            "  wfi\n",
        )

        assert "z_SysNmiOnReset" in syms
        assert syms["z_SysNmiOnReset"].is_global

    def test_a_macro_defined_symbol_is_found(self, tmp_path: Path):
        syms = self._syms(tmp_path,
                          "#define GTEXT(s) .global s\nGTEXT(Foo)\nFoo:\n  b .\n")

        assert syms["Foo"].is_global

    def test_type_object_is_a_variable(self, tmp_path: Path):
        syms = self._syms(tmp_path, ".type table, %object\ntable:\n  .long 0\n")

        assert syms["table"].kind == "variable"

    def test_a_label_without_a_type_is_a_function(self, tmp_path: Path):
        """GNU as does not require .type, and an exported label is code."""
        syms = self._syms(tmp_path, "Bare:\n  b .\n")

        assert syms["Bare"].kind == "function"

    def test_a_weak_symbol_is_marked(self, tmp_path: Path):
        syms = self._syms(tmp_path, ".weak Maybe\nMaybe:\n  b .\n")

        assert syms["Maybe"].is_weak

    def test_an_alias_records_its_target(self, tmp_path: Path):
        """CMSIS points an unimplemented interrupt at the default handler."""
        syms = self._syms(
            tmp_path,
            ".weak TIM1_IRQHandler\n.thumb_set TIM1_IRQHandler, Default_Handler\n"
            "Default_Handler:\n  b .\n",
        )

        assert syms["TIM1_IRQHandler"].alias_target == "Default_Handler"
        assert syms["TIM1_IRQHandler"].is_weak
        assert syms["Default_Handler"].alias_target is None

    def test_a_real_label_wins_over_an_alias_of_the_same_name(self, tmp_path: Path):
        syms = self._syms(tmp_path, ".set Both, Other\nBoth:\n  b .\n")

        assert syms["Both"].alias_target is None

    def test_the_directives_may_come_after_the_label(self, tmp_path: Path):
        """irq_cm4f.S writes .type, then .global, then the label."""
        syms = self._syms(tmp_path,
                          "Late:\n  b .\n.global Late\n.type Late, %function\n")

        assert syms["Late"].is_global


class TestStoredSymbols:
    def test_an_assembly_symbol_lands_in_the_index(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "startup.S", ".global Reset_Handler\nReset_Handler:\n  b .\n")
        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            row = conn.execute(
                "SELECT name, kind, usr, line FROM symbols WHERE config_hash='ch'"
            ).fetchone()
        finally:
            conn.close()

        assert row["name"] == "Reset_Handler"
        assert row["kind"] == "function"
        assert row["line"] == 2

    def test_the_usr_has_its_own_namespace(self, tmp_path: Path):
        """A handler defined in both C and assembly must not collide.

        except.S defines HardFault_Handler weakly and a project defines it
        strongly; both are true, and a shared USR would make one silently
        replace the other on the primary key.
        """
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "t.S", "HardFault_Handler:\n  b .\n")
        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            usr = conn.execute(
                "SELECT usr FROM symbols WHERE config_hash='ch'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert usr.startswith("asm:")
        assert not usr.startswith("c:")


class TestVectorTable:
    """A vector slot is the only edge an interrupt handler has.

    Nothing calls a handler by name; the hardware jumps to an address in a
    table.  Without the table in the index, HardFault_Handler had zero
    references and find_dead_code reported it — measured on zbox-ecb-fw,
    alongside BusFault_Handler.
    """

    @staticmethod
    def _vectors(tmp_path: Path, text: str):
        from fw_context_mcp.indexer.asm import extract_vectors

        src = preprocess(_unit(tmp_path, "t.S", _table(text)))
        assert src is not None
        return {v.name: v for v in extract_vectors(src)}

    def test_a_word_entry_names_a_handler(self, tmp_path: Path):
        vec = self._vectors(tmp_path, "  .word HardFault_Handler\n")

        # Line 2: _table puts the `.section` directive on line 1, exactly
        # as a startup file does.
        assert vec["HardFault_Handler"].line == 2
        assert vec["HardFault_Handler"].index == 0, "first slot of the table"

    def test_the_arm_dcd_spelling_is_read(self, tmp_path: Path):
        """mbed's ARM-toolchain startup files write DCD, not .word."""
        vec = self._vectors(tmp_path, "  DCD SVC_Handler\n")

        assert "SVC_Handler" in vec

    def test_a_reserved_slot_is_not_an_entry(self, tmp_path: Path):
        """A startup table holds dozens: measured on FM, 12 of 206."""
        vec = self._vectors(tmp_path, "  .word 0\n  .word 0x20000000\n")

        assert vec == {}

    def test_an_expression_is_not_an_entry(self, tmp_path: Path):
        """The IAR form, and `.word A + 4`, name no single symbol."""
        vec = self._vectors(tmp_path, "  .word sfe(CSTACK)\n  .word A + 4\n")

        assert vec == {}

    def test_a_repeated_line_yields_one_entry(self, tmp_path: Path):
        """One source line is one slot, however often it reaches the reader.

        This used to be about cpp: the reader handed clang the source file
        twice, so the whole unit came back doubled, and the dedupe here
        hid it.  The duplication is fixed at the source now — see
        `preprocess` — and the dedupe stays because a slot must not become
        two edges whatever produces the repeat.
        """
        from fw_context_mcp.indexer.asm import extract_vectors

        src = preprocess(_unit(tmp_path, "t.S", _table("  .word Foo\n")))
        src.line_map = src.line_map + src.line_map
        src.text = src.text + "\n" + src.text

        assert len(extract_vectors(src)) == 1


class TestVectorEdges:
    @staticmethod
    def _run(tmp_path: Path, asm: str, c_symbols: list[str] = ()):
        """Store *c_symbols* as C definitions, then index the assembly."""
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import (
            insert_symbols_batch,
            transaction,
            upsert_file,
        )

        conn = TestStoreUnits._db(tmp_path)
        with transaction(conn):
            if c_symbols:
                fid = upsert_file(conn, "ch", "src/main.c", "c")
                insert_symbols_batch(conn, [
                    ("ch", fid, "src/main.c", n, f"c:@F@{n}", n, n, "function",
                     10, 1, 12, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0)
                    for n in c_symbols
                ])
            result = store_units(conn, "ch", [_unit(tmp_path, "startup.S", _table(asm))],
                                 tmp_path)
        rows = conn.execute(
            "SELECT s.name, r.to_usr, r.from_file, r.from_line, r.from_usr, "
            "r.ref_kind "
            "FROM refs r JOIN symbols s ON s.usr=r.to_usr "
            "WHERE r.config_hash='ch' AND r.ref_kind='vector'"
        ).fetchall()
        conn.close()
        return result, {r["name"]: r for r in rows}

    def test_a_handled_slot_becomes_a_reference(self, tmp_path: Path):
        """The regression this whole plan exists for."""
        result, edges = self._run(
            tmp_path, "  .word HardFault_Handler\n", ["HardFault_Handler"]
        )

        assert result.vectors == 1
        edge = edges["HardFault_Handler"]
        assert edge["ref_kind"] == "vector"
        assert edge["from_usr"] is None, (
            "a table entry has no enclosing function; the column is nullable "
            "and already used that way by 6445 rows on zbox-ecb-fw"
        )
        assert edge["from_file"] == "startup.S"

    def test_a_default_aliased_slot_links_to_the_alias(self, tmp_path: Path):
        """An unserviced interrupt still has a slot, and the slot has a target.

        The slot exists, the hardware reaches it, and the alias it names is
        already a symbol in the index.  Withholding the one reference that
        symbol has would say it is unreferenced, which is false — measured
        on FM, that hid 64 of 97 slots.

        Whether the interrupt is serviced is read from where the edge LANDS:
        into the startup file means it reaches Default_Handler, into C means
        a handler runs.  Default_Handler itself gains nothing here, because
        the table names the per-interrupt alias, not the handler.
        """
        result, edges = self._run(
            tmp_path,
            "Default_Handler:\n  b .\n"
            "  .word TIM1_IRQHandler\n"
            ".weak TIM1_IRQHandler\n.thumb_set TIM1_IRQHandler, Default_Handler\n",
        )

        assert result.vectors == 1
        assert result.unresolved == 0
        assert "TIM1_IRQHandler" in edges, edges
        assert edges["TIM1_IRQHandler"]["to_usr"].startswith("asm:"), (
            "the edge lands in assembly, which is what says the slot traps"
        )
        assert "Default_Handler" not in edges, (
            "the table names the alias; Default_Handler must not collect an "
            "edge per interrupt"
        )

    def test_a_name_nothing_defines_is_declared_and_linked(self, tmp_path: Path):
        """`_estack` comes from the linker script, so no unit defines it.

        The slot used to disappear.  Two things about it are true and
        worth keeping: the name exists in the program, and the table
        reaches it from a known line — which is what `nm` calls `U`.
        """
        result, edges = self._run(tmp_path, "  .word _estack\n")

        assert result.vectors == 1
        assert result.unresolved == 0
        assert "_estack" in edges, edges
        assert edges["_estack"]["to_usr"] == "asm:@_estack", (
            "no file part: there is no defining file, and one row shared by "
            "every reference is the truth"
        )

    def test_the_declaration_is_not_a_definition(self, tmp_path: Path):
        """It says this file NAMES the symbol, not that it defines it."""
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch",
                            [_unit(tmp_path, "startup.S",
                                   _table("  .word _estack\n"))], tmp_path)
            row = conn.execute(
                "SELECT kind, is_definition FROM symbols WHERE name='_estack'"
            ).fetchone()
        finally:
            conn.close()

        assert row["is_definition"] == 0
        assert row["kind"] == "undefined", (
            "guessing between function and variable would be wrong half the "
            "time: a linker script's _estack is an address, a handler from a "
            "prebuilt library is code"
        )

    def test_an_ambiguous_name_is_still_left_unresolved(self, tmp_path: Path):
        """Not a missing symbol but one with two answers.

        Two units define the same name strongly, so the resolver cannot
        choose and the slot stays unlinked.  Declaring it as well would
        add a third row and make the ambiguity worse, not better.
        """
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        first = _unit(tmp_path, "one.S", ".global Twice\nTwice:\n  b .\n")
        (tmp_path / "sub").mkdir()
        second = _unit(tmp_path / "sub", "two.S",
                       _table(".global Twice\nTwice:\n  b .\n  .word Twice\n"))

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                result = store_units(conn, "ch", [first, second], tmp_path)
            rows = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE name='Twice'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert result.vectors == 0
        assert result.unresolved == 1
        assert rows == 2, "no third row was added for an ambiguous name"

    def test_the_strong_definition_wins_over_the_weak_assembly_one(self, tmp_path: Path):
        """except.S defines HardFault_Handler weakly; a project defines it in C.

        The linker takes the strong one, and so must the edge.
        """
        result, edges = self._run(
            tmp_path,
            ".weak HardFault_Handler\nHardFault_Handler:\n  b .\n"
            "  .word HardFault_Handler\n",
            ["HardFault_Handler"],
        )

        assert result.vectors == 1
        conn_usr = edges["HardFault_Handler"]
        assert conn_usr["name"] == "HardFault_Handler"

    def test_a_slot_pointing_at_assembly_only_code_still_links(self, tmp_path: Path):
        """No C definition anywhere, but the assembly one is unambiguous."""
        result, edges = self._run(
            tmp_path, ".global Reset_Handler\nReset_Handler:\n  b .\n"
                      "  .word Reset_Handler\n"
        )

        assert result.vectors == 1
        assert "Reset_Handler" in edges


class TestAssemblySurvivesPostProcessing:
    """The coverage purge deletes any files row that is not part of the build.

    It builds that set from the libclang units and the manifest, and knows
    about neither assembly unit nor assembly include.  Measured on a real
    FM reindex: the log said "2 file(s), 99 symbol(s), 1 vector edge(s)" and
    the database held none of it — written and deleted in the same run.

    The end-to-end test through runner.run() did NOT catch this: a synthetic
    project has no manifest header lists, so the purge returns early.
    """

    def test_the_purge_keeps_the_paths_it_was_told_about(self, tmp_path: Path):
        from fw_context_mcp.indexer._postprocess import _step_purge_files_outside_build
        from fw_context_mcp.indexer.db import (
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
            # Twenty covered C files against two assembly ones, because the
            # step refuses to act when more than 20% of the rows look stale.
            # On the real FM index the assembly was 2 rows of about 600 —
            # 0.3%, far under the guard — which is why it was purged there
            # and why a fixture of four files hides the bug entirely.
            covered_c = [f"src/keep{i}.c" for i in range(20)]
            for path in [*covered_c, "src/api.h", "src/startup.S", "src/table.inc"]:
                upsert_file(conn, "ch", path, "c")

        # A manifest that covers the C units only — exactly what a real one
        # holds, because libclang never saw the assembly.
        #
        # The entries MUST carry a non-empty header list.  _build_coverage_set
        # returns None otherwise and the purge stops before it deletes
        # anything — a test built on an empty list passes without ever
        # reaching the line it means to check.
        manifest = {
            "entries": [{"file": c, "headers": ["src/api.h"]} for c in covered_c],
            "headers": {"src/api.h": {}},
        }
        ctx = {
            "config_hash": "ch",
            "project_root": tmp_path,
            "units": [SimpleNamespace(file=tmp_path / c) for c in covered_c],
            "effective_manifest": manifest,
            "asm_paths": {"src/startup.S", "src/table.inc"},
            "db_dir": tmp_path,
        }
        with transaction(conn):
            _step_purge_files_outside_build(conn, ctx)
        kept = {r["path"] for r in conn.execute(
            "SELECT path FROM files WHERE config_hash='ch'")}
        conn.close()

        assert "src/startup.S" in kept, (
            "an assembly unit belongs to the build; the purge has no other "
            "way to learn that"
        )
        assert "src/table.inc" in kept, "and so does what it includes"

    def test_a_file_outside_the_build_still_goes(self, tmp_path: Path):
        """The purge must keep doing its job.

        Ten covered files against one stale, because the step refuses to act
        when more than 20% of the rows look stale — that guard reads such a
        set as broken coverage data rather than a stale index.
        """
        from fw_context_mcp.indexer._postprocess import _step_purge_files_outside_build
        from fw_context_mcp.indexer.db import (
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
            covered = [f"src/keep{i}.c" for i in range(10)]
            for path in [*covered, "src/api.h", "src/dropped.c"]:
                upsert_file(conn, "ch", path, "c")

        units = [SimpleNamespace(file=tmp_path / c) for c in covered]
        ctx = {
            "config_hash": "ch",
            "project_root": tmp_path,
            "units": units,
            "effective_manifest": {
                "entries": [{"file": c, "headers": ["src/api.h"]} for c in covered],
                "headers": {"src/api.h": {}},
            },
            "asm_paths": set(),
            "db_dir": tmp_path,
        }
        with transaction(conn):
            _step_purge_files_outside_build(conn, ctx)
        kept = {r["path"] for r in conn.execute(
            "SELECT path FROM files WHERE config_hash='ch'")}
        conn.close()

        assert "src/dropped.c" not in kept
        assert "src/keep0.c" in kept


class TestIsProjectClassification:
    """A startup file lives in the framework package, not in the project.

    Calling it project code put ninety weak interrupt aliases into
    `find_dead_code(project_only=True)`: measured on a real FM index, 91
    handlers reported where two real ones stood before assembly was indexed
    at all.  The rule is the one the C path uses.
    """

    def test_a_file_outside_the_project_is_vendor(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import _is_project_file

        outside = tmp_path.parent / "framework" / "startup.S"

        assert _is_project_file(str(outside), tmp_path, [], []) == 0

    def test_a_file_inside_the_project_is_project(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import _is_project_file

        inside = tmp_path / "src" / "startup.S"

        assert _is_project_file(str(inside), tmp_path, [], []) == 1

    def test_a_vendor_pattern_wins_inside_the_project(self, tmp_path: Path):
        """A vendored SDK committed into the tree is still not team code."""
        from fw_context_mcp.indexer.asm import _is_project_file

        vendored = tmp_path / "mbed-os" / "startup.S"

        assert _is_project_file(str(vendored), tmp_path, ["mbed-os/%"], []) == 0

    def test_a_project_pattern_wins_over_a_vendor_one(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import _is_project_file

        claimed = tmp_path / "mbed-os" / "targets_custom" / "startup.S"

        assert _is_project_file(
            str(claimed), tmp_path, ["mbed-os/%"], ["mbed-os/targets_custom/%"]
        ) == 1

    def test_the_stored_symbol_carries_the_verdict(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        vendor_root = tmp_path / "vendor"
        vendor_root.mkdir()
        unit = _unit(vendor_root, "startup.S", ".global Foo\nFoo:\n  b .\n")
        project_root = tmp_path / "proj"
        project_root.mkdir()

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], project_root)
            is_project = conn.execute(
                "SELECT is_project FROM symbols WHERE config_hash='ch'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert is_project == 0, (
            "the unit is outside the project, thus find_dead_code with "
            "project_only must not report its symbols"
        )

    def test_the_stored_file_carries_the_verdict_too(self, tmp_path: Path):
        """files.is_project is a separate column and a separate filter.

        It defaults to 0 and `upsert_file` cannot set it, thus a project
        that writes its own assembly would not find its own file through
        search_content(project_only=True) unless the content update sets it.
        """
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        project_root = tmp_path / "proj"
        project_root.mkdir()
        unit = _unit(project_root, "startup.S", ".global Foo\nFoo:\n  b .\n")

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], project_root)
            rows = dict(conn.execute(
                "SELECT path, is_project FROM files WHERE config_hash='ch'"
            ).fetchall())
        finally:
            conn.close()

        assert rows.get("startup.S") == 1, rows

    def test_a_vendor_file_row_stays_vendor(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        project_root = tmp_path / "proj"
        project_root.mkdir()
        (project_root / "mbed-os").mkdir()
        unit = _unit(project_root / "mbed-os", "startup.S",
                     ".global Foo\nFoo:\n  b .\n")

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], project_root,
                            vendor_patterns=["mbed-os/%"])
            rows = dict(conn.execute(
                "SELECT path, is_project FROM files WHERE config_hash='ch'"
            ).fetchall())
        finally:
            conn.close()

        assert rows.get("mbed-os/startup.S") == 0, rows


class TestReindexReplacesWhatItWrote:
    """A second run must be able to correct the first one.

    Two mechanisms hid the stale rows, and both were found on real data:

    * `insert_symbols_batch` merges on ON CONFLICT(config_hash, usr), but
      guards the UPDATE with `excluded.is_definition = 1 AND
      symbols.is_definition = 0`.  Every assembly symbol is a definition,
      so neither side matches and the row from the earlier run survives.
      An is_project fix looked like it had no effect until this was found:
      99 symbols, 99 still carrying the old verdict after a --force run.
    * `refs` has no unique constraint, so a vector edge is appended again
      every run.  FM reported one edge and held two rows.
    """

    @staticmethod
    def _store(conn, tmp_path: Path, body: str, name: str = "startup.S", **kw):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, name, body)
        with transaction(conn):
            return store_units(conn, "ch", [unit], tmp_path, **kw)

    def test_a_moved_symbol_gets_its_new_line(self, tmp_path: Path):
        conn = TestStoreUnits._db(tmp_path)
        try:
            self._store(conn, tmp_path, ".global Foo\nFoo:\n  b .\n")
            first = conn.execute(
                "SELECT line FROM symbols WHERE name='Foo'").fetchone()[0]
            # The same symbol, two lines further down.
            self._store(conn, tmp_path, "  .text\n  .text\n.global Foo\nFoo:\n  b .\n")
            second = conn.execute(
                "SELECT line FROM symbols WHERE name='Foo'").fetchone()[0]
        finally:
            conn.close()

        assert second == first + 2, (
            f"the row still carries the line of the previous run: "
            f"{first} then {second}"
        )

    def test_a_removed_symbol_goes_away(self, tmp_path: Path):
        conn = TestStoreUnits._db(tmp_path)
        try:
            self._store(conn, tmp_path,
                        ".global Foo\nFoo:\n  b .\n.global Bar\nBar:\n  b .\n")
            self._store(conn, tmp_path, ".global Foo\nFoo:\n  b .\n")
            names = {r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash='ch'")}
        finally:
            conn.close()

        assert names == {"Foo"}, f"Bar was deleted from the source: {names}"

    def test_a_changed_verdict_reaches_the_row(self, tmp_path: Path):
        """The case that made the is_project fix look inert."""
        conn = TestStoreUnits._db(tmp_path)
        body = ".global Foo\nFoo:\n  b .\n"
        try:
            self._store(conn, tmp_path, body)
            assert conn.execute(
                "SELECT is_project FROM symbols WHERE name='Foo'").fetchone()[0] == 1
            # The same file, now covered by a vendor pattern.
            self._store(conn, tmp_path, body, vendor_patterns=["startup.S"])
            after = conn.execute(
                "SELECT is_project FROM symbols WHERE name='Foo'").fetchone()[0]
        finally:
            conn.close()

        assert after == 0, "the second run must be able to reclassify the symbol"

    def test_a_vector_edge_is_not_appended_twice(self, tmp_path: Path):
        conn = TestStoreUnits._db(tmp_path)
        body = _table(".global Reset_Handler\nReset_Handler:\n  b .\n"
                      "  .word Reset_Handler\n")
        try:
            self._store(conn, tmp_path, body)
            self._store(conn, tmp_path, body)
            rows = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE ref_kind='vector'").fetchone()[0]
        finally:
            conn.close()

        assert rows == 1, f"refs has no unique constraint; got {rows} rows"

    def test_c_symbols_are_left_alone(self, tmp_path: Path):
        """The clear keys on the USR namespace, never on the file.

        An assembly unit can include a header that C includes too.  Keying
        the cleanup by file id — which is what the C path does — would
        delete the C symbols stored earlier in the same run.
        """
        from fw_context_mcp.indexer.db import insert_symbols_batch, transaction, upsert_file

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                fid = upsert_file(conn, "ch", "shared.h", "c")
                insert_symbols_batch(conn, [(
                    "ch", fid, "shared.h", "c_thing", "c:@F@c_thing", "c_thing",
                    "c_thing", "function", 1, 1, 1, 1,
                    "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
                )])
            self._store(conn, tmp_path, ".global Foo\nFoo:\n  b .\n")
            names = {r[0] for r in conn.execute(
                "SELECT name FROM symbols WHERE config_hash='ch'")}
        finally:
            conn.close()

        assert "c_thing" in names, f"the C symbol was collateral damage: {names}"

    def test_a_run_that_preprocesses_nothing_keeps_the_old_index(self, tmp_path: Path):
        """No clang on PATH must not empty what an earlier run stored.

        The clear happens once, on the first unit that preprocessed, for
        exactly this reason.  The C path gets the property for free: a
        translation unit that fails to parse never reaches its own
        `replace_file_data`.
        """
        import fw_context_mcp.indexer.asm as asm_mod

        conn = TestStoreUnits._db(tmp_path)
        try:
            self._store(conn, tmp_path, ".global Foo\nFoo:\n  b .\n")
            before = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash='ch'").fetchone()[0]

            original = asm_mod.preprocess
            asm_mod.preprocess = lambda unit: None
            try:
                result = self._store(conn, tmp_path, ".global Foo\nFoo:\n  b .\n")
            finally:
                asm_mod.preprocess = original
            after = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE config_hash='ch'").fetchone()[0]
        finally:
            conn.close()

        assert before == 1
        assert result.failed == 1
        assert after == before, "a failed run must not be a delete"


class TestCommentsAndLocalLabels:
    """Prose reads like assembly, and not every label is a symbol.

    Both defects came from a real startup file.  startup_NRF52840.S opens
    with a block comment whose first line is `NOTE: Template files ...`,
    and the label pattern took `NOTE` for a definition.  The same file
    writes its copy loop with `.LC0` and `.LC1`, which GNU as keeps out of
    the object symbol table entirely.
    """

    @staticmethod
    def _symbols(tmp_path: Path, body: str):
        from fw_context_mcp.indexer.asm import extract_symbols, preprocess

        source = preprocess(_unit(tmp_path, "a.S", body))
        assert source is not None
        return {s.name for s in extract_symbols(source)}

    def test_a_word_in_a_comment_is_not_a_label(self, tmp_path: Path):
        names = self._symbols(
            tmp_path,
            "/*\nNOTE: Template files are application specific\n*/\n"
            ".global Real\nReal:\n  b .\n",
        )

        assert names == {"Real"}, names

    def test_a_directive_in_a_comment_does_not_count(self, tmp_path: Path):
        """The comment fix protects every pattern, not just the label one."""
        names = self._symbols(
            tmp_path,
            "/* .global Ghost\nGhost:\n*/\n.global Real\nReal:\n  b .\n",
        )

        assert names == {"Real"}, names

    def test_a_comment_that_opens_and_closes_on_one_line(self, tmp_path: Path):
        names = self._symbols(
            tmp_path, ".global Real  /* Fake: not a label */\nReal:\n  b .\n"
        )

        assert names == {"Real"}, names

    def test_code_after_a_closing_comment_still_counts(self, tmp_path: Path):
        names = self._symbols(
            tmp_path, "/* lead */ .global Real\nReal:\n  b .\n"
        )

        assert names == {"Real"}, names

    def test_a_local_label_is_not_a_symbol(self, tmp_path: Path):
        names = self._symbols(
            tmp_path,
            ".global Real\nReal:\n.LC1:\n  subs r3, 4\n  bgt .LC1\n.LC0:\n  b .\n",
        )

        assert names == {"Real"}, names

    def test_a_vector_slot_in_a_comment_is_not_a_slot(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import extract_vectors, preprocess

        source = preprocess(_unit(
            tmp_path, "a.S",
            _VECTOR_SECTION
            + ".global Real\nReal:\n  b .\n"
            "/*\n  .word Ghost\n*/\n  .word Real\n",
        ))
        assert source is not None
        names = {v.name for v in extract_vectors(source)}

        assert names == {"Real"}, names


class TestAssemblyDefinitionSortsLast:
    """A weak assembly stub must not shadow the C definition that wins.

    A CMSIS startup file defines every interrupt handler weakly so C code
    can override it, and both end up in the index under the same name —
    measured on zbox-ecb-fw, 11 names carry one of each.  Line number used
    to decide, so a project whose handler sits further down its file than
    the stub does in the startup file got the stub: no callers, and a body
    of two directives.
    """

    @staticmethod
    def _db_with_both(tmp_path: Path, c_line: int, asm_line: int):
        from fw_context_mcp.indexer.db import (
            insert_symbols_batch,
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
            c_file = upsert_file(conn, "ch", "src/main.cpp", "cpp")
            asm_file = upsert_file(conn, "ch", "startup.S", "c")
            insert_symbols_batch(conn, [
                ("ch", c_file, "src/main.cpp", "HardFault_Handler",
                 "c:@F@HardFault_Handler", "HardFault_Handler",
                 "HardFault_Handler", "function", c_line, 1, c_line, 1,
                 "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0),
                ("ch", asm_file, "startup.S", "HardFault_Handler",
                 "asm:startup.S@HardFault_Handler", "HardFault_Handler",
                 "HardFault_Handler", "function", asm_line, 1, asm_line, 1,
                 "", "", None, 0, 0, "", 0, "", 0, 0.0, "", 0),
            ])
        return conn

    def test_the_c_definition_wins_even_when_it_comes_later(self, tmp_path: Path):
        """The failing arrangement: C at 300, the weak stub at 181."""
        from fw_context_mcp.mcp.handlers.source import _lookup_definition

        conn = self._db_with_both(tmp_path, c_line=300, asm_line=181)
        try:
            sym = _lookup_definition(
                conn, "ch", "HardFault_Handler", preferred_kinds=None)
        finally:
            conn.close()

        assert sym["usr"] == "c:@F@HardFault_Handler", sym["usr"]

    def test_the_c_definition_wins_when_it_comes_first_too(self, tmp_path: Path):
        """The arrangement that happened to work before, must keep working."""
        from fw_context_mcp.mcp.handlers.source import _lookup_definition

        conn = self._db_with_both(tmp_path, c_line=81, asm_line=181)
        try:
            sym = _lookup_definition(
                conn, "ch", "HardFault_Handler", preferred_kinds=None)
        finally:
            conn.close()

        assert sym["usr"] == "c:@F@HardFault_Handler", sym["usr"]

    def test_an_assembly_only_symbol_still_resolves(self, tmp_path: Path):
        """Reset_Handler has no C definition anywhere; it must still resolve."""
        from fw_context_mcp.indexer.db import (
            insert_symbols_batch,
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )
        from fw_context_mcp.mcp.handlers.source import _lookup_definition

        conn = open_db(tmp_path / "index.db")
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(tmp_path))
                upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
                fid = upsert_file(conn, "ch", "startup.S", "c")
                insert_symbols_batch(conn, [(
                    "ch", fid, "startup.S", "Reset_Handler",
                    "asm:startup.S@Reset_Handler", "Reset_Handler",
                    "Reset_Handler", "function", 130, 1, 130, 1,
                    "", "", None, 0, 0, "", 0, "", 0, 0.0, "", 0,
                )])
            sym = _lookup_definition(
                conn, "ch", "Reset_Handler", preferred_kinds=None)
        finally:
            conn.close()

        assert sym is not None
        assert sym["usr"] == "asm:startup.S@Reset_Handler"


class TestCommentStrippingWidensTheVectorMatch:
    """Removing the comment changes what the slot pattern accepts.

    The pattern requires the statement to end after the identifier, so a
    slot written `.long __StackTop  /* Top of Stack */` did not match while
    the comment was still in the line.  Measured on zbox-ecb-fw: the slot
    count went from 53 to 54 when the comment stripping arrived, with the
    linked count unchanged at 11 — the new slot is the initial stack
    pointer, which no handler serves.
    """

    def test_a_slot_with_a_trailing_comment_is_seen(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import extract_vectors, preprocess

        source = preprocess(_unit(
            tmp_path, "a.S",
            _VECTOR_SECTION
            + ".global Reset_Handler\nReset_Handler:\n  b .\n"
            "  .long   __StackTop   /* Top of Stack */\n"
            "  .long   Reset_Handler\n",
        ))
        assert source is not None
        names = {v.name for v in extract_vectors(source)}

        assert names == {"__StackTop", "Reset_Handler"}, names

    def test_a_linker_symbol_slot_is_linked_to_a_declaration(self, tmp_path: Path):
        """__StackTop is defined by the linker script, not by any unit.

        The slot is real all the same, and so is the name; both are now
        recorded, with the target stored as a declaration rather than a
        definition.
        """
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(
            tmp_path, "a.S",
            _table(
                ".global Reset_Handler\nReset_Handler:\n  b .\n"
                "  .long   __StackTop   /* Top of Stack */\n"
                "  .long   Reset_Handler\n"
            ),
        )
        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                result = store_units(conn, "ch", [unit], tmp_path)
        finally:
            conn.close()

        assert result.vectors == 2, "Reset_Handler and __StackTop"
        assert result.unresolved == 0


class TestAWeakAliasDoesNotHideTheCOverride:
    """`.weak X` + `.thumb_set X, Default_Handler` is how C overrides X.

    The pair says assembly does not service the interrupt.  It says nothing
    about whether C does, and CMSIS writes it precisely so that C can.
    Asking about the alias before asking the index threw every override
    away: measured on FM, 26 handlers that interrupt.cpp, HardwareTimer.cpp,
    uart.c and clock.c really define — SysTick_Handler among them — were
    reported unhandled and got no edge, leaving the whole vector table with
    one edge out of 97 slots.
    """

    _STARTUP = (
        _VECTOR_SECTION +
        "  .word  Reset_Handler\n"
        "  .word  SysTick_Handler\n"
        "  .word  TIM2_IRQHandler\n"
        ".global Reset_Handler\n"
        "Reset_Handler:\n  b .\n"
        "Default_Handler:\n  b .\n"
        "  .weak SysTick_Handler\n"
        "  .thumb_set SysTick_Handler,Default_Handler\n"
        "  .weak TIM2_IRQHandler\n"
        "  .thumb_set TIM2_IRQHandler,Default_Handler\n"
    )

    @staticmethod
    def _run(tmp_path: Path, c_handlers: list[str]):
        """Index *c_handlers* as C definitions, then the startup file."""
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import (
            insert_symbols_batch,
            transaction,
            upsert_file,
        )

        conn = TestStoreUnits._db(tmp_path)
        with transaction(conn):
            fid = upsert_file(conn, "ch", "src/hal.c", "c")
            insert_symbols_batch(conn, [
                ("ch", fid, "src/hal.c", name, f"c:@F@{name}", name, name,
                 "function", 10 + i, 1, 12 + i, 1,
                 "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0)
                for i, name in enumerate(c_handlers)
            ])
        unit = _unit(tmp_path, "startup.S",
                     TestAWeakAliasDoesNotHideTheCOverride._STARTUP)
        with transaction(conn):
            result = store_units(conn, "ch", [unit], tmp_path)
        edges = {
            r[0] for r in conn.execute(
                "SELECT to_usr FROM refs WHERE ref_kind='vector'")
        }
        conn.close()
        return result, edges

    def test_an_overridden_alias_gets_an_edge_to_the_c_definition(self, tmp_path: Path):
        result, edges = self._run(tmp_path, ["SysTick_Handler"])

        assert "c:@F@SysTick_Handler" in edges, edges
        assert result.vectors == 3, "every slot is linked"
        assert result.unresolved == 0

    def test_every_override_is_found_not_just_one(self, tmp_path: Path):
        result, edges = self._run(tmp_path, ["SysTick_Handler", "TIM2_IRQHandler"])

        assert result.vectors == 3
        assert result.unresolved == 0
        assert "c:@F@TIM2_IRQHandler" in edges, edges

    def test_the_edge_says_which_handler_actually_runs(self, tmp_path: Path):
        """The discriminator, now that every slot is linked.

        An edge into C means the interrupt is serviced; an edge back into
        the assembly unit means it reaches the alias and thus the trap
        loop.  This is what replaced the old counter, and it says which
        interrupt rather than how many.
        """
        _, edges = self._run(tmp_path, ["SysTick_Handler"])

        assert "c:@F@SysTick_Handler" in edges, (
            f"overridden in C, thus serviced: {edges}"
        )
        assert any(e.startswith("asm:") and e.endswith("TIM2_IRQHandler")
                   for e in edges), (
            f"nobody overrode it, thus it reaches Default_Handler: {edges}"
        )


class TestBodiesAreSkippedNotRead:
    """A .macro or .rept body is a template, not code that exists.

    These are ASSEMBLER directives; `clang -E` expands C preprocessor
    macros and leaves them alone.  What a body defines depends on whether
    and how often it runs, so READING it as ordinary assembly invents
    symbols — verified against arm-none-eabi-as in
    tests/test_asm_corpus.py, where it produced four names that are in no
    object file.

    The reader expands these bodies now rather than skipping them, which
    is how it gets the same answer as the assembler.  The cases here are
    the ones where an expansion yields NOTHING, and reading would have
    yielded a name: a macro nobody invokes, and `.rept 0`.
    """

    @staticmethod
    def _names(tmp_path: Path, body: str) -> set[str]:
        from fw_context_mcp.indexer.asm import extract_symbols, preprocess

        source = preprocess(_unit(tmp_path, "a.S", body))
        assert source is not None
        return {s.name for s in extract_symbols(source)}

    def test_a_macro_never_invoked_defines_nothing(self, tmp_path: Path):
        names = self._names(
            tmp_path,
            ".global Real\nReal:\n  b .\n"
            ".macro never_called\n.global Ghost\nGhost:\n  b .\n.endm\n",
        )

        assert names == {"Real"}, names

    def test_a_rept_body_is_skipped(self, tmp_path: Path):
        """`.rept 0` assembles its body zero times."""
        names = self._names(
            tmp_path,
            ".global Real\nReal:\n  b .\n"
            ".rept 0\n.global Ghost\nGhost:\n  .word 0\n.endr\n",
        )

        assert names == {"Real"}, names

    def test_an_irp_body_is_expanded_once_per_value(self, tmp_path: Path):
        """`.irp` runs its body once per value, so what it defines exists."""
        names = self._names(
            tmp_path,
            ".global Real\nReal:\n  b .\n"
            ".irp which, a, b\n.global from_\\which\nfrom_\\which:\n"
            "  .word 0\n.endr\n",
        )

        assert names == {"Real", "from_a", "from_b"}, names

    def test_an_irp_with_no_values_runs_once(self, tmp_path: Path):
        """"If no VALUE is listed, the sequence is assembled once, with
        SYMBOL set to the null string.\""""
        names = self._names(
            tmp_path,
            ".irp nothing\n.global ran_once\nran_once:\n  b .\n.endr\n",
        )

        assert names == {"ran_once"}, names

    def test_code_after_a_body_is_read_again(self, tmp_path: Path):
        """The skip must end where the body does."""
        names = self._names(
            tmp_path,
            ".macro m\n.global Ghost\nGhost:\n.endm\n"
            ".global After\nAfter:\n  b .\n",
        )

        assert names == {"After"}, names

    def test_a_nested_body_does_not_end_the_outer_one(self, tmp_path: Path):
        """`.endr` must not close a `.macro`, which is why a stack is used."""
        names = self._names(
            tmp_path,
            ".macro outer\n"
            ".rept 2\n.global GhostInner\nGhostInner:\n.endr\n"
            ".global GhostOuter\nGhostOuter:\n"
            ".endm\n"
            ".global After\nAfter:\n  b .\n",
        )

        assert names == {"After"}, names

    def test_a_vector_slot_inside_a_body_is_skipped_too(self, tmp_path: Path):
        """The rule protects the table as well as the symbol list."""
        from fw_context_mcp.indexer.asm import extract_vectors, preprocess

        source = preprocess(_unit(
            tmp_path, "a.S",
            _VECTOR_SECTION
            + "  .word RealSlot\n"
            ".macro m\n  .word GhostSlot\n.endm\n",
        ))
        assert source is not None
        names = {v.name for v in extract_vectors(source)}

        assert names == {"RealSlot"}, names


class TestConstantsDefinedByEqu:
    """`.equ NAME, 56` defines NAME as surely as `.equ NAME, other_symbol`.

    The assembler records both, the second as an absolute symbol.  A
    startup file writes its structure offsets and magic values that way:
    irq_cm4f.S defines TCB_SP_OFS, FPU_USED and three more, and the STM32
    startup defines BootRAM.  Requiring the value to be an identifier
    lost all six — found by comparing the reader against the object files
    the builds had already produced.
    """

    @staticmethod
    def _symbols(tmp_path: Path, body: str):
        from fw_context_mcp.indexer.asm import extract_symbols, preprocess

        source = preprocess(_unit(tmp_path, "a.S", body))
        assert source is not None
        return {s.name: s for s in extract_symbols(source)}

    def test_a_numeric_constant_is_a_symbol(self, tmp_path: Path):
        syms = self._symbols(tmp_path, "  .equ TCB_SP_OFS, 56\n")

        assert "TCB_SP_OFS" in syms

    def test_a_hex_constant_is_a_symbol(self, tmp_path: Path):
        syms = self._symbols(tmp_path, "  .equ BootRAM, 0xF1E0F85F\n")

        assert "BootRAM" in syms

    def test_an_expression_is_a_symbol(self, tmp_path: Path):
        syms = self._symbols(tmp_path, "  .set SIZE, 8 * 4 + 1\n")

        assert "SIZE" in syms

    def test_a_constant_is_data_not_code(self, tmp_path: Path):
        """A structure offset must not answer "what does this firmware do"."""
        syms = self._symbols(tmp_path, "  .equ TCB_SP_OFS, 56\n")

        assert syms["TCB_SP_OFS"].kind == "variable"
        assert syms["TCB_SP_OFS"].alias_target is None, (
            "a number is not an alias of anything"
        )

    def test_an_alias_of_a_symbol_stays_an_alias(self, tmp_path: Path):
        syms = self._symbols(
            tmp_path,
            "Default_Handler:\n  b .\n"
            "  .thumb_set TIM2_IRQHandler, Default_Handler\n",
        )

        assert syms["TIM2_IRQHandler"].alias_target == "Default_Handler"
        assert syms["TIM2_IRQHandler"].kind == "function"

    def test_a_mode_setting_set_defines_nothing(self, tmp_path: Path):
        """`.set noreorder` takes one operand and is not a definition.

        The comma is what separates the two, which is why the pattern
        requires it.
        """
        syms = self._symbols(tmp_path, "  .set noreorder\n  .set nomacro\n")

        assert syms == {}


class TestWeakLosesToStrong:
    """`.weak` says the linker drops this definition for a strong one.

    A CMSIS startup defines every core exception weakly so that an RTOS
    can define it properly, and both then sit in the index under one
    name.  Without knowing which is weak the resolver saw two equally
    good answers and gave up, so the slot reached no handler at all:
    measured, that cost 3 slots on zbox-ecb-fw and 7 on birdie1-v2-fw-v3,
    HardFault_Handler, SysTick_Handler and the rest of the core
    exceptions among them.
    """

    @staticmethod
    def _db(tmp_path: Path, rows: list[tuple[str, int, int]]):
        """Index *rows* as (usr, is_definition, is_weak) for one name."""
        from fw_context_mcp.indexer.db import (
            insert_symbols_batch,
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )

        conn = open_db(tmp_path / "index.db")
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
            fid = upsert_file(conn, "ch", "src/a.c", "c")
            insert_symbols_batch(conn, [
                ("ch", fid, "src/a.c", "SysTick_Handler", usr, "SysTick_Handler",
                 "SysTick_Handler", "function", 10, 1, 12, definition,
                 "", "", None, 0, 0, "", 0, "", 1, 0.0, "", weak)
                for usr, definition, weak in rows
            ])
        return conn

    def test_a_strong_assembly_definition_wins_over_a_weak_one(self, tmp_path: Path):
        """The zbox case: an RTOS body against a CMSIS stub."""
        from fw_context_mcp.indexer.asm import _resolve_handler

        conn = self._db(tmp_path, [
            ("asm:irq_cm4f.S@SysTick_Handler", 1, 0),
            ("asm:startup.S@SysTick_Handler", 1, 1),
        ])
        try:
            assert _resolve_handler(conn, "ch", "SysTick_Handler") == (
                "asm:irq_cm4f.S@SysTick_Handler"
            )
        finally:
            conn.close()

    def test_two_strong_definitions_are_still_refused(self, tmp_path: Path):
        """A wrong edge is worse than none: on an interrupt it is the only
        edge a reader has."""
        from fw_context_mcp.indexer.asm import _resolve_handler

        conn = self._db(tmp_path, [
            ("asm:one.S@SysTick_Handler", 1, 0),
            ("asm:two.S@SysTick_Handler", 1, 0),
        ])
        try:
            assert _resolve_handler(conn, "ch", "SysTick_Handler") is None
        finally:
            conn.close()

    def test_two_weak_definitions_are_refused_too(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import _resolve_handler

        conn = self._db(tmp_path, [
            ("asm:one.S@SysTick_Handler", 1, 1),
            ("asm:two.S@SysTick_Handler", 1, 1),
        ])
        try:
            assert _resolve_handler(conn, "ch", "SysTick_Handler") is None
        finally:
            conn.close()

    def test_a_single_weak_definition_still_resolves(self, tmp_path: Path):
        """Nothing overrode it, so it IS what the interrupt reaches."""
        from fw_context_mcp.indexer.asm import _resolve_handler

        conn = self._db(tmp_path, [("asm:startup.S@SysTick_Handler", 1, 1)])
        try:
            assert _resolve_handler(conn, "ch", "SysTick_Handler") == (
                "asm:startup.S@SysTick_Handler"
            )
        finally:
            conn.close()

    def test_a_c_definition_still_wins_over_any_assembly_one(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import _resolve_handler

        conn = self._db(tmp_path, [
            ("c:@F@SysTick_Handler", 1, 0),
            ("asm:irq_cm4f.S@SysTick_Handler", 1, 0),
            ("asm:startup.S@SysTick_Handler", 1, 1),
        ])
        try:
            assert _resolve_handler(conn, "ch", "SysTick_Handler") == (
                "c:@F@SysTick_Handler"
            )
        finally:
            conn.close()

    def test_the_weak_flag_reaches_the_stored_symbol(self, tmp_path: Path):
        """The column is only useful if the assembly pass fills it."""
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(
            tmp_path, "startup.S",
            ".global Strong\nStrong:\n  b .\n"
            ".weak Weak\nWeak:\n  b .\n",
        )
        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            rows = dict(conn.execute(
                "SELECT name, is_weak FROM symbols WHERE config_hash='ch'"
            ).fetchall())
        finally:
            conn.close()

        assert rows == {"Strong": 0, "Weak": 1}


class TestSlotPositions:
    """A vector table is an ORDERED array, and the position is the answer.

    Verified against the Cortex-M layout on two real projects: SysTick
    sits at 15 and the first external interrupt at 16, on FM's STM32 and
    on zbox-ecb-fw's nRF52 alike.

    What the position MEANS is architecture knowledge the index does not
    decide.  On Cortex-M slot 16+n is external IRQ n; on arm64 it selects
    an exception class, and that table is written as branch instructions
    this reader does not read at all.
    """

    @staticmethod
    def _slots(tmp_path: Path, body: str):
        from fw_context_mcp.indexer.asm import extract_vectors, preprocess

        source = preprocess(_unit(tmp_path, "t.S", body))
        assert source is not None
        return {v.name: v.index for v in extract_vectors(source)}

    def test_positions_count_from_zero(self, tmp_path: Path):
        slots = self._slots(tmp_path, _table(
            "  .word First\n  .word Second\n  .word Third\n"))

        assert slots == {"First": 0, "Second": 1, "Third": 2}

    def test_a_reserved_slot_takes_its_position(self, tmp_path: Path):
        """`.word 0` names nothing and still occupies a place.

        Skipping it would put SysTick at 10 instead of 15 on every
        Cortex-M table, because five reserved entries come before it.
        """
        slots = self._slots(tmp_path, _table(
            "  .word First\n  .word 0\n  .word 0\n  .word Fourth\n"))

        assert slots == {"First": 0, "Fourth": 3}

    def test_the_cortex_m_layout_comes_out_right(self, tmp_path: Path):
        """The arrangement every CMSIS startup writes, in miniature."""
        slots = self._slots(tmp_path, _table(
            "  .word _estack\n  .word Reset_Handler\n  .word NMI_Handler\n"
            "  .word HardFault_Handler\n  .word MemManage_Handler\n"
            "  .word BusFault_Handler\n  .word UsageFault_Handler\n"
            "  .word 0\n  .word 0\n  .word 0\n  .word 0\n"
            "  .word SVC_Handler\n  .word DebugMon_Handler\n  .word 0\n"
            "  .word PendSV_Handler\n  .word SysTick_Handler\n"
            "  .word WWDG_IRQHandler\n"))

        assert slots["SysTick_Handler"] == 15
        assert slots["WWDG_IRQHandler"] == 16, "the first external interrupt"

    def test_several_entries_on_one_line_each_take_a_position(self, tmp_path: Path):
        slots = self._slots(tmp_path, _table("  .word One, Two, Three\n"))

        assert slots == {"One": 0, "Two": 1, "Three": 2}

    def test_a_constant_outside_the_table_is_not_a_slot(self, tmp_path: Path):
        """The error this rule exists for.

        A real STM32 startup writes `.word _sidata` in the ordinary
        section for its copy loop.  Taking those for slots made the index
        claim the vector table reached `_sidata`, which it does not.
        """
        slots = self._slots(
            tmp_path,
            "  .word _sidata\n  .word _sdata\n" + _table("  .word Real\n"),
        )

        assert slots == {"Real": 0}, slots

    def test_leaving_the_section_ends_the_table(self, tmp_path: Path):
        slots = self._slots(
            tmp_path,
            _table("  .word InTable\n") + "  .text\n  .word AfterTable\n",
        )

        assert slots == {"InTable": 0}, slots


class TestPreprocessRunsOnce:
    """The source file is named once, not twice.

    `clang_args` from compile_commands.json already holds the file, and
    appending it again handed clang the same file twice: it preprocessed
    the whole unit twice and concatenated the results.  Every assembly
    unit cost twice what it should — the preprocessor is 89-95% of the
    pass — and once the vector reader began carrying a section and a slot
    counter, the second copy started at positions the first had used.
    """

    def test_the_file_is_preprocessed_once(self, tmp_path: Path):
        """One `<built-in>` ENTRY per invocation.

        A linemarker's trailing flag says what happened: 1 is entering a
        file, 2 returning to one, 3 a system header.  `<built-in>` is
        entered once per clang run, so counting the flag-1 markers counts
        the runs — the bare name appears several times in a single run.
        """
        source = preprocess(_unit(
            tmp_path, "t.S", "  .globl Foo\nFoo:\n  b .\n",
            args=[str(tmp_path / "t.S")],
        ))

        assert source is not None
        assert source.text.count('"<built-in>" 1') == 1, (
            "the unit was preprocessed more than once"
        )

    def test_the_source_survives_the_filtering(self, tmp_path: Path):
        """Dropping the file from the flags must not drop the file."""
        from fw_context_mcp.indexer.asm import extract_symbols

        source = preprocess(_unit(
            tmp_path, "t.S", "  .globl Foo\nFoo:\n  b .\n",
            args=[str(tmp_path / "t.S")],
        ))

        assert source is not None
        assert {s.name for s in extract_symbols(source)} == {"Foo"}


class TestTheSlotPositionReachesTheIndex:
    """The position has to survive into refs, or it answers nothing.

    Verified against the CMSIS header on FM: TIM2_IRQHandler sits in slot
    44, and stm32l476xx.h defines TIM2_IRQn = 28, which is 44 - 16.  The
    subtraction is Cortex-M knowledge and stays with the reader of the
    index; what is stored is the position.
    """

    def test_the_edge_carries_the_position(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        unit = _unit(tmp_path, "startup.S", _table(
            "  .word Slot0\n  .word 0\n  .word Slot2\n"
            "Slot0:\n  b .\nSlot2:\n  b .\n"))
        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch", [unit], tmp_path)
            rows = dict(conn.execute(
                "SELECT s.name, r.slot_index FROM refs r "
                "JOIN symbols s ON s.usr = r.to_usr "
                "WHERE r.ref_kind='vector'"
            ).fetchall())
        finally:
            conn.close()

        assert rows == {"Slot0": 0, "Slot2": 2}, (
            "the reserved entry between them takes position 1"
        )

    def test_an_ordinary_reference_carries_no_position(self, tmp_path: Path):
        """slot_index belongs to a vector slot and to nothing else."""
        from fw_context_mcp.indexer.db import (
            insert_refs_batch,
            open_db,
            transaction,
            upsert_build_config,
            upsert_project,
        )

        conn = open_db(tmp_path / "index.db")
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(tmp_path))
                upsert_build_config(conn, "ch", "pid", str(tmp_path / "cc.json"))
                insert_refs_batch(conn, [
                    ("ch", "c:@F@callee", "src/a.c", 10, "c:@F@caller",
                     "call", None),
                ])
            value = conn.execute(
                "SELECT slot_index FROM refs WHERE ref_kind='call'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert value is None


class TestAliasEdges:
    """`.thumb_set X, Y` is a reference: X is another name for Y.

    It is also the only thing that separates a serviced interrupt from an
    unserviced one.  A CMSIS startup declares BOTH kinds weak —
    `Reset_Handler` weak with a body, `NMI_Handler` weak with nothing but
    an alias of `Default_Handler` — so weakness reported the reset vector
    as unserviced until this edge existed.
    """

    @staticmethod
    def _store(tmp_path: Path, body: str):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        conn = TestStoreUnits._db(tmp_path)
        with transaction(conn):
            store_units(conn, "ch", [_unit(tmp_path, "startup.S", body)],
                        tmp_path)
        return conn

    def test_an_alias_becomes_an_edge(self, tmp_path: Path):
        conn = self._store(tmp_path, _table(
            "  .word TIM2_IRQHandler\n"
            "Default_Handler:\n  b .\n"
            "  .weak TIM2_IRQHandler\n"
            "  .thumb_set TIM2_IRQHandler, Default_Handler\n"))
        try:
            rows = conn.execute(
                "SELECT s.name FROM refs r JOIN symbols s ON s.usr = r.to_usr "
                "WHERE r.ref_kind='alias'"
            ).fetchall()
        finally:
            conn.close()

        assert [r[0] for r in rows] == ["Default_Handler"]

    def test_a_handler_with_a_body_gets_no_alias_edge(self, tmp_path: Path):
        """`Reset_Handler` is weak AND real.  Weakness cannot say that."""
        conn = self._store(tmp_path, _table(
            "  .word Reset_Handler\n"
            "  .weak Reset_Handler\nReset_Handler:\n  b .\n"))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE ref_kind='alias'"
            ).fetchone()[0]
            weak = conn.execute(
                "SELECT is_weak FROM symbols WHERE name='Reset_Handler'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 0, "a label with a body is not an alias of anything"
        assert weak == 1, "and it is still weak, which is why weakness cannot tell"

    def test_an_alias_to_an_unknown_name_writes_nothing(self, tmp_path: Path):
        """An edge to the wrong symbol would be worse than no edge."""
        conn = self._store(tmp_path, _table(
            "  .word Handler\n"
            "  .thumb_set Handler, NoSuchSymbol\n"))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE ref_kind='alias'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 0

    def test_the_alias_edges_go_on_a_reindex(self, tmp_path: Path):
        """`_clear_previous_pass` owns them like the vector edges."""
        body = _table(
            "  .word TIM2_IRQHandler\n"
            "Default_Handler:\n  b .\n"
            "  .thumb_set TIM2_IRQHandler, Default_Handler\n")
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import transaction

        conn = TestStoreUnits._db(tmp_path)
        try:
            for _ in range(2):
                with transaction(conn):
                    store_units(conn, "ch",
                                [_unit(tmp_path, "startup.S", body)], tmp_path)
            count = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE ref_kind='alias'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 1, f"two runs left {count} edges"


class TestGetVectorTable:
    """The tool that answers "which interrupts does this firmware service"."""

    @staticmethod
    def _table_of(tmp_path: Path, body: str, c_symbols: list[str] = ()):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import (
            get_vector_table,
            insert_symbols_batch,
            transaction,
            upsert_file,
        )

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                if c_symbols:
                    fid = upsert_file(conn, "ch", "src/main.c", "c")
                    insert_symbols_batch(conn, [
                        ("ch", fid, "src/main.c", n, f"c:@F@{n}", n, n,
                         "function", 10, 1, 12, 1, "", "", None, 0, 0, "", 0,
                         "", 1, 0.0, "", 0)
                        for n in c_symbols
                    ])
                store_units(conn, "ch", [_unit(tmp_path, "startup.S", body)],
                            tmp_path)
            return {r["slot"]: r for r in get_vector_table(conn, "ch")}
        finally:
            conn.close()

    _STARTUP = _table(
        "  .word _estack\n"
        "  .word Reset_Handler\n"
        "  .word NMI_Handler\n"
        "  .word SysTick_Handler\n"
        "  .weak Reset_Handler\nReset_Handler:\n  b .\n"
        "Default_Handler:\n  b .\n"
        "  .weak NMI_Handler\n  .thumb_set NMI_Handler, Default_Handler\n"
        "  .weak SysTick_Handler\n  .thumb_set SysTick_Handler, Default_Handler\n"
    )

    def test_a_linker_symbol_is_not_a_handler(self, tmp_path: Path):
        """Slot 0 holds the initial stack pointer."""
        table = self._table_of(tmp_path, self._STARTUP)

        assert table[0]["name"] == "_estack"
        assert table[0]["status"] == "linker"

    def test_a_handler_with_a_body_is_serviced_by_assembly(self, tmp_path: Path):
        table = self._table_of(tmp_path, self._STARTUP)

        assert table[1]["status"] == "assembly"
        assert table[1]["name"] == "Reset_Handler"

    def test_an_unoverridden_alias_is_unhandled(self, tmp_path: Path):
        table = self._table_of(tmp_path, self._STARTUP)

        assert table[2]["status"] == "unhandled"
        assert table[2]["aliases"]["name"] == "Default_Handler", (
            "the row says what the interrupt really reaches"
        )

    def test_a_c_definition_services_the_interrupt(self, tmp_path: Path):
        table = self._table_of(tmp_path, self._STARTUP, ["SysTick_Handler"])

        assert table[3]["status"] == "c"
        assert table[3]["file"] == "src/main.c"

    def test_a_serviced_slot_names_the_stub_it_replaced(self, tmp_path: Path):
        """The CMSIS pattern, which the reader asked to see."""
        table = self._table_of(tmp_path, self._STARTUP, ["SysTick_Handler"])

        assert "overridden" in table[3], table[3]
        assert table[3]["overridden"]["file"] == "startup.S"

    def test_unhandled_only_keeps_just_those(self, tmp_path: Path):
        from fw_context_mcp.indexer.asm import store_units
        from fw_context_mcp.indexer.db import get_vector_table, transaction

        conn = TestStoreUnits._db(tmp_path)
        try:
            with transaction(conn):
                store_units(conn, "ch",
                            [_unit(tmp_path, "startup.S", self._STARTUP)],
                            tmp_path)
            rows = get_vector_table(conn, "ch", unhandled_only=True)
        finally:
            conn.close()

        assert {r["name"] for r in rows} == {"NMI_Handler", "SysTick_Handler"}
