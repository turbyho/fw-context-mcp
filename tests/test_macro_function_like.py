"""The shape of a ``#define``: does it take arguments, and which ones.

The pinned binding (libclang 18.1.1) has no ``Cursor.is_macro_function_like``
— the name is absent from ``clang/cindex.py``.  The extractor called it
anyway, and a broad ``except (ValueError, TypeError, RuntimeError,
AttributeError)`` turned the AttributeError into a silent ``False`` on EVERY
macro.  Measured over six indexes before the repair: 734 386 macros, 0 marked
function-like, and 453 256 of them holding a value that opens with a
parameter list.

The C rule needs no API, and these tests pin it: a macro is function-like
when ``(`` follows the name with NO character between them.  ``#define
SPACED (x)`` is object-like exactly because of that one space, thus a test
that only looks at the spelling would pass over the defect.

The parameter list is the second half of the same defect.  It used to live
inside ``value``, glued to the replacement text — ``MBED_ASSERT`` read
``( expr ) do { … }`` — thus a reader that wanted either one got both.
``macros.params`` now holds the list alone.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.libclang


def _macros(text: str) -> dict[str, object]:
    """Extract the macros of *text* through the real extractor.

    The return maps a macro name to its ``Macro`` record.
    """
    import clang.cindex as cx

    from fw_context_mcp.indexer.symbols import _extract_macros

    path = Path(tempfile.mkdtemp()) / "probe.c"
    path.write_text(text, encoding="utf-8")

    tu = cx.Index.create().parse(
        str(path),
        args=["-std=c11"],
        options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    # A translation unit carries the macros of every builtin and every
    # system header as well.  Keep the ones this file defines.
    return {
        m.name: m
        for m in _extract_macros(tu.cursor, str)
        if Path(m.file).name == "probe.c"
    }


SOURCE = """\
#define EMPTY
#define OBJ 42
#define SPACED (x)
#define FN(x) ((x) + 1)
#define NOARGS() 0
#define VARIADIC(...) f(__VA_ARGS__)
#define MULTI(a, b) \\
    ((a) < (b) ? (a) : (b))
