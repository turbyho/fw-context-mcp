"""The GNU as macro expander, rule by rule from the manual.

`tests/test_asm_corpus.py` checks the whole reader against a real
assembler, which is the authority.  These tests pin the individual rules
so that a failure says WHICH rule broke, and cover the refusals, which
an assembler cannot demonstrate: it expands what this deliberately will
not.

Each rule below cites the manual (`as.info`, nodes `Macro`, `Exitm`,
`Purgem`) rather than a shape observed in some SDK.  Platforms differ;
the manual does not.
"""

from __future__ import annotations

from fw_context_mcp.indexer._asm_macro import (
    MAX_DEPTH,
    Report,
    bind,
    expand_stream,
    parse_parameters,
    split_arguments,
    substitute,
)


def _run(text: str) -> tuple[list[str], Report]:
    """Expand *text* as one file, returning the statements and the report."""
    report = Report()
    stream = (("a.S", i + 1, line) for i, line in enumerate(text.splitlines()))
    produced = [stmt for _, _, stmt in expand_stream(stream, report)]
    return produced, report


class TestParseParameters:
    """"specify their names after the macro name, separated by commas or
    spaces" — and a default follows the name with `=`."""

    def test_names_separated_by_commas(self):
        assert [p.name for p in parse_parameters("p, p1")] == ["p", "p1"]

    def test_names_separated_by_blanks(self):
        assert [p.name for p in parse_parameters("p p1")] == ["p", "p1"]

    def test_no_parameters_at_all(self):
        assert parse_parameters("") == []

    def test_a_default_value(self):
        (param,) = parse_parameters("name=default_value")

        assert param.name == "name"
        assert param.default == "default_value"

    def test_an_empty_default_is_not_no_default(self):
        """`sum from=0` and `sum from=` differ: The second defaults to ""."""
        (param,) = parse_parameters("from=")

        assert param.default == ""

    def test_the_req_qualifier(self):
        (param,) = parse_parameters("p:req")

        assert param.name == "p"
        assert param.required is True

    def test_the_vararg_qualifier(self):
        (param,) = parse_parameters("rest:vararg")

        assert param.name == "rest"
        assert param.vararg is True

    def test_the_manuals_own_example(self):
        """`.macro m p1:req, p2=0, p3:vararg`"""
        first, second, third = parse_parameters("p1:req, p2=0, p3:vararg")

        assert (first.name, first.required) == ("p1", True)
        assert (second.name, second.default) == ("p2", "0")
        assert (third.name, third.vararg) == ("p3", True)


class TestSplitArguments:
    """"Multiple arguments can be separated by blanks or commas."

    An argument that contains either is enclosed in `()`, `[]` or `"…"`,
    and the manual is explicit that **only the double quotes are
    stripped**.
    """

    def test_commas(self):
        assert split_arguments("one, two") == ["one", "two"]

    def test_blanks(self):
        assert split_arguments("one two") == ["one", "two"]

    def test_no_arguments(self):
        assert split_arguments("") == []

    def test_a_leading_comma_is_an_empty_argument(self):
        """`reserve_str ,B` is the manual's own way of taking a default."""
        assert split_arguments(", second") == ["", "second"]

    def test_a_trailing_comma_is_an_empty_argument(self):
        assert split_arguments("first,") == ["first", ""]

    def test_double_quotes_are_stripped(self):
        assert split_arguments('"quoted value"') == ["quoted value"]

    def test_two_quotes_stand_for_one(self):
        assert split_arguments('"say ""hi"""') == ['say "hi"']

    def test_a_backslash_escapes_and_is_not_kept(self):
        assert split_arguments(r'"say \"hi\""') == ['say "hi"']

    def test_parentheses_are_not_stripped(self):
        """The manual strips only quotes; the brackets stay in the value."""
        assert split_arguments("(a, b)") == ["(a, b)"]

    def test_brackets_are_not_stripped(self):
        assert split_arguments("[a b]") == ["[a b]"]

    def test_adjacent_literals_are_not_concatenated(self):
        """"such adjacent string literals … will not be concatenated"."""
        assert split_arguments('"one" "two"') == ["one", "two"]


