"""A positional element of a struct init list assigns the field at its position.

``static struct ops o = { dev_init, dev_cb };`` names no field, thus the
extraction had no name to read and wrote no fp_assignments row for it.
The position of each element is its field, in the order of the struct.

The mapping stops at the first doubt (brace elision, a designator, an
anonymous member, a C++ base class).  The tests of those cases check that
every row that IS written is true; a row that is left out is the price of
not guessing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

C_SOURCE = """\
typedef int (*init_fn_t)(void);
typedef void (*cb_t)(int);
struct ops { init_fn_t init; cb_t cb; };
struct device { const char *name; struct ops ops; };
struct mix { int n; cb_t cb; };
struct elided { cb_t h[2]; cb_t after; };
struct outer { struct ops o; cb_t tail; };
union either { cb_t a; init_fn_t b; };
static int dev_init(void) { return 0; }
static void dev_cb(int v) { (void)v; }
static struct ops plain = { dev_init, dev_cb };
static struct device nested = { "b", { dev_init, dev_cb } };
static struct mix skipped = { 3, dev_cb };
static struct elided spread = { dev_cb, dev_cb, dev_cb };
static struct outer flat = { dev_init, dev_cb, dev_cb };
static struct ops mixed = { dev_init, .cb = dev_cb };
static union either one = { dev_cb };
static cb_t not_a_struct[] = { dev_cb };
static void fa(int v) { (void)v; }
static void fb(int v) { (void)v; }
static void fc(int v) { (void)v; }
struct anon { cb_t a; union { cb_t x; int y; }; cb_t c; };
static struct anon anonymous = { fa, fb, fc };
static struct ops gnu = { cb: fa, init: dev_init };
static cb_t pick(cb_t f) { return f; }
void at_run_time(void) { struct ops called = { dev_init, pick(fb) }; (void)called; }
struct named { char n[4]; cb_t cb; };
static struct named stringed = { "abc", fa };
struct bits { int a : 3; int : 5; cb_t cb; };
static struct bits unnamed_bits = { 1, fa };
struct context { void *ctx; cb_t cb; };
static struct context untyped = { (void *)fa, fb };
struct pops { const struct ops *p; cb_t cb; };
static struct pops nested_pointer = { &(struct ops){ dev_init, fa }, fb };
struct counted { long n; cb_t cb; };
int reg(cb_t f);
static struct counted mentioned_by_call = { 0, fb };
void at_start(void) { struct counted c = { reg(fa), fb }; (void)c; }
static struct counted mentioned_by_size = { sizeof(&fa), fb };
static struct counted cast_address = { (long)fa, fb };
void designated(void) { struct counted d = { .n = reg(fa), .cb = pick(fb) }; (void)d; }
static void fd(int v) { (void)v; }
struct tail2 { cb_t a[2]; cb_t after; cb_t last; };
static struct tail2 scalar_literals = { (cb_t){ fa }, (cb_t){ fb }, fc, fd };
struct names2 { const char *n[2]; cb_t cb; cb_t cb2; };
static struct names2 strings_in_pointers = { "a", "b", fa, fb };
static struct counted chosen = { .n = 1 ? (long)fa : 0, .cb = fb };
int flag;
void nested_calls(void) {
    struct ops in_conditional = { dev_init, flag ? pick(fa) : fb };
    struct ops in_comma = { dev_init, (0, pick(fa)) };
    struct ops designated_conditional = { .cb = flag ? pick(fa) : fb };
    (void)in_conditional; (void)in_comma; (void)designated_conditional;
}
int is_v2(void);
struct many { cb_t arr[3]; };
void runtime_setup(void) {
    struct ops call_in_condition = { .cb = is_v2() ? fa : fb };
    struct ops one_branch_calls = { .cb = flag ? pick(fa) : fb };
    struct many array_with_call = { .arr = { fa, fb, pick(fc) } };
    (void)call_in_condition; (void)one_branch_calls; (void)array_with_call;
}
"""

CXX_SOURCE = """\
typedef void (*cb_t)(int);
static void dev_cb(int v) { (void)v; }
struct base { int id; };
struct derived : base { cb_t cb; };
struct agg { cb_t first; cb_t second; };
static derived with_base = { {1}, dev_cb };
static agg aggregate = { dev_cb, dev_cb };
typedef void (*cbd_t)(double);
void ov(int);
void ov(double);
struct by_type { cbd_t d; };
static by_type overloaded = { ov };
"""


def _extract(tmp_path_factory, name: str, text: str, language: str, args: list[str]):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("positional") / name
    src.write_text(text, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language=language, clang_args=args)
    return extract_all(unit, with_refs=True)


@pytest.fixture(scope="module")
def c_rows(tmp_path_factory):
    return _extract(tmp_path_factory, "pos.c", C_SOURCE, "c", ["-std=c11"])


@pytest.fixture(scope="module")
def cxx_rows(tmp_path_factory):
    return _extract(tmp_path_factory, "pos.cpp", CXX_SOURCE, "cpp", ["-std=c++17"])


def _rows(extracted, source: str, text: str) -> set[tuple[str, str, str]]:
    line = next(i for i, row in enumerate(source.splitlines(), 1) if text in row)
    return {(f.lhs_name, f.rhs_name, f.fn_ptr_type)
            for f in extracted.fp_assignments if f.from_line == line and f.method == "init_list"}


@pytest.mark.libclang
def test_each_position_gives_its_field(c_rows):
    assert _rows(c_rows, C_SOURCE, "struct ops plain") == {
        ("init", "dev_init", "init_fn_t"), ("cb", "dev_cb", "cb_t"),
    }


@pytest.mark.libclang
def test_a_nested_positional_struct_gives_its_fields(c_rows):
    assert _rows(c_rows, C_SOURCE, "struct device nested") == {
        ("init", "dev_init", "init_fn_t"), ("cb", "dev_cb", "cb_t"),
    }


@pytest.mark.libclang
def test_a_field_that_holds_no_function_pointer_keeps_its_position(c_rows):
    assert _rows(c_rows, C_SOURCE, "struct mix skipped") == {("cb", "dev_cb", "cb_t")}


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "truth"), [
    ("struct elided spread", {("h", "dev_cb", "cb_t"), ("after", "dev_cb", "cb_t")}),
    ("struct outer flat", {("init", "dev_init", "init_fn_t"), ("cb", "dev_cb", "cb_t"),
                           ("tail", "dev_cb", "cb_t")}),
    ("struct ops mixed", {("init", "dev_init", "init_fn_t"), ("cb", "dev_cb", "cb_t")}),
])
def test_where_positions_are_in_doubt_every_written_row_is_true(c_rows, text, truth):
    """Brace elision and a designator stop the mapping: no row may name a wrong field."""
    assert _rows(c_rows, C_SOURCE, text) <= truth


@pytest.mark.libclang
def test_a_union_takes_its_first_member(c_rows):
    assert _rows(c_rows, C_SOURCE, "union either one") == {("a", "dev_cb", "cb_t")}


@pytest.mark.libclang
def test_an_array_list_is_not_a_struct(c_rows):
    assert _rows(c_rows, C_SOURCE, "not_a_struct") == set()


@pytest.mark.libclang
def test_a_cxx_aggregate_maps_and_a_base_class_stops_the_mapping(cxx_rows):
    assert _rows(cxx_rows, CXX_SOURCE, "agg aggregate") == {
        ("first", "dev_cb", "cb_t"), ("second", "dev_cb", "cb_t"),
    }
    assert _rows(cxx_rows, CXX_SOURCE, "derived with_base") <= {("cb", "dev_cb", "cb_t")}


@pytest.mark.libclang
def test_the_field_type_picks_an_overload_at_its_position(cxx_rows):
    rows = {(f.lhs_name, f.rhs_usr) for f in cxx_rows.fp_assignments
            if f.from_line == CXX_SOURCE.splitlines().index("static by_type overloaded = { ov };") + 1}
    assert rows == {("d", "c:@F@ov#d#")}


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "truth"), [
    ("struct anon anonymous", {("a", "fa", "cb_t"), ("x", "fb", "cb_t"), ("c", "fc", "cb_t")}),
    ("struct ops gnu", {("cb", "fa", "cb_t"), ("init", "dev_init", "init_fn_t")}),
    ("struct ops called", {("init", "dev_init", "init_fn_t"), ("cb", "fb", "cb_t")}),
])
def test_review_shapes_write_no_wrong_row(c_rows, text, truth):
    """An anonymous member, a GNU designator, and a call as an element."""
    assert _rows(c_rows, C_SOURCE, text) <= truth


@pytest.mark.libclang
def test_a_call_element_does_not_store_its_callee(c_rows):
    assert not any(row[1] == "pick" for row in _rows(c_rows, C_SOURCE, "struct ops called"))


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "row"), [
    ("struct named stringed", ("cb", "fa", "cb_t")),
    ("struct bits unnamed_bits", ("cb", "fa", "cb_t")),
    ("struct context untyped", ("cb", "fb", "cb_t")),
])
def test_a_string_an_unnamed_bit_field_and_a_void_pointer_keep_the_positions(c_rows, text, row):
    assert row in _rows(c_rows, C_SOURCE, text)


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "rows"), [
    ("struct pops nested_pointer", {("cb", "fb", "cb_t")}),
    ("struct counted c =", {("cb", "fb", "cb_t")}),
    ("struct counted mentioned_by_size", {("cb", "fb", "cb_t")}),
    ("struct counted cast_address", {("n", "fa", "long"), ("cb", "fb", "cb_t")}),
    ("struct counted d =", set()),
])
def test_a_field_gets_a_function_only_when_it_stores_it(c_rows, text, rows):
    """A call stores its result, a nested struct its own fields, and an int only a cast address."""
    nested = {("init", "dev_init", "init_fn_t"), ("cb", "fa", "cb_t")} if "nested_pointer" in text else set()
    assert _rows(c_rows, C_SOURCE, text) == rows | nested


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "truth"), [
    ("struct tail2 scalar_literals", {("a", "fa", "cb_t"), ("a", "fb", "cb_t"),
                                      ("after", "fc", "cb_t"), ("last", "fd", "cb_t")}),
    ("struct names2 strings_in_pointers", {("cb", "fa", "cb_t"), ("cb2", "fb", "cb_t")}),
])
def test_brace_elision_by_a_literal_writes_no_wrong_row(c_rows, text, truth):
    """A scalar literal and a string into pointers do not fill a whole aggregate field."""
    assert _rows(c_rows, C_SOURCE, text) <= truth


@pytest.mark.libclang
def test_a_conditional_cast_address_goes_into_an_integer_field(c_rows):
    assert ("n", "fa", "long") in _rows(c_rows, C_SOURCE, "struct counted chosen")


@pytest.mark.libclang
@pytest.mark.parametrize("text", ["in_conditional", "in_comma", "designated_conditional"])
def test_a_call_inside_the_value_stores_none_of_its_arguments(c_rows, text):
    """The field holds what ``pick`` returns: ``fa`` is only its argument."""
    assert ("cb", "fa", "cb_t") not in _rows(c_rows, C_SOURCE, text)


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "rows"), [
    ("call_in_condition", {("cb", "fa", "cb_t"), ("cb", "fb", "cb_t")}),
    ("one_branch_calls", {("cb", "fb", "cb_t")}),
    ("array_with_call", {("arr", "fa", "cb_t"), ("arr", "fb", "cb_t")}),
])
def test_only_the_path_of_the_value_decides(c_rows, text, rows):
    """A call in the condition stores nothing and costs nothing; a call in a branch costs that branch."""
    assert _rows(c_rows, C_SOURCE, text) == rows
