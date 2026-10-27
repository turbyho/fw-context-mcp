"""A positional element of a struct init list fills the field at its position.

``static struct ops o = { dev_init, dev_cb };`` names no field: the
position of each element is its field, in the order of the struct
(``symbols._positional_struct_fields``).

The mapping stops at the first doubt (brace elision, a designator, an
anonymous member, a C++ base class).  An element after the stop gives its
function with no slot (only its type tells its calls).  The tests of those
cases check that each slot that IS given is true; a slot that is left out
is the price of not guessing.
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


def _short(key: str) -> str:
    """`F|…@FI@cb` → `cb`, `P|…@F@pick|0` → `pick|0`, `V|…@arr` → `V:arr`, `R|…@F@pick` → `R:pick`, a code key → its name."""
    if key.startswith("F|"):
        return key.rsplit("@FI@", 1)[-1]
    if key.startswith("P|"):
        code, _, index = key[2:].rpartition("|")
        return f"{_short(code)}|{index}"
    if key.startswith("V|"):
        return "V:" + key.rsplit("@", 1)[-1]
    if key.startswith("R|"):
        return "R:" + _short(key[2:])
    return key.rsplit("@F@", 1)[-1].split("#", 1)[0]


def _line(source: str, text: str) -> int:
    return next(i for i, row in enumerate(source.splitlines(), 1) if text in row)


def _sources(extracted, source: str, text: str) -> set[tuple[str, str]]:
    """(function, slot) of each slot fact on the line of *text*; the slot is "" when none is known."""
    line = _line(source, text)
    return {(_short(s.function), _short(s.slot) if s.slot else "")
            for s in extracted.slots.sources if s.line == line}


def _flows(extracted, source: str, text: str) -> set[tuple[str, str]]:
    line = _line(source, text)
    return {(_short(f.source), _short(f.target)) for f in extracted.slots.flows if f.line == line}


def _slotted(extracted, source: str, text: str) -> set[tuple[str, str]]:
    """The facts that name a slot: each of them must be true."""
    return {fact for fact in _sources(extracted, source, text) if fact[1]}


@pytest.mark.libclang
def test_each_position_fills_its_field(c_rows):
    assert _sources(c_rows, C_SOURCE, "struct ops plain") == {("dev_init", "init"), ("dev_cb", "cb")}


@pytest.mark.libclang
def test_a_nested_positional_struct_fills_its_fields(c_rows):
    assert _sources(c_rows, C_SOURCE, "struct device nested") == {("dev_init", "init"), ("dev_cb", "cb")}


@pytest.mark.libclang
def test_a_field_that_holds_no_function_pointer_keeps_its_position(c_rows):
    assert _sources(c_rows, C_SOURCE, "struct mix skipped") == {("dev_cb", "cb")}


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "truth"), [
    ("struct elided spread", {("dev_cb", "h"), ("dev_cb", "after")}),
    ("struct outer flat", {("dev_init", "init"), ("dev_cb", "cb"), ("dev_cb", "tail")}),
    ("struct ops mixed", {("dev_init", "init"), ("dev_cb", "cb")}),
])
def test_where_positions_are_in_doubt_every_slot_is_true(c_rows, text, truth):
    """Brace elision and a designator stop the mapping: no fact may name a wrong field."""
    assert _slotted(c_rows, C_SOURCE, text) <= truth


@pytest.mark.libclang
def test_a_union_takes_its_first_member(c_rows):
    assert _sources(c_rows, C_SOURCE, "union either one") == {("dev_cb", "a")}


@pytest.mark.libclang
def test_an_array_list_fills_the_array(c_rows):
    assert _sources(c_rows, C_SOURCE, "not_a_struct") == {("dev_cb", "V:not_a_struct")}


@pytest.mark.libclang
def test_a_cxx_aggregate_maps_and_a_base_class_stops_the_mapping(cxx_rows):
    assert _sources(cxx_rows, CXX_SOURCE, "agg aggregate") == {("dev_cb", "first"), ("dev_cb", "second")}
    assert _slotted(cxx_rows, CXX_SOURCE, "derived with_base") <= {("dev_cb", "cb")}


@pytest.mark.libclang
def test_the_field_type_picks_an_overload_at_its_position(cxx_rows):
    line = _line(CXX_SOURCE, "static by_type overloaded = { ov };")
    assert {(s.function, _short(s.slot)) for s in cxx_rows.slots.sources if s.line == line} == {
        ("c:@F@ov#d#", "d"),
    }


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "truth"), [
    ("struct anon anonymous", {("fa", "a"), ("fb", "x"), ("fc", "c")}),
    ("struct ops gnu", {("fa", "cb"), ("dev_init", "init")}),
    ("struct ops called", {("dev_init", "init"), ("fb", "pick|0")}),
])
def test_review_shapes_give_no_wrong_slot(c_rows, text, truth):
    """An anonymous member, a GNU designator, and a call as an element."""
    assert _slotted(c_rows, C_SOURCE, text) <= truth


@pytest.mark.libclang
def test_a_call_element_does_not_store_its_callee(c_rows):
    """The field gets what ``pick`` returns, and ``fb`` is its argument."""
    assert not any(function == "pick" for function, _ in _sources(c_rows, C_SOURCE, "struct ops called"))
    assert ("R:pick", "cb") in _flows(c_rows, C_SOURCE, "struct ops called")


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "fact"), [
    ("struct named stringed", ("fa", "cb")),
    ("struct bits unnamed_bits", ("fa", "cb")),
    ("struct context untyped", ("fb", "cb")),
])
def test_a_string_an_unnamed_bit_field_and_a_void_pointer_keep_the_positions(c_rows, text, fact):
    assert fact in _sources(c_rows, C_SOURCE, text)


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "facts"), [
    ("struct pops nested_pointer", {("dev_init", "init"), ("fa", "cb"), ("fb", "cb")}),
    ("struct counted c =", {("fa", "reg|0"), ("fb", "cb")}),
    ("struct counted mentioned_by_size", {("fb", "cb")}),
    ("struct counted cast_address", {("fa", ""), ("fb", "cb")}),
    ("struct counted d =", {("fa", "reg|0"), ("fb", "pick|0")}),
])
def test_a_field_gets_a_function_only_when_it_stores_it(c_rows, text, facts):
    """A call takes its arguments, a nested struct fills its own fields, and an int holds no function."""
    assert _sources(c_rows, C_SOURCE, text) == facts


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "truth"), [
    ("struct tail2 scalar_literals", {("fa", "a"), ("fb", "a"), ("fc", "after"), ("fd", "last")}),
    ("struct names2 strings_in_pointers", {("fa", "cb"), ("fb", "cb2")}),
])
def test_brace_elision_by_a_literal_gives_no_wrong_slot(c_rows, text, truth):
    """A scalar literal and a string into pointers do not fill a whole aggregate field."""
    assert _slotted(c_rows, C_SOURCE, text) <= truth


@pytest.mark.libclang
def test_a_conditional_cast_address_goes_into_no_slot(c_rows):
    """``(long)fa`` in an integer field: only the type of ``fa`` tells its calls."""
    assert _sources(c_rows, C_SOURCE, "struct counted chosen") == {("fa", ""), ("fb", "cb")}


@pytest.mark.libclang
@pytest.mark.parametrize("text", ["in_conditional", "in_comma", "designated_conditional"])
def test_a_call_inside_the_value_stores_none_of_its_arguments(c_rows, text):
    """The field holds what ``pick`` returns: ``fa`` is only its argument."""
    assert ("fa", "pick|0") in _sources(c_rows, C_SOURCE, text)
    assert ("fa", "cb") not in _sources(c_rows, C_SOURCE, text)
    assert ("R:pick", "cb") in _flows(c_rows, C_SOURCE, text)


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "facts"), [
    ("call_in_condition", {("fa", "cb"), ("fb", "cb")}),
    ("one_branch_calls", {("fa", "pick|0"), ("fb", "cb")}),
    ("array_with_call", {("fa", "arr"), ("fb", "arr"), ("fc", "pick|0")}),
])
def test_only_the_path_of_the_value_decides(c_rows, text, facts):
    """A call in the condition stores nothing; a call in a branch takes its argument."""
    assert _sources(c_rows, C_SOURCE, text) == facts
