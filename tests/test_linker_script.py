"""The GNU ld linker script reader, rule by rule from the manual.

Each class below cites the GNU ld manual — sections "Simple Assignments",
"PROVIDE", "MEMORY", and "Linker Script Format" — rather than a shape seen
in one SDK.  Platforms differ, the manual does not.

Two rules have no manual behind them, and their tests say where the
evidence comes from instead: `#` as a comment, and the placement suffix
that follows a closing brace.  Both were measured on a build that links.

`scripts` in this file are minimal on purpose.  The check against a real
linked binary lives outside the suite, because it needs a built project.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer.linker_script import (
    Report,
    evaluate_constant,
    find_assignment,
    find_memory_blocks,
    parse,
    parse_assignment,
    parse_regions,
    statements,
    strip_comments,
)


def _parse(tmp_path: Path, text: str):
    """Write *text* as a script and read it back."""
    path = tmp_path / "script.ld"
    path.write_text(text, encoding="utf-8")
    result = parse(path)
    assert result is not None
    return result


def _names(script) -> list[str]:
    return [symbol.name for symbol in script.symbols]


class TestComments:
    """"You may include comments in linker scripts just as in C, delimited
    by '/*' and '*/'." — Linker Script Format."""

    def test_a_block_comment_becomes_spaces(self):
        assert strip_comments("a /* b */ c") == "a         c"

    def test_a_block_comment_keeps_every_newline(self):
        # An offset in the result must name the same line as in the input,
        # because the reader reports a line number from an offset.
        cleaned = strip_comments("a\n/* x\ny */\nb")
        assert cleaned.count("\n") == 3
        assert len(cleaned) == len("a\n/* x\ny */\nb")

    def test_an_unterminated_block_comment_runs_to_the_end(self):
        assert strip_comments("a /* b").strip() == "a"

    def test_a_hash_comment_runs_to_the_end_of_the_line(self):
        # Not in the manual.  Zephyr writes it: nine such lines sit outside
        # any `/* */` in build/.../app/zephyr/linker.cmd, and that link
        # produced zephyr.elf, thus ld accepts the form.
        assert strip_comments("a # b\nc") == "a    \nc"

    def test_a_hash_inside_a_string_is_not_a_comment(self):
        text = 'ASSERT(x, "a # b");\n_after = .;'
        assert "a # b" in strip_comments(text)

    def test_a_block_comment_start_inside_a_string_is_not_a_comment(self):
        assert '"/*"' in strip_comments('_x = 1; MESSAGE("/*")')


class TestStatements:
    """"You may separate commands using semicolons." — Linker Script Format.

    A brace ends an output section description, which carries no semicolon,
    so the scanner treats it as a terminator too.
    """

    def test_a_semicolon_ends_a_statement(self):
        assert [s for s, _ in statements("a = 1; b = 2;")] == ["a = 1", "b = 2"]

    def test_a_brace_ends_a_statement(self):
        produced = [s for s, _ in statements(".text : { *(.text) }")]
        assert produced == [".text :", "*(.text)"]

    def test_a_semicolon_inside_a_string_does_not_end_a_statement(self):
        produced = [s for s, _ in statements('ASSERT(x, "a; b");')]
        assert produced == ['ASSERT(x, "a; b")']

    def test_the_line_is_the_first_line_that_holds_text(self):
        produced = list(statements("\n\n\na = 1;"))
        assert produced == [("a = 1", 4)]

    def test_a_skipped_range_yields_nothing(self):
        text = "a = 1; SKIPPED = 2; b = 3;"
        start = text.index("SKIPPED")
        produced = [s for s, _ in statements(text, [(start, start + 12)])]
        assert "SKIPPED = 2" not in produced


