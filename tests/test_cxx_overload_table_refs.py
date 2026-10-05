"""An overloaded or template function in a C++ init list gives the function that the type picks.

In an init list, libclang gives the syntactic form of the element: the
name of an overloaded or template function is a DECL_REF_EXPR to the
overload set (OVERLOADED_DECL_REF), not to the function that the
conversion chose.  A C++ table of such handlers gave no indirect
reference and no slot.  ``gcb = ov`` resolved already.

The set is resolved only where the storage type is known.  The callee of
a dependent call in a template is an overload set too, and a pick there
turned each direct call into an indirect reference (21 to 1073 indirect
references in one translation unit of a real project).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOURCE = """\
typedef void (*cb_t)(int);
typedef void (*cbd_t)(double);
void ov(int);
void ov(double);
void ov(int, int);
template <typename T> void tf(T) {}
static cb_t by_int[] = { ov, tf<int> };
static cbd_t by_double[] = { ov, &ov };
struct pair { cb_t a; cbd_t b; };
static pair fields = { .a = ov, .b = ov };
struct C { void m(int); void m(int) const; static void s(int); void s(double); };
static void (C::*plain_members[])(int) = { &C::m };
static void (C::*const_members[])(int) const = { &C::m };
static cb_t statics[] = { C::s };
extern "C" void ec(int);
void ec(double);
static cb_t linkage[] = { ec };
void nx(int) noexcept;
void nx(double) noexcept;
static cb_t no_except[] = { nx };
template <class T> void tg(T) {}
template <class T> void dependent(T x) { tg(x); }
"""


@pytest.fixture(scope="module")
def refs(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("overload") / "ov.cpp"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++20"])
    return extract_all(unit, with_refs=True).references


def _slots(refs, text: str) -> list[tuple[str, int | None]]:
    line = next(i for i, row in enumerate(SOURCE.splitlines(), 1) if text in row)
    return sorted((r.to_usr, r.slot_index) for r in refs
                  if r.from_line == line and r.ref_kind == "indirect")


@pytest.mark.libclang
def test_the_type_of_the_table_picks_the_overload(refs):
    assert _slots(refs, "by_double[]") == [("c:@F@ov#d#", 0), ("c:@F@ov#d#", 1)]


@pytest.mark.libclang
def test_a_template_gives_the_template(refs):
    """An implicit specialization is no symbol of the index; the template is."""
    assert _slots(refs, "by_int[]") == [("c:@F@ov#I#", 0), ("c:@FT@>1#Ttf#t0.0#v#", 1)]


@pytest.mark.libclang
def test_the_type_of_each_field_picks_its_overload(refs):
    assert _slots(refs, "static pair fields") == [("c:@F@ov#I#", None), ("c:@F@ov#d#", None)]


@pytest.mark.libclang
def test_a_member_pointer_tells_the_const_overload_apart(refs):
    assert _slots(refs, "plain_members[]") == [("c:@S@C@F@m#I#", 0)]
    assert _slots(refs, "const_members[]") == [("c:@S@C@F@m#I#1", 0)]


@pytest.mark.libclang
def test_a_plain_pointer_takes_the_static_method_only(refs):
    assert _slots(refs, "statics[]") == [("c:@S@C@F@s#I#S", 0)]


@pytest.mark.libclang
def test_linkage_and_noexcept_do_not_stop_the_pick(refs):
    assert _slots(refs, "linkage[]") == [("c:@F@ec", 0)]
    assert _slots(refs, "no_except[]") == [("c:@F@nx#I#", 0)]


@pytest.mark.libclang
def test_a_dependent_call_stays_a_call(refs):
    line = next(i for i, row in enumerate(SOURCE.splitlines(), 1) if "void dependent" in row)
    kinds = {r.ref_kind for r in refs if r.from_line == line and "tg" in r.to_usr}
    assert "indirect" not in kinds, kinds