int main(void) { return OBJ; }
"""


class TestFunctionLikeFlag:
    @pytest.mark.parametrize("name", ["FN", "NOARGS", "VARIADIC", "MULTI"])
    def test_macro_that_takes_arguments(self, name: str) -> None:
        assert _macros(SOURCE)[name].is_function_like is True

    @pytest.mark.parametrize("name", ["EMPTY", "OBJ"])
    def test_macro_that_takes_none(self, name: str) -> None:
        assert _macros(SOURCE)[name].is_function_like is False

    def test_a_space_before_the_paren_makes_it_object_like(self) -> None:
        # The whole reason the rule reads offsets and not spelling.  The
        # value of SPACED is the text "(x)", not a parameter list.
        macro = _macros(SOURCE)["SPACED"]
        assert macro.is_function_like is False
        assert macro.value == "( x )"

    def test_a_tab_before_the_paren_makes_it_object_like(self) -> None:
        macro = _macros("#define TABBED\t(y)\nint main(void) { return 0; }\n")["TABBED"]
        assert macro.is_function_like is False

    def test_the_flag_is_not_always_false(self) -> None:
        # The defect this module is about was invisible one macro at a time:
        # False is a legitimate answer for an object-like macro, and every
        # answer was False.  A count is what shows it.
        macros = _macros(SOURCE)
        assert sum(1 for m in macros.values() if m.is_function_like) == 4


class TestParameterListIsItsOwnField:
    @pytest.mark.parametrize(
        ("name", "params"),
        [
            ("FN", "x"),
            ("NOARGS", ""),
            ("VARIADIC", "..."),
            ("MULTI", "a, b"),
        ],
    )
    def test_params_holds_the_list(self, name: str, params: str) -> None:
        assert _macros(SOURCE)[name].params == params

    def test_a_mixed_variadic_keeps_both(self) -> None:
        macro = _macros("#define MIXED(a, ...) g(a, __VA_ARGS__)\n")["MIXED"]
        assert macro.params == "a, ..."

    def test_value_no_longer_holds_the_list(self) -> None:
        # The exact shape the V001 round reported: `value` opened with the
        # parameter list, thus the replacement text could not be read.
        macro = _macros(SOURCE)["FN"]
        assert macro.value == "( ( x ) + 1 )"
        assert not macro.value.startswith("( x )")

    def test_an_object_like_macro_has_no_params(self) -> None:
        macros = _macros(SOURCE)
        assert macros["OBJ"].params == ""
        assert macros["SPACED"].params == ""

    def test_the_flag_separates_NAME_from_NAME_parens(self) -> None:
        # Both have an empty params, thus the string alone cannot tell them
        # apart — which is why is_function_like stays a field of its own.
        macros = _macros(SOURCE)
        assert macros["NOARGS"].params == macros["OBJ"].params == ""
        assert macros["NOARGS"].is_function_like is True
        assert macros["OBJ"].is_function_like is False


class _FakeExtent:
    def __init__(self, start: int, end: int) -> None:
        self.start = SimpleNamespace(offset=start)
        self.end = SimpleNamespace(offset=end)


class _FakeToken:
    """A token with only what the split reads: a spelling and two offsets."""

    def __init__(self, spelling: str, start: int, end: int) -> None:
        self.spelling = spelling
        self.extent = _FakeExtent(start, end)


def _tokens(*spellings: str, gap_after_name: int = 0) -> list[_FakeToken]:
    """Lay the spellings out end to end, with *gap_after_name* after the first."""
    tokens: list[_FakeToken] = []
    offset = 0
    for position, spelling in enumerate(spellings):
        tokens.append(_FakeToken(spelling, offset, offset + len(spelling)))
        offset += len(spelling)
        if position == 0:
            offset += gap_after_name
    return tokens


class TestSplitOnTokensAlone:
    """The rules of the split, without a parser in the way.

    libclang refuses a ``#define`` whose parameter list never closes and
    hands over no cursor for it, thus the guard against that shape cannot be
    reached through a real parse — only here.
    """

    def test_a_list_that_never_closes_keeps_the_text_whole(self) -> None:
        # A malformed token stream must not make the split cut the value.
        from fw_context_mcp.indexer.symbols import _split_macro_definition

        assert _split_macro_definition(_tokens("BAD", "(", "a")) == (False, "", "( a")

    def test_the_gap_after_the_name_decides(self) -> None:
        from fw_context_mcp.indexer.symbols import _split_macro_definition

        touching = _tokens("FN", "(", "x", ")", "1")
        assert _split_macro_definition(touching) == (True, "x", "1")

        spaced = _tokens("FN", "(", "x", ")", "1", gap_after_name=1)
        assert _split_macro_definition(spaced) == (False, "", "( x ) 1")

    def test_a_name_alone_gives_nothing(self) -> None:
        from fw_context_mcp.indexer.symbols import _split_macro_definition

        assert _split_macro_definition(_tokens("EMPTY")) == (False, "", "")

    def test_the_value_may_hold_its_own_parentheses(self) -> None:
        # The depth count, not the first ")", ends the parameter list.
        from fw_context_mcp.indexer.symbols import _split_macro_definition

        stream = _tokens("FN", "(", "x", ")", "(", "(", "x", ")", "+", "1", ")")
        assert _split_macro_definition(stream) == (True, "x", "( ( x ) + 1 )")


class TestMacroSignature:
    """One spelling of how a macro is invoked, for every tool that answers."""

    def test_object_like(self) -> None:
        from fw_context_mcp.utils import macro_signature

        assert macro_signature("VERSION", False, "") == "VERSION"

    def test_function_like_with_parameters(self) -> None:
        from fw_context_mcp.utils import macro_signature

        assert macro_signature("MIN", True, "a, b") == "MIN(a, b)"

    def test_function_like_without_parameters(self) -> None:
        from fw_context_mcp.utils import macro_signature

        assert macro_signature("NOARGS", True, "") == "NOARGS()"
