"""The value of an init-list element is never the field that it assigns.

An element with no designator got its field from the first variable or
field access anywhere in its VALUE.  Thus ``{ &cobj.s }`` wrote
``cobj = s``, and ``{ use_a ? dev_a : dev_b }`` wrote ``use_a = dev_a``
and ``use_a = dev_b``: rows that name a variable as the receiver of a
function it never holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOURCE = """\
typedef void (*cb_t)(int);
static void dev_a(int v) { (void)v; }
static void dev_b(int v) { (void)v; }
struct C { static void s(int); };
static C cobj;
static int use_a;
static cb_t through_object[] = { &cobj.s };
static cb_t chosen[] = { use_a ? dev_a : dev_b };
"""


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("valuefield") / "v.cpp"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++20"])
    return extract_all(unit, with_refs=True)


def _line(text: str) -> int:
    return next(i for i, row in enumerate(SOURCE.splitlines(), 1) if text in row)


@pytest.mark.libclang
@pytest.mark.parametrize("text", ["through_object[]", "chosen[]"])
def test_no_row_names_a_variable_of_the_value(extracted, text):
    line = _line(text)
    assert [(f.lhs_name, f.rhs_name) for f in extracted.fp_assignments if f.from_line == line] == []


@pytest.mark.libclang
def test_the_elements_keep_their_references_and_slots(extracted):
    line = _line("chosen[]")
    slots = sorted((r.to_usr.split("@F@")[-1], r.slot_index) for r in extracted.references
                   if r.from_line == line and r.ref_kind == "indirect")
    assert slots == [("dev_a#I#", 0), ("dev_b#I#", 0)]