class TestBind:
    """How a call's arguments reach the formal parameters."""

    @staticmethod
    def _macro(params: str):
        from fw_context_mcp.indexer._asm_macro import Macro

        return Macro("m", parse_parameters(params), [])

    def test_by_position(self):
        assert bind(self._macro("a, b"), ["1", "2"]) == {"a": "1", "b": "2"}

    def test_by_keyword_in_any_order(self):
        """`sum 9,17` is equivalent to `sum to=17, from=9`."""
        assert bind(self._macro("from, to"), ["to=17", "from=9"]) == {
            "from": "9", "to": "17"
        }

    def test_a_keyword_may_omit_earlier_values(self):
        """`sum to=6` is equivalent to `sum 0, 6`."""
        assert bind(self._macro("from=0, to=0"), ["to=6"]) == {
            "from": "0", "to": "6"
        }

    def test_an_omitted_argument_takes_its_default(self):
        assert bind(self._macro("p1=zero p2"), ["", "given"]) == {
            "p1": "zero", "p2": "given"
        }

    def test_a_missing_required_value_refuses(self):
        """None means refuse: a wrong binding writes wrong symbol names."""
        assert bind(self._macro("p:req"), []) is None

    def test_a_blank_required_value_refuses(self):
        assert bind(self._macro("p:req"), [""]) is None

    def test_vararg_takes_everything_left(self):
        assert bind(self._macro("first, rest:vararg"), ["a", "1", "2"]) == {
            "first": "a", "rest": "1,2"
        }

    def test_too_many_arguments_refuses(self):
        assert bind(self._macro("only"), ["a", "b"]) is None


class TestSubstitute:
    def test_a_named_argument(self):
        assert substitute(r"  .globl \name", {"name": "Foo"}, 0) == "  .globl Foo"

    def test_the_empty_separator(self):
        r"""`\()` separates the argument from what follows and expands to
        nothing: `\base\().\length`."""
        assert substitute(r"\a\()_\b", {"a": "x", "b": "y"}, 0) == "x_y"

    def test_the_execution_counter(self):
        assert substitute(r"  .word \@", {}, 7) == "  .word 7"

    def test_a_longer_name_wins_over_its_prefix(self):
        r"""The manual's own `plus1 p, p1` trap: `\p` must not match in `\p1`."""
        assert substitute(r"\p1 \p", {"p": "A", "p1": "B"}, 0) == "B A"

    def test_an_unknown_name_is_left_alone(self):
        """Replacing it would be a guess, and a guess here writes a name."""
        assert substitute(r"\unknown", {"known": "x"}, 0) == r"\unknown"


class TestExpandStream:
    def test_a_definition_produces_no_statements_of_its_own(self):
        produced, report = _run(".macro m\n  .globl Ghost\nGhost:\n.endm\n")

        assert produced == []
        assert report.defined == 1
        assert report.expanded == 0

    def test_an_invocation_produces_the_body(self):
        produced, report = _run(
            ".macro IRQ handler\n  .weak \\handler\n.endm\n  IRQ Foo\n"
        )

        assert produced == ["  .weak Foo"]
        assert report.expanded == 1

    def test_a_statement_carries_the_line_of_the_invocation(self):
        """A reader wants the line that says `IRQ Foo`, not one inside the
        definition, which names no handler at all."""
        report = Report()
        lines = ".macro IRQ h\n  .weak \\h\n.endm\n\n  IRQ Foo\n".splitlines()
        stream = (("a.S", i + 1, line) for i, line in enumerate(lines))
        # Blank lines pass through as empty statements; the patterns that
        # read this stream ignore them, and dropping them here would only
        # hide whether the interesting one is placed correctly.
        produced = [row for row in expand_stream(stream, report) if row[2].strip()]

        assert produced == [("a.S", 5, "  .weak Foo")]

    def test_a_macro_may_invoke_a_macro(self):
        produced, _ = _run(
            ".macro inner x\n  .globl \\x\n.endm\n"
            ".macro outer y\n  inner \\y\n.endm\n"
            "  outer Deep\n"
        )

        assert produced == ["  .globl Deep"]

    def test_exitm_stops_the_body(self):
        produced, _ = _run(
            ".macro m\n  .globl Before\n  .exitm\n  .globl After\n.endm\n  m\n"
        )

        assert produced == ["  .globl Before"]

    def test_purgem_allows_a_redefinition(self):
        """"You cannot define two macros with the same MACNAME unless it
        has been subject to the `.purgem` directive"."""
        produced, _ = _run(
            ".macro m\n  .globl First\n.endm\n  m\n"
            "  .purgem m\n"
            ".macro m\n  .globl Second\n.endm\n  m\n"
        )

        assert produced == ["  .globl First", "  .globl Second"]

    def test_a_purged_macro_stops_expanding(self):
        produced, _ = _run(
            ".macro m\n  .globl Body\n.endm\n  .purgem m\n  m\n"
        )

        assert produced == ["  m"], "the name is no longer a macro"

    def test_a_statement_that_is_not_an_invocation_passes_through(self):
        produced, _ = _run("  .globl Plain\nPlain:\n")

        assert produced == ["  .globl Plain", "Plain:"]


