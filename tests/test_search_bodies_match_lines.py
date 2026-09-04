"""search_bodies must say where in the definition the match is.

FTS5 ``snippet()`` gives the matching text and drops its position, and the
symbol row gives only the first line of the definition.  For a large
function the two are hundreds of lines apart, thus a caller that had to
cite the statement left fw-context for a text search over raw source.

``_match_lines`` closes that gap.  These tests pin the two helpers that
compute it, and the budget that keeps the body of a type out of the output.
"""

from __future__ import annotations

from fw_context_mcp.mcp.handlers.search import (
    _CALLABLE_KINDS,
    _MATCH_LINES_CAP,
    _SOURCE_CAP_CALLABLE,
    _SOURCE_CAP_TYPE,
    _body_match_lines,
    _body_query_terms,
)


class TestQueryTerms:
    """The terms must come from the operator's query, not from FTS5 syntax."""

    def test_plain_words_pass_through(self) -> None:
        assert _body_query_terms("self test") == ["self", "test"]

    def test_case_is_folded(self) -> None:
        assert _body_query_terms("SELF_TEST") == ["self_test"]

    def test_trailing_wildcard_is_dropped(self) -> None:
        # `attach*` is FTS5 syntax; the star never appears in source text.
        assert _body_query_terms("attach*") == ["attach"]

    def test_quoted_phrase_becomes_its_words(self) -> None:
        assert _body_query_terms('"interrupt handler"') == ["interrupt", "handler"]

    def test_operators_and_single_characters_are_dropped(self) -> None:
        # An operator carries no text, and one character matches almost
        # every line while telling the reader nothing.
        assert _body_query_terms("uart AND x OR tx") == ["uart", "tx"]

    def test_empty_query_gives_no_terms(self) -> None:
        assert _body_query_terms("   ") == []


class TestMatchLines:
    """The line numbers must be absolute, and must point at the match."""

    def test_offset_is_added_to_the_start_line(self) -> None:
        source = (
            "void handler(void)\n"   # 1636
            "{\n"                    # 1637
            "    prepare();\n"       # 1638
            "    case SELF_TEST:\n"  # 1639
            "}\n"                    # 1640
        )
        assert _body_match_lines(source, 1636, ["self_test"]) == [1639]

    def test_underscore_query_matches_a_longer_identifier(self) -> None:
        # FTS5 splits on the underscore, thus a query for `self test` must
        # reach `_is_self_test`.  The substring test does that.
        source = "struct Flags\n{\n    uint32_t _is_self_test:1;\n};\n"
        assert _body_match_lines(source, 369, ["self", "test"]) == [371]

    def test_every_matching_line_is_reported(self) -> None:
        source = "a attach\nb\nc attach\n"
        assert _body_match_lines(source, 10, ["attach"]) == [10, 12]

    def test_the_list_is_capped(self) -> None:
        source = "".join("attach\n" for _ in range(_MATCH_LINES_CAP + 25))
        assert len(_body_match_lines(source, 1, ["attach"])) == _MATCH_LINES_CAP

    def test_no_term_gives_no_lines(self) -> None:
        assert _body_match_lines("int x;\n", 1, []) == []

    def test_no_source_gives_no_lines(self) -> None:
        assert _body_match_lines("", 1, ["x"]) == []

    def test_a_match_after_the_truncation_point_still_has_a_line(self) -> None:
        """The lines are computed from the full text, not the cut copy.

        The result carries at most 2000 characters of a body.  A match after
        that point used to have no anchor at all, which is the case the
        caller cannot recover from without reading the file.
        """
        filler = "".join("    int pad;\n" for _ in range(400))
        source = f"void big(void)\n{{\n{filler}    attach(cb);\n}}\n"
        assert len(source) > _SOURCE_CAP_CALLABLE
        lines = _body_match_lines(source, 100, ["attach"])
        assert lines == [100 + 2 + 400]


class TestSourceBudget:
    """A callable keeps more text than a type."""

    def test_a_callable_gets_the_larger_budget(self) -> None:
        assert _SOURCE_CAP_CALLABLE > _SOURCE_CAP_TYPE

    def test_the_callable_kinds_are_the_four_that_hold_statements(self) -> None:
        assert _CALLABLE_KINDS == {"function", "method", "constructor", "destructor"}
