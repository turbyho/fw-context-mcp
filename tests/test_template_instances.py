"""``symbols.template_usr`` must link an instantiation to its template.

The pinned binding (libclang 18.1.1) does not define
``Cursor.specialized_template`` — the name is absent from
``clang/cindex.py``.  The extractor read the attribute anyway, and the broad
``except (ValueError, TypeError, RuntimeError, AttributeError)`` around it
made the AttributeError a silent empty string on EVERY cursor.  Measured over
five C++ indexes before the repair: 11 934 templates, 0 instances.

The consequence reached the tools: ``query_template_instances`` selects
``WHERE template_usr = ?``, thus ``get_template_instances`` could never
answer, and the two suite cases over it ended as SKIP in every round.

The C entry point ``clang_getSpecializedCursorTemplate`` is registered in the
binding's function table with the same argtypes, restype and errcheck, thus
it gives what the property would.  These tests pin all three resolution
levels the indexer uses, because an implicit specialization is NOT a child of
the translation unit — a test that only walks the children would find one
level and miss two.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang

SOURCE = """\
template <typename T>
struct Box {
    T value;
    T get() const { return value; }
};

template <typename T>
T twice(T x) { return x + x; }

template <>
struct Box<char> { char value; };

Box<int> box_of_int;
Box<double> box_of_double;

int use(void) { return twice<int>(2) + box_of_int.get(); }
"""


def _extract(tmp_path: Path, text: str) -> list:
    """Run the real extractor over *text* and give its symbols."""
    from fw_context_mcp.indexer.compile_commands import parse as parse_cc
    from fw_context_mcp.indexer.symbols import extract_all

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    main = root / "src" / "main.cpp"
    main.write_text(text, encoding="utf-8")

    cc = root / "compile_commands.json"
    cc.write_text(
        json.dumps([{
            "directory": str(root),
            "file": str(main),
            "arguments": ["c++", "-std=c++17", "-c", str(main)],
        }]),
        encoding="utf-8",
    )
    unit = next(iter(parse_cc(cc)))
    return extract_all(unit).symbols


class TestTemplateUsrIsFilled:
    def test_at_least_one_instance_is_linked(self, tmp_path: Path) -> None:
        # The defect was invisible one symbol at a time: an empty
        # template_usr is the right answer for a symbol that instantiates
        # nothing, and every answer was empty.  A count is what shows it.
        linked = [s for s in _extract(tmp_path, SOURCE) if s.template_usr]
        assert linked, "no symbol links to a template"

    def test_a_variable_links_through_its_type(self, tmp_path: Path) -> None:
        # `Box<int> box_of_int;` — the implicit specialization is not a child
        # of the translation unit, thus only the type declaration reaches it.
        symbols = {s.name: s for s in _extract(tmp_path, SOURCE) if s.template_usr}
        assert "box_of_int" in symbols
        assert symbols["box_of_int"].template_usr.endswith("Box")

    def test_two_instances_of_one_template_share_its_usr(self, tmp_path: Path) -> None:
        symbols = {s.name: s for s in _extract(tmp_path, SOURCE) if s.template_usr}
        assert symbols["box_of_int"].template_usr == symbols["box_of_double"].template_usr

    def test_the_template_itself_is_not_an_instance(self, tmp_path: Path) -> None:
        # A primary template instantiates nothing.  Its own row must stay
        # empty, or query_template_instances answers with the template.
        templates = [s for s in _extract(tmp_path, SOURCE) if s.is_template]
        assert templates, "the primary templates must be in the extraction"
        assert all(not s.template_usr for s in templates)

    def test_a_plain_type_links_to_nothing(self, tmp_path: Path) -> None:
        plain = """\
struct Plain { int value; };
Plain plain_value;
int use(void) { return plain_value.value; }
"""
        assert not [s for s in _extract(tmp_path, plain) if s.template_usr]


class TestBindingEntryPoint:
    def test_a_way_to_read_the_template_of_an_instance_exists(self) -> None:
        # The repair rests on this.  When a later binding drops the C entry
        # point AND still has no property, this fails loudly instead of
        # letting every template_usr go quietly empty again.
        from fw_context_mcp.indexer.symbols import _template_of_instance_call

        assert _template_of_instance_call() is not None