class TestRefusals:
    """What the expander will not do, and says so.

    Refusing is the rule that keeps a narrow sample of test projects
    honest: an unsupported construct degrades to a missing symbol, never
    to an invented one.
    """

    def test_a_conditional_body_is_refused(self):
        """Which branch it takes is an expression, and this does not
        evaluate expressions."""
        produced, report = _run(
            ".macro m which\n  .if \\which\n  .globl A\n  .else\n"
            "  .globl B\n  .endif\n.endm\n  m 1\n"
        )

        assert produced == []
        assert report.refused["conditional body"] == 1

    def test_the_manuals_recursive_example_is_refused(self):
        """`sum` recurses and is terminated only by `.if \\to-\\from`."""
        produced, report = _run(
            ".macro sum from=0, to=5\n  .long \\from\n"
            "  .if \\to-\\from\n  sum \"(\\from+1)\",\\to\n  .endif\n.endm\n"
            "  sum 0,5\n"
        )

        assert produced == []
        assert report.refused["conditional body"] == 1

    def test_a_recursion_without_a_conditional_hits_the_depth_limit(self):
        """The guard for a recursion the body check cannot see."""
        produced, report = _run(
            ".macro loop\n  .globl Deeper\n  loop\n.endm\n  loop\n"
        )

        assert report.refused["recursion deeper than the limit"] == 1
        assert len(produced) == MAX_DEPTH

    def test_a_call_that_does_not_fit_the_parameters_is_refused(self):
        produced, report = _run(
            ".macro m p:req\n  .globl \\p\n.endm\n  m\n"
        )

        assert produced == []
        assert report.refused["arguments do not match the parameters"] == 1

    def test_a_macro_with_no_endm_is_refused(self):
        """The assembler rejects the file, so this cannot have been built."""
        produced, report = _run(".macro m\n  .globl Ghost\nGhost:\n")

        assert produced == []
        assert report.refused["unterminated macro"] == 1

    def test_the_report_reads_as_one_line(self):
        _, report = _run(
            ".macro ok\n  .globl A\n.endm\n  ok\n"
            ".macro bad x\n  .if \\x\n  .endif\n.endm\n  bad 1\n"
        )

        assert report.summary() == (
            "2 macro(s) defined, 1 expanded, 1 refused (conditional body)"
        )


class TestVarargSlicing:
    """`:vararg` takes what is LEFT, found by position.

    Searching the argument list for the value instead would take the
    slice from the first occurrence, and a call whose arguments repeat is
    ordinary — a vector table full of `Default_Handler` is exactly that.
    """

    @staticmethod
    def _macro(params: str):
        from fw_context_mcp.indexer._asm_macro import Macro

        return Macro("m", parse_parameters(params), [])

    def test_a_repeated_value_does_not_move_the_slice(self):
        bound = bind(self._macro("first, rest:vararg"), ["a", "a", "b"])

        assert bound == {"first": "a", "rest": "a,b"}

    def test_the_value_repeating_the_head_much_later(self):
        bound = bind(self._macro("head, rest:vararg"), ["x", "1", "x", "2"])

        assert bound == {"head": "x", "rest": "1,x,2"}

    def test_vararg_may_take_nothing(self):
        bound = bind(self._macro("first, rest:vararg"), ["only"])

        assert bound == {"first": "only", "rest": ""}


class TestAlternateMacroMode:
    """`.altmacro` changes the rules, so an invocation in it is refused.

    The mode adds `'…'` and `<…>` string delimiters, `!` as a character
    escape, `%expr`, `&` as a separator, and `LOCAL`; keyword arguments
    stop working.  Applying the ordinary rules there does not merely miss
    symbols, it INVENTS them: measured against arm-none-eabi-as, a
    `LOCAL scratch` body made the expander report `scratch`, a name the
    assembler renames on every expansion so it is in no object file,
    while the real symbol behind `<bracket_delimited>` went missing.
    """

    _ALT = (
        ".macro named who\n  .globl \\who\n\\who:\n.endm\n"
        "  named before_the_switch\n"
        "  .altmacro\n"
        "  named <in_alternate_mode>\n"
        "  .noaltmacro\n"
        "  named after_the_switch\n"
    )

    def test_an_invocation_in_alternate_mode_is_refused(self):
        _, report = _run(self._ALT)

        assert report.refused["alternate macro mode"] == 1

    def test_the_refusal_starts_only_at_the_directive(self):
        produced, _ = _run(self._ALT)

        assert "  .globl before_the_switch" in produced

    def test_noaltmacro_turns_expansion_back_on(self):
        produced, _ = _run(self._ALT)

        assert "  .globl after_the_switch" in produced

    def test_nothing_from_alternate_mode_reaches_the_output(self):
        produced, _ = _run(self._ALT)

        assert not any("alternate_mode" in stmt for stmt in produced), produced

    def test_a_local_name_is_never_reported(self):
        """LOCAL is the invention that made this refusal necessary."""
        produced, report = _run(
            "  .altmacro\n"
            ".macro with_local\n  LOCAL scratch\nscratch:\n.endm\n"
            "  with_local\n"
        )

        assert produced == []
        assert report.refused["alternate macro mode"] == 1


