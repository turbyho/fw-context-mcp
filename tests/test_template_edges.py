"""An edge to a template instance goes to the code of it: the template (plan of the call edges, phase 4).

An implicit instantiation is no symbol of the index.  An edge to its USR
was invisible to ``find_callers`` of the template: 14 to 19 % of the call
edges of a C++ project went to a USR that is no symbol (probe s6).  An
explicit specialization is written code with a symbol of its own, and it
keeps its USR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang

_CODE = """\
template <class T> int f(T v) { return (int)v; }
template <> int f<char>(char v) { return 7; }
template <> int f<long>(long v);
template <class T> struct Box { int get() { return 1; } int put(); };
template <> struct Box<char> { int get() { return 2; } };
template <> int Box<short>::get() { return 3; }
template <class T> struct Part { int get() { return 4; } };
template <class T> struct Part<T*> { int get() { return 5; } };
int use() {
    Box<int> bi; Box<char> bc; Box<short> bs; Part<int> pi; Part<int*> pp;
    return f(1) + f('c') + f(2L) + bi.get() + bc.get() + bs.get() + pi.get() + pp.get() + bi.put();
}
"""


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("tmpl") / "spec.cpp"
    src.write_text(_CODE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    return extract_all(unit, with_refs=True)


def _call_targets(extracted) -> dict[int, str]:
    """The target of each call on line 11, by its column, as the qualified name of a symbol of the unit."""
    names = {s.usr: s.qualified_name for s in extracted.symbols}
    return {
        r.from_col: names.get(r.to_usr, f"<no symbol: {r.to_usr}>")
        for r in extracted.references
        if r.ref_kind == "call" and r.from_line == 11
    }


def test_an_implicit_instance_is_its_template(extracted):
    targets = _call_targets(extracted)
    assert targets[12] == "f"            # f<int>
    assert targets[36] == "Box::get"     # Box<int>::get
    assert targets[69] == "Part::get"    # Part<int>::get
    assert targets[91] == "Box::put"     # declared only: the template has no body either


def test_a_member_of_a_partial_specialization_is_the_member_of_that_specialization(extracted):
    """Part<int*>::get is code of the partial specialization Part<T*>, not of the primary template."""
    pattern = next(s for s in extracted.symbols if s.qualified_name == "Part::get" and s.line == 8)
    call = next(r for r in extracted.references if r.ref_kind == "call" and r.from_line == 11 and r.from_col == 80)
    assert call.to_usr == pattern.usr


def test_an_explicit_specialization_keeps_its_own_symbol(extracted):
    targets = _call_targets(extracted)
    specialization = {s.usr: s for s in extracted.symbols}
    for col, line in ((19, 2), (58, 6)):   # f<char>, Box<short>::get
        call = next(r for r in extracted.references if r.ref_kind == "call" and r.from_line == 11 and r.from_col == col)
        assert specialization[call.to_usr].line == line, targets[col]


def test_a_member_of_an_explicit_class_specialization_keeps_its_own_symbol(extracted):
    call = next(r for r in extracted.references if r.ref_kind == "call" and r.from_line == 11 and r.from_col == 47)
    symbol = next(s for s in extracted.symbols if s.usr == call.to_usr)
    assert symbol.line == 5, (symbol.qualified_name, symbol.line)


def test_a_declared_only_explicit_specialization_is_not_the_template(extracted):
    """f<long> has no body here; the edge must not move to the template f, whose code it is not."""
    call = next(r for r in extracted.references if r.ref_kind == "call" and r.from_line == 11 and r.from_col == 28)
    symbol = next(s for s in extracted.symbols if s.usr == call.to_usr)
    assert symbol.line == 3, (symbol.qualified_name, symbol.line)


_OUT_OF_LINE = """\
template <class T> struct A { int put(); static int cnt; };
template <class T> int A<T>::put() { return 1; }
template <class T> int A<T>::cnt = 0;
template <class T> int fwd(T v);
template <class T> int fwd(T v) { return (int)v; }
template <class T> int h(T v) { return (int)v; }
typedef int (*fn_t)(int);
struct Ops { fn_t cb; };
int reg(fn_t f);
Ops g_ops = { h<int> };
int use(A<int>& a) { return a.put() + fwd(1) + A<int>::cnt + reg(h<int>); }
"""


@pytest.fixture(scope="module")
def out_of_line(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("ool") / "ool.cpp"
    src.write_text(_OUT_OF_LINE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    return extract_all(unit, with_refs=True)


def _symbol_of(result, usr: str):
    return next((s for s in result.symbols if s.usr == usr), None)


def test_a_member_defined_out_of_its_class_is_its_template(out_of_line):
    """The instance has the place of the definition, not of the declaration in the class."""
    call = next(r for r in out_of_line.references if r.ref_kind == "call" and r.from_line == 11 and r.from_col == 29)
    symbol = _symbol_of(out_of_line, call.to_usr)
    assert symbol is not None and symbol.qualified_name == "A::put", call.to_usr


def test_a_function_template_declared_before_its_definition_is_the_template(out_of_line):
    call = next(r for r in out_of_line.references if r.ref_kind == "call" and r.from_line == 11 and r.from_col == 39)
    symbol = _symbol_of(out_of_line, call.to_usr)
    assert symbol is not None and symbol.qualified_name == "fwd", call.to_usr


def test_the_instance_of_a_static_data_member_keeps_its_own_symbol(out_of_line):
    """It is a symbol of its own (with template_usr), unlike the instance of a function."""
    ref = next(r for r in out_of_line.references if r.from_line == 11 and r.to_usr.endswith("@cnt"))
    symbol = _symbol_of(out_of_line, ref.to_usr)
    assert symbol is not None and symbol.template_usr, ref.to_usr


def test_an_address_of_an_instance_is_an_indirect_edge_to_the_template(out_of_line):
    template = next(s for s in out_of_line.symbols if s.qualified_name == "h")
    indirect = {r.from_line for r in out_of_line.references if r.ref_kind == "indirect" and r.to_usr == template.usr}
    assert indirect == {10, 11}


def test_an_init_list_keeps_the_assignment_of_an_instance(out_of_line):
    """The filter of the stored functions holds the USR of the instance: the row must stay."""
    template = next(s for s in out_of_line.symbols if s.qualified_name == "h")
    rows = [f for f in out_of_line.fp_assignments if f.from_line == 10]
    assert [(f.lhs_name, f.rhs_usr) for f in rows] == [("cb", template.usr)]


_INSTANCES = """\
struct Dev { int run(); int stop(); int idle(); };
int helper();
template <class T> struct Holder {
    T& dev;
    int call() { return dev.run(); }
    virtual int quit() { return dev.stop(); }
};
template class Holder<Dev>;
template <class T> int twice(T& d) { return d.idle() + helper(); }
template int twice<Dev>(Dev&);
template <class T> int outer(T& d) { return twice(d); }
template <class T> int spec(T& d) { return 0; }
template <> int spec<Dev>(Dev& d) { return d.run(); }
int use(Dev& d) { return outer(d) + spec(d); }
"""


@pytest.fixture(scope="module")
def instances(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.models import origin_names
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("inst") / "inst.cpp"
    src.write_text(_INSTANCES, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    result = extract_all(unit, with_refs=True)
    names = {s.usr: s.qualified_name for s in result.symbols}
    return {
        (names.get(r.from_usr or "", r.from_usr), names.get(r.to_usr, r.to_usr), r.from_line): origin_names(r.origins)
        for r in result.references if r.ref_kind == "call"
    }


def test_an_explicit_class_instantiation_gives_the_edges_of_its_members(instances):
    """No call names Holder<Dev>::call, but the instantiation defines it."""
    assert instances[("Holder::call", "Dev::run", 5)] == ["instantiation"]


def test_a_member_that_no_call_names_comes_from_the_methods_of_the_class(instances):
    assert instances[("Holder::quit", "Dev::stop", 6)] == ["instantiation"]


def test_a_function_instance_that_an_instance_calls_gives_the_edges_of_its_body(instances):
    """outer<Dev> calls twice<Dev>; the explicit instantiation of twice has no cursor in libclang 22."""
    assert instances[("twice", "Dev::idle", 9)] == ["instantiation"]


def test_a_call_that_the_template_resolves_is_one_row_with_both_origins(instances):
    """helper() is no dependent call: the walk of the template and of the instance find one site."""
    assert instances[("twice", "helper", 9)] == ["direct", "instantiation"]


def test_an_instance_that_an_instance_names_is_walked_too(instances):
    """use -> outer<Dev> -> twice<Dev>: the call of outer reaches the template twice."""
    assert instances[("outer", "twice", 11)] == ["instantiation"]


def test_an_explicit_specialization_is_written_code_and_no_instance(instances):
    assert instances[("spec", "Dev::run", 13)] == ["direct"]


_LAMBDAS = """\
struct Dev { int run(); int stop(); int idle(); };
int helper();
template <class T> int with_lambda(T& d) { auto l = [&]() { return d.run(); }; return l(); }
template <class T> int nested(T& d) { auto a = [&]() { auto b = [&]() { return d.stop(); }; return b(); }; return a(); }
template <class T> int local(T& d) { struct Local { static int go(T& x) { return x.idle(); } }; return Local::go(d) + helper(); }
int plain(Dev& d) { auto g = [](auto& x) { return x.run(); }; return g(d); }
int use(Dev& d) { return with_lambda(d) + nested(d) + local(d); }
"""


@pytest.fixture(scope="module")
def lambdas(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("lam") / "lam.cpp"
    src.write_text(_LAMBDAS, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    result = extract_all(unit, with_refs=True)
    names = {s.usr: s.qualified_name for s in result.symbols}
    return [
        (names.get(r.from_usr or "", f"<no symbol: {r.from_usr}>"), names.get(r.to_usr, f"<no symbol: {r.to_usr}>"),
         r.from_line, r.from_col)
        for r in result.references if r.ref_kind == "call"
    ]


def _calls_to(edges, target: str) -> list[tuple]:
    return sorted(e for e in edges if e[1] == target)


def test_a_call_in_a_lambda_of_an_instance_is_one_row_of_the_template(lambdas):
    """The call belongs to the lambda, in the walk of the written code and of the instance alike."""
    assert _calls_to(lambdas, "Dev::run") == [
        ("plain::<lambda>", "Dev::run", 6, 51), ("with_lambda::<lambda>", "Dev::run", 3, 68),
    ]


def test_a_call_in_nested_lambdas_is_one_row(lambdas):
    assert _calls_to(lambdas, "Dev::stop") == [("nested::<lambda>::<lambda>", "Dev::stop", 4, 80)]


def test_a_method_of_a_local_class_is_its_own_caller(lambdas):
    callers = {e[0] for e in lambdas if e[1] == "Dev::idle"}
    assert len(callers) == 1 and next(iter(callers)).endswith("Local::go"), callers


def test_a_call_of_a_lambda_is_an_edge_to_the_lambda(lambdas):
    pairs = {(e[0], e[1]) for e in lambdas}
    assert ("with_lambda", "with_lambda::<lambda>") in pairs, lambdas
    assert ("nested::<lambda>", "nested::<lambda>::<lambda>") in pairs, lambdas
    assert ("plain", "plain::<lambda>") in pairs, lambdas


def test_the_walk_of_an_instance_gives_no_edge_to_the_call_operator_of_a_lambda(lambdas):
    """The call operator of a closure is no symbol: the edge goes to the lambda."""
    templates = {"with_lambda", "nested", "local"}
    assert not [
        e for e in lambdas
        if e[0] in templates and e[1].startswith("<no symbol") and "operator()" in e[1]
    ], lambdas
