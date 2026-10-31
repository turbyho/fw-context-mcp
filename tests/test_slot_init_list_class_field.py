"""A class field built from a function: the function goes to what builds it.

A value that is a call gives its argument to the parameter of the call,
and the field gets what the call returns (``.cb = pick(fa)``).  A class
field is built by a constructor or a factory (the mbed idiom
``.k = callback(fa)``, or ``Callback<void(int)>(fa)``): the function goes
into the parameter, and the class holds it.  A class that converts to the
pointer of the field gives the return slot of its conversion.  A ``bool``
holds no function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._usr_keys import base_names

SOURCE = """\
template <typename F> class Callback;
template <typename R, typename... A> class Callback<R(A...)> {
public:
    Callback(R (*f)(A...)) : f_(f) {}
    template <typename T> Callback(T *obj, R (T::*m)(A...)) : f_(nullptr) { (void)obj; (void)m; }
private:
    R (*f_)(A...);
};
template <typename R, typename... A> Callback<R(A...)> callback(R (*f)(A...)) { return Callback<R(A...)>(f); }
static void fa(int v) { (void)v; }
typedef void (*cb_t)(int);
static cb_t pick(cb_t f) { return f; }
struct holder { Callback<void(int)> k; cb_t cb; };
static holder by_helper = { .k = callback(fa), .cb = fa };
static holder by_constructor = { .k = Callback<void(int)>(fa), .cb = fa };
static holder by_name = { .k = fa, .cb = pick(fa) };
static holder by_returned_pointer = { .k = pick(fa), .cb = fa };
struct ops { cb_t init; cb_t cb; };
static ops make_ops(cb_t f) { return ops{ f, f }; }
struct config { ops o; cb_t cb; };
static config by_factory = { .o = make_ops(fa), .cb = fa };
static holder by_cast = { .k = fa, .cb = cb_t(fa) };
static holder by_brace_cast = { .k = fa, .cb = cb_t{ fa } };
struct Timer { Timer() {} cb_t cb; };
static Timer make_timer(cb_t f) { Timer t; t.cb = f; return t; }
struct Base { Base(cb_t f) : f_(f) {} cb_t f_; };
struct Derived : Base { using Base::Base; };
struct Wrap { Wrap(cb_t f) : f_(f) {} operator cb_t() const { return f_; } cb_t f_; };
struct mixed { Timer t; Derived d; bool has; cb_t cb; };
static mixed shapes = { .t = make_timer(fa), .d = Derived(fa), .has = bool(fa), .cb = Wrap(fa) };
template <typename F> struct FCb { FCb(F *f) : f_(f) {} FCb(const FCb &o) : f_(o.f_) {} F *f_; };
template <typename F> FCb<F> fcb(F *f) { return FCb<F>(f); }
template <typename T> struct Box { Box(T value) : v(value) {} Box(const Box &) = default; T v; };
static Box<int> make_box(cb_t f) { (void)f; return Box<int>(1); }
struct factories { FCb<void(int)> k; Box<int> b; bool has; bool off; };
static factories built = { .k = fcb(fa), .b = make_box(fa), .has = (bool)fa, .off = !fa };
struct heap { Wrap *wp; cb_t *pp; };
static heap allocated = { new Wrap(fa), new cb_t(fa) };
"""


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("classfield") / "k.cpp"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++20"])
    return base_names(extract_all(unit, with_refs=True).slots, src)


FA = "c:k.cpp@F@fa#I#"
HOLDER_K = "F|c:@S@holder@FI@k"
HOLDER_CB = "F|c:@S@holder@FI@cb"
PICK = "P|c:k.cpp@F@pick#*Fv(#I)#|0"
PICK_RETURN = "R|c:k.cpp@F@pick#*Fv(#I)#"


def _line(text: str) -> int:
    return next(i for i, row in enumerate(SOURCE.splitlines(), 1) if text in row)


def _slots(facts, text: str) -> set[str]:
    """The slots that ``fa`` goes into on the line of *text* ("" when none is known)."""
    line = _line(text)
    return {s.slot for s in facts.sources if s.line == line and s.function == FA}


def _flows(facts, text: str) -> set[tuple[str, str]]:
    line = _line(text)
    return {(f.source, f.target) for f in facts.flows if f.line == line}


@pytest.mark.libclang
def test_a_helper_that_builds_a_class_field_gets_the_function(facts):
    slots = _slots(facts, "by_helper")
    assert HOLDER_CB in slots
    assert any(slot.startswith("P|") and "callback" in slot for slot in slots), slots


@pytest.mark.libclang
def test_a_constructor_that_builds_a_class_field_gets_the_function(facts):
    slots = _slots(facts, "by_constructor")
    assert HOLDER_CB in slots
    assert any(slot.startswith("P|") and "@F@Callback#" in slot for slot in slots), slots


@pytest.mark.libclang
def test_a_call_into_a_pointer_field_gives_its_result(facts):
    """``pick`` gets ``fa``, and the field gets what ``pick`` returns."""
    assert _slots(facts, "by_name") == {HOLDER_K, PICK}
    assert _flows(facts, "by_name") == {(PICK_RETURN, HOLDER_CB)}


@pytest.mark.libclang
def test_a_call_that_returns_a_plain_pointer_builds_the_class_field_from_its_result(facts):
    assert _slots(facts, "by_returned_pointer") == {PICK, HOLDER_CB}
    assert _flows(facts, "by_returned_pointer") == {(PICK_RETURN, HOLDER_K)}


@pytest.mark.libclang
def test_a_factory_of_a_plain_struct_fills_the_fields_in_its_body(facts):
    """``make_ops`` returns a struct that it fills: ``o`` itself is no slot."""
    assert _slots(facts, "by_factory") == {"P|c:k.cpp@F@make_ops#*Fv(#I)#|0", "F|c:@S@config@FI@cb"}


@pytest.mark.libclang
@pytest.mark.parametrize("text", ["by_cast", "by_brace_cast"])
def test_a_functional_cast_to_a_pointer_is_a_cast(facts, text):
    assert _slots(facts, text) == {HOLDER_K, HOLDER_CB}


@pytest.mark.libclang
def test_each_shape_gives_the_function_to_what_stores_it(facts):
    """A factory and a constructor (also an inherited one) get the function, a
    ``bool`` holds none, and a wrapper converts back into the field."""
    assert _slots(facts, "static mixed shapes") == {
        "P|c:k.cpp@F@make_timer#*Fv(#I)#|0",
        "P|c:@S@Derived@F@Base#*Fv(#I)#|0",
        "",
        "P|c:@S@Wrap@F@Wrap#*Fv(#I)#|0",
    }
    assert _flows(facts, "static mixed shapes") == {
        ("R|c:@S@Wrap@F@operator void (*)(int)#1", "F|c:@S@mixed@FI@cb"),
    }


@pytest.mark.libclang
def test_a_bool_holds_no_function_whatever_the_cast(facts):
    slots = _slots(facts, "static factories built")
    assert not any(slot.startswith(("F|", "V|")) for slot in slots), slots
    assert "P|c:k.cpp@F@make_box#*Fv(#I)#|0" in slots
    assert any(slot.startswith("P|") and "fcb" in slot for slot in slots), slots


@pytest.mark.libclang
def test_a_new_object_is_not_the_function(facts):
    """``new Wrap(fa)`` gives a pointer to a heap object: the field does not hold ``fa``."""
    assert _slots(facts, "static heap allocated") == {"P|c:@S@Wrap@F@Wrap#*Fv(#I)#|0", ""}