class TestRepetition:
    """`.rept`, `.irp` and `.irpc`, all closed by `.endr`.

    All three are BOUNDED, which is what separates them from the
    conditionals: `.rept 3` repeats three times whatever any symbol
    holds, so expanding them needs no expression evaluator.
    """

    def test_rept_repeats_the_body(self):
        produced, report = _run("  .rept 3\n  .word 0\n  .endr\n")

        assert produced == ["  .word 0"] * 3
        assert report.repeated == 1

    def test_a_count_of_zero_generates_nothing(self):
        """"A count of zero is allowed, but nothing is generated." """
        produced, _ = _run("  .rept 0\n  .globl Ghost\nGhost:\n  .endr\n")

        assert produced == []

    def test_a_negative_count_generates_nothing(self):
        """"Negative counts are not allowed"; nothing is the safe reading."""
        produced, _ = _run("  .rept -2\n  .word 0\n  .endr\n")

        assert produced == []

    def test_a_count_that_is_not_a_literal_is_refused(self):
        """An expression needs an evaluator, which this does not have."""
        produced, report = _run("  .rept SIZE*2\n  .word 0\n  .endr\n")

        assert produced == []
        assert report.refused[".rept this cannot evaluate"] == 1

    def test_irp_binds_the_symbol_to_each_value(self):
        produced, _ = _run("  .irp n, 1, 2, 3\n  .word \\n\n  .endr\n")

        assert produced == ["  .word 1", "  .word 2", "  .word 3"]

    def test_irp_with_no_values_runs_once_with_the_null_string(self):
        produced, _ = _run("  .irp nothing\n  .globl ran\n  .endr\n")

        assert produced == ["  .globl ran"]

    def test_irpc_binds_one_character_at_a_time(self):
        produced, _ = _run("  .irpc c, 123\n  .word \\c\n  .endr\n")

        assert produced == ["  .word 1", "  .word 2", "  .word 3"]

    def test_a_repetition_may_nest(self):
        produced, _ = _run(
            "  .irp outer, a, b\n  .irp inner, 1, 2\n"
            "  .globl \\outer\\()_\\inner\n  .endr\n  .endr\n"
        )

        assert produced == [
            "  .globl a_1", "  .globl a_2", "  .globl b_1", "  .globl b_2",
        ]

    def test_a_macro_may_be_invoked_inside_a_repetition(self):
        """The reason one routine handles both: they compose."""
        produced, _ = _run(
            ".macro emit name\n  .globl \\name\n.endm\n"
            "  .irp which, x, y\n  emit \\which\n  .endr\n"
        )

        assert produced == ["  .globl x", "  .globl y"]

    def test_a_repetition_may_sit_inside_a_macro_body(self):
        produced, _ = _run(
            ".macro three\n  .rept 3\n  .word 0\n  .endr\n.endm\n  three\n"
        )

        assert produced == ["  .word 0"] * 3

    def test_a_statement_keeps_its_own_line(self):
        """Unlike a macro body, a repetition body sits in the file, so the
        more precise location is available and is used."""
        report = Report()
        lines = "  .irp n, 1, 2\n  .word \\n\n  .endr\n".splitlines()
        stream = (("a.S", i + 1, line) for i, line in enumerate(lines))
        produced = [row for row in expand_stream(stream, report) if row[2].strip()]

        assert produced == [("a.S", 2, "  .word 1"), ("a.S", 2, "  .word 2")]

    def test_a_repetition_with_no_endr_is_refused(self):
        produced, report = _run("  .rept 2\n  .globl Ghost\n")

        assert produced == []
        assert report.refused["unterminated repetition"] == 1
