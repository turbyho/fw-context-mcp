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
    BARRIER,
    is_barrier,
    MAX_DEPTH,
    Report,
    bind,
    expand_stream,
    parse_parameters,
    split_arguments,
    substitute,
)


def _plain(statement: str) -> str:
    """A barrier as BARRIER: these tests ask where one is, not its effects."""
    return BARRIER if is_barrier(statement) else statement


def _run(text: str) -> tuple[list[str], Report]:
    """Expand *text* as one file, returning the statements and the report."""
    report = Report()
    stream = (("a.S", i + 1, line) for i, line in enumerate(text.splitlines()))
    produced = [_plain(stmt) for _, _, stmt in expand_stream(stream, report)]
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
    to an invented one.  The refusal leaves one BARRIER at its place: the
    skipped statements can hold a label or a section change, and a reader
    must not carry its state across them.
    """

    def test_a_conditional_body_is_refused(self):
        """Which branch it takes is an expression, and this does not
        evaluate expressions."""
        produced, report = _run(
            ".macro m which\n  .if \\which\n  .globl A\n  .else\n"
            "  .globl B\n  .endif\n.endm\n  m 1\n"
        )

        assert produced == [BARRIER]
        assert report.refused["conditional body"] == 1

    def test_the_manuals_recursive_example_is_refused(self):
        """`sum` recurses and is terminated only by `.if \\to-\\from`."""
        produced, report = _run(
            ".macro sum from=0, to=5\n  .long \\from\n"
            "  .if \\to-\\from\n  sum \"(\\from+1)\",\\to\n  .endif\n.endm\n"
            "  sum 0,5\n"
        )

        assert produced == [BARRIER]
        assert report.refused["conditional body"] == 1

    def test_a_recursion_without_a_conditional_hits_the_depth_limit(self):
        """The guard for a recursion the body check cannot see."""
        produced, report = _run(
            ".macro loop\n  .globl Deeper\n  loop\n.endm\n  loop\n"
        )

        assert report.refused["recursion deeper than the limit"] == 1
        assert produced == ["  .globl Deeper"] * MAX_DEPTH + [BARRIER]

    def test_a_call_that_does_not_fit_the_parameters_is_refused(self):
        produced, report = _run(
            ".macro m p:req\n  .globl \\p\n.endm\n  m\n"
        )

        assert produced == [BARRIER]
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

        assert produced == [BARRIER]
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

    def test_a_count_with_no_known_value_is_refused(self):
        """SIZE is defined nowhere, thus the count has no value."""
        produced, report = _run("  .rept SIZE*2\n  .word 0\n  .endr\n")

        assert produced == [BARRIER]
        assert report.refused[".rept this cannot evaluate"] == 1

    def test_a_count_from_a_constant_is_evaluated(self):
        produced, _ = _run("  .equ SIZE, 1\n  .rept SIZE*2\n  .word 0\n  .endr\n")

        assert produced == ["  .equ SIZE, 1", "  .word 0", "  .word 0"]

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


class TestConditionals:
    """A conditional outside a macro body: one branch, the assembler's.

    The values of the expressions are the subject of `test_asm_expr.py`;
    these tests pin which statements reach the reader.
    """

    def test_only_the_taken_branch_is_read(self):
        produced, report = _run(
            "  .if 1\n  .globl A\n  .else\n  .globl B\n  .endif\n"
        )

        assert produced == ["  .globl A"]
        assert report.conditionals == 1

    def test_the_else_branch_when_the_condition_is_false(self):
        produced, _ = _run(
            "  .if 0\n  .globl A\n  .else\n  .globl B\n  .endif\n"
        )

        assert produced == ["  .globl B"]

    def test_elseif_takes_the_first_true_branch_only(self):
        produced, _ = _run(
            "  .if 0\n  .globl A\n  .elseif 1\n  .globl B\n"
            "  .elseif 1\n  .globl C\n  .else\n  .globl D\n  .endif\n"
        )

        assert produced == ["  .globl B"]

    def test_a_conditional_in_a_skipped_branch_is_only_matched(self):
        produced, _ = _run(
            "  .if 0\n  .if 1\n  .globl Nested\n  .endif\n  .else\n"
            "  .globl Outer\n  .endif\n"
        )

        assert produced == ["  .globl Outer"]

    def test_a_value_from_set_is_used(self):
        produced, _ = _run(
            "  .equ LEVEL, 2\n  .if LEVEL == 2\n  .globl Two\n  .endif\n"
        )

        assert produced == ["  .equ LEVEL, 2", "  .globl Two"]

    def test_a_later_set_changes_the_value(self):
        produced, _ = _run(
            "  .set N, 1\n  .set N, 5\n  .if N == 5\n  .globl Five\n  .endif\n"
        )

        assert "  .globl Five" in produced

    def test_ifdef_sees_only_what_comes_before(self):
        """Measured: `.ifdef` of a label defined later in the file is false."""
        produced, _ = _run(
            "early:\n  .ifdef early\n  .globl SeesEarly\n  .endif\n"
            "  .ifdef later\n  .globl SeesLater\n  .endif\nlater:\n"
        )

        assert "  .globl SeesEarly" in produced
        assert "  .globl SeesLater" not in produced

    def test_a_global_alone_does_not_define(self):
        """Measured: `.globl x` then `.ifdef x` is false."""
        produced, _ = _run(
            "  .globl only_global\n  .ifdef only_global\n  .globl Wrong\n  .endif\n"
        )

        assert "  .globl Wrong" not in produced

    def test_a_label_and_a_conditional_in_one_statement(self):
        """`KEXC_areg: .if 4` is a label, then a conditional."""
        produced, _ = _run("lbl: .if 4\n  .space 16\n  .endif\n")

        assert produced == ["lbl:", "  .space 16"]

    def test_a_label_and_an_instruction_in_one_statement(self):
        produced, _ = _run("here: addi sp, sp, -32\n")

        assert produced == ["here:", " addi sp, sp, -32"]

    def test_ifc_is_case_sensitive_and_ignores_blanks_after_the_comma(self):
        produced, _ = _run(
            "  .ifc abc, abc\n  .globl Same\n  .endif\n"
            "  .ifc ABC,abc\n  .globl Case\n  .endif\n"
        )

        assert produced == ["  .globl Same"]

    def test_a_macro_defined_in_a_skipped_branch_does_not_exist(self):
        produced, _ = _run(
            "  .if 0\n.macro pick\n  .globl Wrong\n.endm\n  .else\n"
            ".macro pick\n  .globl Right\n.endm\n  .endif\n  pick\n"
        )

        assert produced == ["  .globl Right"]

    def test_no_exact_value_skips_every_branch_and_leaves_a_barrier(self):
        """A label has an address, which this never computes."""
        produced, report = _run(
            "a:\nb:\n  .if b - a\n  .globl A\n  .else\n  .globl B\n  .endif\n"
            "  .globl After\n"
        )

        assert produced == ["a:", "b:", BARRIER, "  .globl After"]
        assert report.refused["conditional this cannot evaluate"] == 1

    def test_an_elseif_with_no_exact_value_leaves_a_barrier(self):
        produced, _ = _run(
            "  .if 0\n  .globl A\n  .elseif unknown\n  .globl B\n"
            "  .else\n  .globl C\n  .endif\n"
        )

        assert produced == [BARRIER]

    def test_after_a_barrier_ifdef_of_an_unknown_name_is_unknown(self):
        """The skipped branch could have defined it."""
        produced, _ = _run(
            "  .if unknown\nmaybe:\n  .endif\n"
            "  .ifdef maybe\n  .globl Wrong\n  .endif\n"
        )

        assert produced == [BARRIER, BARRIER]

    def test_after_a_barrier_a_symbol_value_is_unknown(self):
        """The skipped branch could have changed it with `.set`."""
        produced, _ = _run(
            "  .set N, 1\n  .if unknown\n  .set N, 2\n  .endif\n"
            "  .if N == 1\n  .globl Wrong\n  .endif\n"
        )

        assert produced == ["  .set N, 1", BARRIER, BARRIER]

    def test_a_stray_endif_is_counted(self):
        produced, report = _run("  .endif\n  .globl A\n")

        assert produced == ["  .globl A"]
        assert report.refused["conditional with no .if"] == 1

    def test_an_if_with_no_endif_is_counted(self):
        _, report = _run("  .if 1\n  .globl A\n")

        assert report.refused["unterminated conditional"] == 1

    def test_the_report_counts_the_conditionals(self):
        _, report = _run("  .if 1\n  .endif\n  .if 0\n  .elseif 1\n  .endif\n")

        assert report.summary() == "3 conditional(s) evaluated"


class TestConditionalTraps:
    """Shapes the assembler reads differently from a naive reader.

    Each one was assembled with `arm-none-eabi-gcc` 12.3 and read with `nm`.
    """

    def test_a_comment_after_else_still_ends_the_branch(self):
        """The assembler takes nothing after `.else` but a comment."""
        produced, _ = _run("  .if 1\nlive:\n  .else @ other\ndead:\n  .endif @ end\n")

        assert produced == ["live:"]

    def test_a_comment_in_the_argument_of_ifb_is_unknown(self):
        """`@` starts a comment on ARM only; the target is not known here."""
        produced, _ = _run("  .ifb @ note\nmaybe:\n  .endif\n")

        assert produced == [BARRIER]

    def test_the_location_counter_has_no_value(self):
        """Measured: after `. = 0x10; nop`, `.if . == 0x10` is not taken."""
        produced, _ = _run("  . = 0x10\n  nop\n  .if . == 0x10\nwrong:\n  .endif\n")

        assert produced == ["  . = 0x10", "  nop", BARRIER]

    def test_a_label_before_endif_in_a_skipped_branch_ends_nothing(self):
        """Measured: in a skipped branch `lbl: .endif` is not an `.endif`."""
        produced, _ = _run(
            "  .if 0\nskipped_lbl: .endif\ndead_in_gas:\n  .endif\nafter_all:\n"
        )

        assert produced == ["after_all:"]

    def test_a_conditional_may_close_after_the_endr_of_its_repetition(self):
        """Measured: an `.if 0` in an `.irp` body skips up to its `.endif`."""
        produced, report = _run(
            "  .irp x, 1\n  .if 0\n  .endr\nin_rept_skip:\n  .endif\nafter:\n"
        )

        assert produced == ["after:"]
        assert not report.refused

    def test_include_stops_the_unit(self):
        """Measured: a conditional the included file opens stays open after
        it, thus nothing after it is known."""
        produced, report = _run('  .include "defs.inc"\nmaybe:\n  .endif\nlater:\n')

        assert produced == [BARRIER]
        assert report.refused[".include this does not read"] == 1

    def test_a_refused_body_that_opens_a_conditional_stops_the_unit(self):
        """`.if 0` in a refused `.rept` can skip the statements after `.endr`."""
        produced, report = _run(
            "  .rept UNKNOWN\n  .if 0\n  .endr\nwrong:\n  .endif\nright:\n"
        )

        assert produced == [BARRIER]
        assert report.refused["rest of the unit after it"] == 1

    def test_a_refused_body_with_balanced_conditionals_does_not_stop(self):
        produced, _ = _run(
            ".macro m x\n  .if \\x\n  .endif\n.endm\n  m 1\nafter:\n"
        )

        assert produced == [BARRIER, "after:"]

    def test_a_refused_body_that_invokes_an_unbalanced_macro_stops(self):
        produced, _ = _run(
            ".macro opens\n  .if 0\n.endm\n  .rept UNKNOWN\n  opens\n  .endr\nwrong:\n"
        )

        assert produced == [BARRIER]

    def test_a_refused_body_with_else_stops_the_unit(self):
        """Its `.else` changes the branch of the `.if` before it."""
        produced, _ = _run(
            "  .if 1\n  .rept UNKNOWN\n  .else\n  .endr\nbad:\n  .endif\n"
        )

        assert produced == [BARRIER]

    def test_a_refused_body_with_include_stops_the_unit(self):
        produced, _ = _run('  .rept UNKNOWN\n  .include "x.s"\n  .endr\nbad:\n')

        assert produced == [BARRIER]

    def test_a_refused_body_that_closes_and_opens_stops_the_unit(self):
        """The depth is the same at the end, and the conditional is another."""
        produced, _ = _run(
            "  .if 1\n  .rept UNKNOWN\n  .endif\n  .if 0\n  .endr\nbad:\n  .endif\n"
        )

        assert produced == [BARRIER]

    def test_a_hash_in_an_argument_is_kept_when_the_comments_are_removed(self):
        """`.ifc #0,#0` on ARM: `#` is an immediate there."""
        report = Report()
        lines = "  .ifc #0,#0\nsame:\n  .endif\n"
        stream = (("a.S", i + 1, line) for i, line in enumerate(lines.splitlines()))
        produced = [_plain(s) for _, _, s in expand_stream(stream, report, comments_removed=True)]

        assert produced == ["same:"]

    def test_defsym_makes_a_negative_ifdef_unknown(self):
        report = Report()
        lines = "known:\n  .ifdef known\nyes:\n  .endif\n  .ifndef OTHER\nmaybe:\n  .endif\n"
        stream = (("a.S", i + 1, line) for i, line in enumerate(lines.splitlines()))
        produced = [_plain(s) for _, _, s in expand_stream(stream, report, defsym=True)]

        assert produced == ["known:", "yes:", BARRIER]


class TestBarrierEffects:
    """What a barrier says about the text behind it.

    A reader keeps its state across a barrier whose text cannot touch that
    state: the owner of the code, the position in a table, a symbol value.
    """

    @staticmethod
    def _effects(text: str):
        from fw_context_mcp.indexer._asm_macro import barrier_effects

        report = Report()
        stream = (("a.S", i + 1, line) for i, line in enumerate(text.splitlines()))
        found = [barrier_effects(s) for _, _, s in expand_stream(stream, report)]
        return [effects for effects in found if effects is not None]

    def test_a_refused_body_of_instructions_only_writes_bytes(self):
        """The arguments bind, thus the text of the body is known."""
        [effects] = self._effects(
            ".macro m x\n  .if \\x\n  bl foo\n  .endif\n.endm\n  m 1\n"
        )

        assert effects.emits and not effects.section
        assert effects.names == frozenset() and effects.exports == frozenset()

    def test_a_label_and_an_export_are_named(self):
        [effects] = self._effects(
            ".macro m\n  .if 1\n  .globl lbl\nlbl:\n  .endif\n.endm\n  m\n"
        )

        assert effects.names == frozenset({"lbl"})
        assert effects.exports == frozenset({"lbl"})

    def test_a_section_switch_is_seen(self):
        [effects] = self._effects("  .rept UNKNOWN\n  .section .vec\n  .endr\n")

        assert effects.section

    def test_a_label_with_the_expansion_count_has_no_known_name(self):
        """`\\@` is the count of the expansion, which a refusal never makes."""
        [effects] = self._effects(
            ".macro m\n  .if 1\nl\\@:\n  .endif\n.endm\n  m\n"
        )

        assert effects.names is None

    def test_a_conditional_with_no_value_gives_the_effects_of_all_branches(self):
        [effects] = self._effects(
            "  .if unknown\nL:\n  .set N, 2\n  .else\n  .text\n  .endif\n"
        )

        assert effects.section
        assert effects.names == frozenset({"L", "N"})

    def test_only_the_names_of_the_text_lose_their_values(self):
        """B keeps its value: the skipped text does not name it."""
        produced, _ = _run(
            "  .set A, 1\n  .set B, 2\n  .if unknown\n  .set A, 3\n  .endif\n"
            "  .if B == 2\nok:\n  .endif\n  .if A == 1\nunsure:\n  .endif\n"
        )

        assert produced == ["  .set A, 1", "  .set B, 2", BARRIER, "ok:", BARRIER]

    def test_include_has_full_effects(self):
        from fw_context_mcp.indexer._asm_macro import FULL

        assert self._effects('  .include "x.s"\n') == [FULL]


class TestBarrierEffectsTraps:
    """Text whose effects a first reading got wrong (measured with gas)."""

    def test_a_name_made_with_the_expansion_count_is_unknown(self):
        """gas defines `foo0`: `.ifdef foo0` must not take a branch."""
        produced, _ = _run(
            ".macro m name\n  .if 1\n  nop\n  .endif\n  .set \\name\\@, 1\n.endm\n"
            "  m foo\n  .ifdef foo0\nseen_def:\n  .endif\n"
            "  .ifndef foo0\nseen_ndef:\n  .endif\n"
        )

        assert "seen_ndef:" not in produced and "seen_def:" not in produced

    def test_a_label_and_a_statement_with_the_count_is_unknown(self):
        [effects] = TestBarrierEffects._effects(
            ".macro m\n  .if 1\n  .endif\nlab\\@: nop\n.endm\n  m\n"
        )

        assert effects.names is None

    def test_a_common_symbol_is_defined(self):
        """gas: after `.comm cbuf, 4`, `.ifdef cbuf` is true."""
        [effects] = TestBarrierEffects._effects(
            ".macro m\n  .if 1\n  .endif\n  .comm cbuf, 4\n.endm\n  m\n"
        )

        assert effects.names is not None and "cbuf" in effects.names

    def test_every_name_of_a_global_list_is_exported(self):
        [effects] = TestBarrierEffects._effects(
            ".macro m\n  .if 1\n  .endif\n  .global x, y\n.endm\n  m\n"
        )

        assert effects.exports == frozenset({"x", "y"})

    def test_moving_the_location_counter_writes_bytes(self):
        [effects] = TestBarrierEffects._effects(
            ".macro m\n  .if 1\n  .endif\n  . = . + 4\n.endm\n  m\n"
        )

        assert effects.emits

    def test_an_include_in_a_conditional_with_no_value_stops_the_unit(self):
        """The included file can leave a conditional open (measured)."""
        produced, _ = _run('  .if unknown\n  .include "x.s"\n  .endif\nafter:\n')

        assert "after:" not in produced
