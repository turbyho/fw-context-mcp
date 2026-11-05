"""The slot facts of an initializer list that libclang gives in its syntactic form.

The syntactic form holds no implicit conversion: an overloaded name stays an
overload set, the conversion of a class to a pointer is no call, and a
scalar in braces whose element needs that conversion has the type ``void``.
The type of the place tells each of them (``_slots.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._usr_keys import base_names

SOURCE = """\
typedef void (*cb_t)(int);
static void fa(int v) { (void)v; }
struct A { void m(int); void m(double); };
typedef void (A::*mp_t)(int);
struct t2 { mp_t m; };
static t2 member_pointer = { &A::m };
template <typename T> void tf(T);
typedef void (*sp_t)(double);
static sp_t from_template[] = { tf<double> };
template <class T> struct S { cb_t cb; };
template <class T> void dependent() { S<T> s = { fa }; (void)s; }
struct Wrap { Wrap(cb_t f) : f_(f) {} operator cb_t() const { return f_; } cb_t f_; };
struct H { cb_t cb; };
void scalar(Wrap w) { cb_t a = { w }; a(1); }
void designated(Wrap w) { H h = { .cb = { w } }; h.cb(2); }
static H temporary = { .cb = { Wrap(fa) } };
static H positional = { { Wrap(fa) } };
"""

# The USR of a type holds the place of its definition: `struct Wrap` at
# line 12, column 8, `struct H` at line 13, `struct A` at line 3.
CONVERSION = "R|c:@S@Wrap~s.cpp:12:8@F@operator void (*)(int)#1"


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("syntactic") / "s.cpp"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++20"])
    return base_names(extract_all(unit, with_refs=True).slots, src)


def _line(text: str) -> int:
    return next(i for i, row in enumerate(SOURCE.splitlines(), 1) if text in row)


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "function", "function_type"), [
    ("member_pointer =", "c:@S@A~s.cpp:3:8@F@m#I#", "M:void (A::*)(int)"),
    ("from_template[]", "c:@FT@>1#Ttf#t0.0#v#", "void (double)"),
])
def test_an_overload_set_gets_the_type_key_of_its_place(facts, text, function, function_type):
    """The key equals the one of a site, which the type-only resolution matches."""
    line = _line(text)
    assert [(s.function, s.function_type) for s in facts.sources if s.line == line] == [(function, function_type)]


@pytest.mark.libclang
def test_a_list_of_a_dependent_type_fills_no_variable(facts):
    """``S<T> s = { fa }``: the variable is a struct, no slot."""
    line = _line("S<T> s = { fa }")
    assert [s.slot for s in facts.sources if s.line == line] == [""]


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "target"), [
    ("cb_t a = { w }", "V|"),
    ("H h = { .cb = { w } }", "F|c:@S@H~s.cpp:13:8@FI@cb"),
    ("static H temporary", "F|c:@S@H~s.cpp:13:8@FI@cb"),
    ("static H positional", "F|c:@S@H~s.cpp:13:8@FI@cb"),
])
def test_a_conversion_in_scalar_braces_flows_into_the_place(facts, text, target):
    line = _line(text)
    flows = [(f.source, f.target) for f in facts.flows if f.line == line]
    assert len(flows) == 1 and flows[0][0] == CONVERSION and flows[0][1].startswith(target), flows


@pytest.mark.libclang
def test_a_long_list_with_no_class_element_reads_its_fields_a_few_times(tmp_path, monkeypatch):
    """The type of the place reads all the fields of the list: one element must not ask it each time.

    A positional list of N scalars took time in N squared (3000 bytes: 113 s).
    """
    from fw_context_mcp.indexer import symbols
    from fw_context_mcp.indexer._slots import collect_slot_facts
    from fw_context_mcp.indexer.symbols import _get_index

    src = tmp_path / "big.c"
    values = ", ".join(["1"] * 200)
    src.write_text(
        "typedef void (*cb_t)(int);\nstatic void f(int v) { (void)v; }\n"
        "struct blob { int id; unsigned char data[200]; cb_t cb; };\n"
        f"static struct blob b = {{ 1, {values}, f }};\n",
        encoding="utf-8",
    )
    calls = {"n": 0}
    original = symbols._positional_struct_fields

    def counted(init_list):
        calls["n"] += 1
        return original(init_list)

    monkeypatch.setattr(symbols, "_positional_struct_fields", counted)
    tu = _get_index().parse(str(src), args=["-x", "c", "-std=c11"])
    collect_slot_facts(tu, None, lambda p: Path(p).resolve())
    assert calls["n"] < 10, calls