class TestSimpleAssignments:
    """The operator list of "Simple Assignments": `= += -= *= /= <<= >>= &= |=`."""

    def test_every_documented_operator(self):
        for operator in ("=", "+=", "-=", "*=", "/=", "<<=", ">>=", "&=", "|="):
            found = parse_assignment(f"sym {operator} 4")
            assert found is not None, operator
            assert found.name == "sym"
            assert found.expression == "4"

    def test_a_comparison_is_not_an_assignment(self):
        # Measured on a Zephyr script:
        # ASSERT(__nrf_kmu_reserved_push_area == (536870912), "...")
        assert parse_assignment("ASSERT(a == b)") is None

    def test_a_bare_comparison_is_not_an_assignment(self):
        assert parse_assignment("a == b") is None

    def test_the_location_counter_is_not_a_symbol(self):
        # "The special symbol name '.' indicates the location counter."
        assert parse_assignment(". = ALIGN(4)") is None

    def test_a_command_is_not_an_assignment(self):
        for command in (
            'OUTPUT_FORMAT("elf32-littlearm")',
            'ENTRY("__start")',
            "KEEP(*(.vectors))",
            ".text :",
            "> RAM AT > FLASH",
        ):
            assert parse_assignment(command) is None, command

    def test_the_expression_keeps_its_text(self):
        found = parse_assignment("_estack = ORIGIN(RAM) + LENGTH(RAM)")
        assert found is not None
        assert found.expression == "ORIGIN(RAM) + LENGTH(RAM)"

    def test_an_assignment_can_span_lines(self):
        found = parse_assignment("_x =\n    1 + 2")
        assert found is not None
        assert found.name == "_x"


class TestProvide:
    """"PROVIDE(symbol = expression)" — the PROVIDE section.

    The manual says that the linker defines the symbol "only if it is
    referenced but not defined", thus the flag is a fact worth keeping.
    """

    def test_provide(self):
        found = parse_assignment("PROVIDE(_x = 1)")
        assert found is not None
        assert (found.name, found.provide, found.hidden) == ("_x", True, False)

    def test_provide_hidden(self):
        found = parse_assignment("PROVIDE_HIDDEN(_x = 1)")
        assert found is not None
        assert (found.provide, found.hidden) == (True, True)

    def test_hidden_alone(self):
        found = parse_assignment("HIDDEN(_x = 1)")
        assert found is not None
        assert (found.provide, found.hidden) == (False, True)

    def test_a_plain_assignment_is_neither(self):
        found = parse_assignment("_x = 1")
        assert found is not None
        assert (found.provide, found.hidden) == (False, False)


class TestPlacementSuffix:
    """An output section ends with a suffix that carries no semicolon.

    Not a manual rule but a measured loss: with an anchored match, the Mbed project
    lost 18 names, `__StackTop` among them — the one name of that build
    that the index held as `kind="undefined"`.
    """

    def test_an_assignment_after_a_region_suffix(self):
        found = find_assignment("> RAM\n    __StackTop = ORIGIN(RAM)")
        assert found is not None
        assert found.name == "__StackTop"

    def test_an_assignment_after_a_load_region_suffix(self):
        found = find_assignment("> RAM AT > FLASH\n_edata = .")
        assert found is not None
        assert found.name == "_edata"

    def test_the_offset_points_at_the_name(self):
        statement = "> RAM\n__StackTop = 1"
        found = find_assignment(statement)
        assert found is not None
        assert statement[found.offset:].startswith("__StackTop")

    def test_a_statement_with_no_assignment_stays_none(self):
        assert find_assignment("> RAM AT > FLASH") is None

    def test_a_multi_line_assert_does_not_become_a_symbol(self):
        assert find_assignment('ASSERT(a == b,\n    "message")') is None


