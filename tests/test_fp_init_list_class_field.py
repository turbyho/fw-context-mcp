"""A class field built from a function keeps its row, also through a call.

A value that is a call stores the call's result, which the index cannot
know, thus ``.cb = pick(fb)`` gives no row for a pointer field.  A class
field is built from the function that the call passes: the mbed idiom
``.k = callback(fa)``, or ``Callback<void(int)>(fa)``.  The first version
of the rule dropped those rows too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
def rows(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("classfield") / "k.cpp"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++20"])
    return extract_all(unit, with_refs=True).fp_assignments


def _fields(rows, text: str) -> set[tuple[str, str]]:
    line = next(i for i, row in enumerate(SOURCE.splitlines(), 1) if text in row)
    return {(f.lhs_name, f.rhs_name) for f in rows if f.from_line == line and f.method == "init_list"}


@pytest.mark.libclang
@pytest.mark.parametrize("text", ["by_helper", "by_constructor"])
def test_a_call_that_builds_a_class_field_keeps_the_row(rows, text):
    assert ("k", "fa") in _fields(rows, text)


@pytest.mark.libclang
def test_a_call_into_a_pointer_field_gives_no_row(rows):
    """``pick`` returns some function: the field does not hold ``fa`` as such."""
    assert _fields(rows, "by_name") == {("k", "fa")}


@pytest.mark.libclang
def test_a_call_that_returns_a_plain_pointer_does_not_build_the_class_field(rows):
    """``pick`` returns some function, and the Callback is built from that, not from ``fa``."""
    assert _fields(rows, "by_returned_pointer") == {("cb", "fa")}


@pytest.mark.libclang
def test_a_factory_of_a_plain_struct_does_not_build_the_field(rows):
    """``make_ops`` returns a struct that it fills: ``o`` does not hold ``fa`` as such."""
    assert _fields(rows, "by_factory") == {("cb", "fa")}


@pytest.mark.libclang
@pytest.mark.parametrize("text", ["by_cast", "by_brace_cast"])
def test_a_functional_cast_to_a_pointer_is_a_cast(rows, text):
    assert _fields(rows, text) == {("k", "fa"), ("cb", "fa")}


@pytest.mark.libclang
def test_each_shape_gets_the_row_that_it_stores(rows):
    """A factory of a class with no function constructor stores nothing, an inherited
    constructor builds its class, ``bool`` holds no function, and a wrapper converts back."""
    assert _fields(rows, "static mixed shapes") == {("d", "fa"), ("cb", "fa")}


@pytest.mark.libclang
def test_a_wrapper_template_on_its_parameter_is_a_wrapper_and_a_box_is_not(rows):
    """``FCb(F *f)`` is a wrapper; ``Box(T value)`` and its copy constructor are not;
    a ``bool`` holds no function, whatever the cast."""
    assert _fields(rows, "static factories built") == {("k", "fa")}


@pytest.mark.libclang
def test_a_new_object_is_not_the_function(rows):
    """``new Wrap(fa)`` gives a pointer to a heap object: the field does not hold ``fa``."""
    assert _fields(rows, "static heap allocated") == set()
