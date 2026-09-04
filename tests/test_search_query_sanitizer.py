"""A code pattern must reach FTS5 as a legal query, and keep its meaning.

Two rules meet here and they pull in opposite directions.

FTS5 accepts a bare term of ``[A-Za-z0-9_]`` and one trailing ``*``.
Everything a caller writes to describe C code — ``.attach(``,
``callback(&`` — is a syntax error to it.  The old guard listed the bad
characters (``#\\[]{}^:+-``) and named neither ``.`` nor ``(``, thus such a
query went to FTS5 raw, was rejected, and every search tool answered with
an empty list.  Measured on one project: ``.attach(`` gave 0 results and
``attach`` gave 18 — a false negative that reads as "this code does not
exist".

The repair must not widen the query, though.  ``search_bodies`` looks for
a pattern in code: a space is an AND of two exact tokens and no wildcard
is added, and that is what keeps the answer precise.  Only ``search_code``
and ``search_content`` expand a term to ``term*`` and OR-join the terms.

These tests pin both rules and the boundary between them.
"""

from __future__ import annotations

from fw_context_mcp.indexer.db import _expand_query
from fw_context_mcp.indexer.db._symbols import (
    _expand_token,
    _fts5_safe_token,
    _sanitize_body_query,
)


class TestSafeToken:
    def test_a_bareword_passes(self):
        assert _fts5_safe_token("SELF_TEST") == "SELF_TEST"

    def test_a_trailing_wildcard_survives(self):
        assert _fts5_safe_token("attach*") == "attach*"

    def test_punctuation_becomes_a_phrase(self):
        assert _fts5_safe_token(".attach(") == '".attach("'
        assert _fts5_safe_token("callback(&") == '"callback(&"'
        assert _fts5_safe_token("#define") == '"#define"'

    def test_a_wildcard_stays_outside_the_quotes(self):
        """``"self test"*`` is the FTS5 form: a prefix on the last token."""
        assert _fts5_safe_token(".attach*") == '".attach"*'

    def test_an_operator_is_syntax_and_stays(self):
        for operator in ("AND", "OR", "NOT", "NEAR"):
            assert _fts5_safe_token(operator) == operator

    def test_an_inner_quote_is_doubled(self):
        assert _fts5_safe_token('extern"C') == '"extern""C"'


class TestSanitizeBodyQuery:
    """The repair runs only on a query FTS5 already refused.

    A first version decided up front which tokens looked like syntax, and
    it could not tell ``NEAR(attach rise)`` — an operator — from
    ``.attach(`` — C code with a bracket.  Measured: the NEAR query fell
    from 6 results to 0 and ``^attach`` widened from 23 to 106.  The engine
    is now the judge; ``TestFtsSyntaxSurvivesTheRepair`` in
    ``test_result_line_anchors`` pins that end to end.
    """

    def test_a_pattern_is_repaired_and_not_widened(self):
        assert _sanitize_body_query(".attach(") == '".attach("'
        assert _sanitize_body_query("callback(&") == '"callback(&"'

    def test_no_wildcard_is_added(self):
        """The body search is the precise one — a prefix would widen it."""
        assert _sanitize_body_query("SELF_TEST") == "SELF_TEST"
        assert _sanitize_body_query("attach") == "attach"

    def test_the_space_is_left_alone(self):
        """FTS5 reads a space as AND, and the body search keeps that."""
        assert _sanitize_body_query("CommandType NUM") == "CommandType NUM"

    def test_an_explicit_wildcard_survives(self):
        assert _sanitize_body_query("SELF_TEST*") == "SELF_TEST*"

    def test_a_quoted_query_belongs_to_the_caller(self):
        """An unbalanced quote is not something more quoting can fix."""
        assert _sanitize_body_query('"self test"') == '"self test"'
        assert _sanitize_body_query('unbalanced "quote') == 'unbalanced "quote'

    def test_a_backslash_goes_away_first(self):
        """A pasted string literal carries escapes that FTS5 never accepts."""
        assert _sanitize_body_query('extern \\"C\\"') == 'extern "C"'


class TestExpandToken:
    def test_a_bareword_gets_the_prefix(self):
        assert _expand_token("modem") == "modem*"

    def test_an_existing_wildcard_is_kept_once(self):
        assert _expand_token("modem*") == "modem*"

    def test_punctuation_becomes_a_phrase_without_a_prefix(self):
        """A prefix on a bracket buys nothing — the tokenizer dropped it."""
        assert _expand_token(".attach(") == '".attach("'

    def test_an_operator_is_left_alone(self):
        assert _expand_token("OR") == "OR"


class TestExpandQueryReachesCodePatterns:
    """The name and content searches carried the same defect as the body one."""

    def test_a_call_pattern_is_repaired(self):
        assert _expand_query(".attach(") == '".attach("'
        assert _expand_query("callback(&") == '"callback(&"'

    def test_real_grouping_still_bypasses_the_expansion(self):
        assert _expand_query("(uart OR spi) AND init") == "(uart OR spi) AND init"

    def test_brackets_without_an_operator_no_longer_bypass(self):
        """A deliberate trade, measured on one index: 18 rows became 2442.

        Brackets alone used to disable the expansion, so `(connect write)`
        reached FTS5 as an implicit AND of two exact tokens.  That was an
        accident of the old rule — the same two words without brackets
        always expanded to OR, and FTS5 reads `(a b)` and `a b` alike.  The
        caller who wants AND writes the operator, which still bypasses.
        """
        assert _expand_query("(connect write)") == '"(connect" OR "write)"'
        assert _expand_query("connect AND write") == "connect AND write"

    def test_the_terms_are_or_joined_with_a_prefix(self):
        assert _expand_query("modem init") == "modem* OR init*"