class TestMemoryBlocks:
    """"MEMORY { name [(attr)] : ORIGIN = origin, LENGTH = len }"."""

    def test_the_block_body_is_found(self):
        text = "MEMORY\n{\n  RAM : ORIGIN = 0, LENGTH = 1\n}\n_x = 1;"
        blocks = find_memory_blocks(text)
        assert len(blocks) == 1
        start, stop = blocks[0]
        assert "RAM" in text[start:stop]
        assert "_x" not in text[start:stop]

    def test_a_nested_brace_does_not_end_the_block(self):
        text = "MEMORY { A : ORIGIN = 0, LENGTH = 1 { } }"
        start, stop = find_memory_blocks(text)[0]
        assert text[stop] == "}"
        assert stop == len(text) - 1

    def test_a_region(self):
        report = Report()
        regions = parse_regions("FLASH (rx) : ORIGIN = 0x10200, LENGTH = 0xefe00",
                                1, report)
        assert len(regions) == 1
        region = regions[0]
        assert region.name == "FLASH"
        assert region.attributes == "rx"
        assert region.origin_value == 0x10200
        assert region.length_value == 0xEFE00

    def test_the_attribute_list_is_optional(self):
        regions = parse_regions("RAM : ORIGIN = 0, LENGTH = 1", 1, Report())
        assert regions[0].attributes == ""

    def test_the_documented_abbreviations(self):
        # "The keyword ORIGIN may be abbreviated to org or o" and
        # "The keyword LENGTH may be abbreviated to len or l".
        for origin_key in ("ORIGIN", "org", "o"):
            for length_key in ("LENGTH", "len", "l"):
                text = f"RAM : {origin_key} = 8, {length_key} = 9"
                regions = parse_regions(text, 1, Report())
                assert len(regions) == 1, text
                assert regions[0].origin_value == 8
                assert regions[0].length_value == 9

    def test_a_comma_inside_the_origin_expression(self):
        # The pattern anchors on the LENGTH keyword, not on the first comma.
        regions = parse_regions("RAM : ORIGIN = MAX(1,2), LENGTH = 4", 1, Report())
        assert len(regions) == 1
        assert regions[0].origin == "MAX(1,2)"
        assert regions[0].length_value == 4

    def test_the_line_is_the_line_of_the_file(self):
        regions = parse_regions("\n\nRAM : ORIGIN = 0, LENGTH = 1", 10, Report())
        assert regions[0].line == 12

    def test_a_line_with_a_colon_that_does_not_parse_is_counted(self):
        report = Report()
        assert parse_regions("RAM : SOMETHING ELSE", 1, report) == []
        assert report.refused["unparsed memory region"] == 1

    def test_a_line_with_no_colon_is_not_counted(self):
        report = Report()
        assert parse_regions("   ", 1, report) == []
        assert not report.refused


class TestEvaluateConstant:
    """Constant arithmetic gets a number, anything else stays text."""

    def test_a_hexadecimal_literal(self):
        assert evaluate_constant("0x10200") == 0x10200

    def test_a_decimal_literal(self):
        assert evaluate_constant("372736") == 372736

    def test_mixed_radix_arithmetic(self):
        # Measured on a Zephyr script: LENGTH = ((673792) - 0xe6).
        assert evaluate_constant("((673792) - 0xe6)") == 673562

    def test_the_documented_suffixes(self):
        assert evaluate_constant("32K") == 32768
        assert evaluate_constant("4M") == 4 * 1024 * 1024
        assert evaluate_constant("1G") == 1024 ** 3

    def test_a_lowercase_suffix(self):
        # The manual writes them uppercase.  Measured on the second Mbed project:
        # `SRAM1 (xrw) : ORIGIN = 0x20000194, LENGTH = 160k - 0x194`, and
        # that script produced the binaries in its build directory, thus ld
        # accepted the lowercase form.  With an uppercase-only pattern the
        # region reported no length at all.
        assert evaluate_constant("160k") == 160 * 1024
        assert evaluate_constant("160k - 0x194") == 160 * 1024 - 0x194
        assert evaluate_constant("4m") == 4 * 1024 * 1024
        assert evaluate_constant("1g") == 1024 ** 3

    def test_a_shift(self):
        assert evaluate_constant("1 << 10") == 1024

    def test_the_bitwise_operators(self):
        assert evaluate_constant("0xF0 | 0x0F") == 0xFF
        assert evaluate_constant("0xFF & 0x0F") == 0x0F
        assert evaluate_constant("0xFF ^ 0x0F") == 0xF0

    def test_division_is_integer_division(self):
        # ld divides integers.  A float result would be wrong for an address.
        assert evaluate_constant("7 / 2") == 3

    def test_division_by_zero_gives_no_value(self):
        assert evaluate_constant("1 / 0") is None

    def test_a_symbol_gives_no_value(self):
        assert evaluate_constant("_estack") is None

    def test_a_call_gives_no_value(self):
        assert evaluate_constant("ORIGIN(RAM) + LENGTH(RAM)") is None

    def test_the_location_counter_gives_no_value(self):
        assert evaluate_constant(".") is None

    def test_a_huge_shift_gives_no_value(self):
        # A shift this size is not an address expression, and evaluating it
        # would build a number that fills the memory.
        assert evaluate_constant("1 << 100000") is None

    def test_nonsense_gives_no_value(self):
        assert evaluate_constant("= = =") is None
        assert evaluate_constant("") is None

    def test_a_string_gives_no_value(self):
        assert evaluate_constant('"text"') is None


class TestWholeScript:
    """The reader over a whole file."""

    SCRIPT = """\
/* A comment on line 1. */
MEMORY
{
    FLASH (rx) : ORIGIN = 0x10200, LENGTH = 0xefe00
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 0x40000
}
ENTRY(Reset_Handler)
SECTIONS
{
    .text :
    {
        KEEP(*(.Vectors))
        *(.text)
    } > FLASH
    _etext = .;
    .data :
    {
        _sdata = .;
        *(.data)
        _edata = .;
    } > RAM AT > FLASH
    __StackTop = ORIGIN(RAM) + LENGTH(RAM);
    PROVIDE(__stack = __StackTop);
}
"""

    def test_the_entry_point(self, tmp_path):
        script = _parse(tmp_path, self.SCRIPT)
        assert script.entry == "Reset_Handler"
        assert script.entry_line == 7

    def test_the_regions(self, tmp_path):
        script = _parse(tmp_path, self.SCRIPT)
        assert [r.name for r in script.regions] == ["FLASH", "RAM"]
        assert script.regions[0].origin_value == 0x10200

    def test_the_region_keywords_are_not_symbols(self, tmp_path):
        # Without the MEMORY block exclusion, `ORIGIN = 0x10200` inside a
        # region would define a symbol named ORIGIN.
        script = _parse(tmp_path, self.SCRIPT)
        assert "ORIGIN" not in _names(script)
        assert "LENGTH" not in _names(script)

    def test_every_symbol(self, tmp_path):
        script = _parse(tmp_path, self.SCRIPT)
        assert _names(script) == [
            "_etext", "_sdata", "_edata", "__StackTop", "__stack",
        ]

    def test_the_symbol_after_a_placement_suffix(self, tmp_path):
        script = _parse(tmp_path, self.SCRIPT)
        stack_top = next(s for s in script.symbols if s.name == "__StackTop")
        assert stack_top.line == 22
        assert stack_top.expression == "ORIGIN(RAM) + LENGTH(RAM)"
        assert stack_top.value is None

    def test_the_provide_flag(self, tmp_path):
        script = _parse(tmp_path, self.SCRIPT)
        stack = next(s for s in script.symbols if s.name == "__stack")
        assert stack.provide is True

    def test_a_repeated_assignment_is_counted(self, tmp_path):
        script = _parse(tmp_path, "_x = 1;\n_x = 2;\n")
        assert _names(script) == ["_x"]
        assert script.symbols[0].line == 1
        assert script.report.refused["repeated assignment"] == 1

    def test_a_quoted_entry_name(self, tmp_path):
        script = _parse(tmp_path, 'ENTRY("__start")\n')
        assert script.entry == "__start"

    def test_a_missing_file_gives_none(self, tmp_path):
        assert parse(tmp_path / "absent.ld") is None

    def test_a_script_with_nothing_in_it(self, tmp_path):
        script = _parse(tmp_path, "/* only a comment */\n")
        assert script.symbols == []
        assert script.regions == []
        assert script.entry is None
        assert script.report.summary() == ""


class TestReport:
    """A refusal must be visible, because a silent skip and an empty file
    look the same in the index."""

    def test_an_empty_report_summarizes_to_nothing(self):
        assert Report().summary() == ""

    def test_a_summary_holds_the_count_and_the_reason(self):
        report = Report()
        report.refused["unparsed memory region"] += 2
        assert report.summary() == "2 unparsed memory region"
