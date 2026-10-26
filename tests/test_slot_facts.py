"""The slot facts of the indirect calls (indexer/_slots.py), from the AST only.

The shapes are those of probe s3 of the call edge plan: a field of a
struct, a global variable that a setter fills, a parameter, a return value,
an initializer list (designated and positional), a lambda in a
`std::function`, a pointer to a member function, and an address cast to
`void *`.  No name is matched: each slot is a USR or a parameter index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang

_C = """\
typedef void (*handler_t)(int);
struct dev { handler_t on_rx; int n; };
void read_flash(int x);
void read_card(int x);
static handler_t g_cb;
void set_cb(handler_t h) { g_cb = h; }
void fire(int v) { g_cb(v); }
void load(struct dev *d) { d->on_rx(1); }
static struct dev flash = { .on_rx = read_flash };
void init(void) {
    set_cb(read_card);
    struct dev card = { read_card, 2 };
    load(&card);
    load(&flash);
    void *p = (void *)read_flash;
    (void)p;
    handler_t *where = &g_cb;
    (void)where;
}
"""

_CPP = """\
#include <functional>
void work(int);
void other(int);
struct Client {
    std::function<void(int)> on_done;
    void (Client::*method)(int);
    void handle(int);
    void start() {
        on_done = [this](int x) { handle(x); };
        method = &Client::handle;
        on_done(1);
        (this->*method)(3);
    }
};
using fn_t = void (*)(int);
fn_t pick(bool a) { return a ? &work : &other; }
void run(bool a) { fn_t f = pick(a); f(4); }
void apply(void (*g)(int), int v) { g(v); }
void relay(fn_t (*chooser)(bool)) { apply(work, 5); chooser(true)(6); }
"""


# The shapes of the review of phase 5a: operands in a macro expansion,
# calls through the result of a call, arrays of slots.
_C_REVIEW = """\
typedef void (*handler_t)(int);
typedef void (*isr_t)(void *);
typedef struct { int a; } res_t;
typedef res_t (*reader_t)(int);
typedef void (*runner_t)(handler_t);
struct ops { handler_t init; handler_t run; };
struct s { handler_t cbs[2]; };
int gpio_isr_handler_add(int pin, isr_t fn, void *arg);
void on_gpio(void *arg);
void abort(void);
void f1(int x);
void f2(int x);
res_t r1(int x);
handler_t pick(int a);
runner_t get_runner(void);
void reg_table(handler_t *t);
#define CHECK(x) do { int rc = (x); if (rc != 0) abort(); } while (0)
#define OPS(i, r) { i, r }
#define CB_OR(cb, d) ((cb) ? (cb) : (d))
#define CALL2(a, b) ((a)(1), (b)(2))
static struct ops o = OPS(f1, f2);
static reader_t readers[2] = { r1, r1 };
static handler_t handlers[4];
handler_t g_a, g_b;
void use(int i, handler_t cb, struct s *p) {
    CHECK(gpio_isr_handler_add(4, on_gpio, 0));
    g_a = CB_OR(cb, f1);
    CALL2(g_a, g_b);
    pick(i)(5);
    get_runner()(f2);
    res_t v = readers[i](i);
    (void)v;
    handler_t *e = &handlers[i];
    handler_t *q = handlers;
    handler_t *r = &p->cbs[i];
    (void)e; (void)q; (void)r;
    *handlers = f2;
    reg_table(handlers);
    handlers[i](3);
}
"""

# The C++ shapes of the review of phase 5a: a lambda converted to a
# pointer, members of class templates, callable objects, default arguments.
_CPP_REVIEW = """\
#include <functional>
typedef void (*isr_t)(void);
typedef void (*task_t)(void *);
typedef void (*cb_t)(int);
void attachInterrupt(int pin, isr_t cb, int mode);
struct timer_args { task_t callback; void *arg; };
volatile int flag;
static isr_t g_isr;
void operatorSelect(void);
void h1(int);
void h2(int);
void takes(cb_t c) { c(1); }
void attach(cb_t cb = h2) { cb(1); }
template <class T> struct Box { cb_t f; static cb_t s; void fire() { f(1); s(2); } };
template <class T> cb_t Box<T>::s;
template <> struct Box<char> { cb_t f; void fire() { f(3); } };
struct Base { void operator()(cb_t) const; };
struct Derived : Base {};
void setup() {
    attachInterrupt(2, []() { flag = 1; }, 1);
    timer_args a = { [](void *) { flag = 2; }, nullptr };
    (void)a;
    g_isr = operatorSelect;
    Box<int> b;
    b.f = h1;
    Box<int>::s = h2;
    Box<char> c;
    c.f = h2;
    std::function<void(cb_t)> w = takes;
    w(h1);
    Derived d;
    d(h2);
}
"""


# The shapes of the second review of phase 5a: operands that are not
# evaluated, read-only pointers to slots, brace elision.
_C_REVIEW2 = """\
typedef void (*cb_t)(int);
void h(int);
void h2(int);
#define ZERO_OR_COMPILE_ERROR(cond) ((int) sizeof(char[1 - 2 * !(cond)]) - 1)
#define IS_ARRAY(a) ZERO_OR_COMPILE_ERROR(!__builtin_types_compatible_p(__typeof__(a), __typeof__(&(a)[0])))
#define ARRAY_SIZE(a) ((unsigned) (IS_ARRAY(a) + (sizeof(a) / sizeof((a)[0]))))
static cb_t handlers[4];
static const cb_t fixed[2] = { h, h2 };
static cb_t other;
struct s { cb_t cbs[2]; int n; };
static struct s packed = { h, h2, 3 };
void dispatch(const cb_t *table, int n);
void *memcpy(void *d, const void *s, unsigned long n);
void test(int i) {
    for (unsigned k = 0; k < ARRAY_SIZE(handlers); k++) handlers[k](1);
    __typeof__(handlers[0]) tmp = h2;
    cb_t c = (__typeof__(other))0;
    tmp(2);
    (h)(3);
    (*h)(4);
    dispatch(fixed, 2);
    memcpy(&other, &handlers[0], sizeof other);
    if (handlers == 0) return;
    (void)c;
}
"""

_CPP_REVIEW2 = """\
#include <array>
typedef void (*cb_t)(int);
void h1(int);
void h2(int);
void h3(int);
static std::array<cb_t, 3> handlers = {h1, h2};
static cb_t g_tab[2];
struct Obj { void operator()(int) const; };
struct Chain { void operator()(int) const; Chain &add(cb_t c); };
void fire(int i, Chain &ch) {
    handlers[i](1);
    handlers[2] = h3;
    for (cb_t cb : g_tab) cb(2);
    Obj()(3);
    ch.add(h1);
}
"""


def _facts(tmp_path: Path, name: str, code: str):
    from fw_context_mcp.indexer._slots import collect_slot_facts
    from fw_context_mcp.indexer.symbols import _get_index

    src = tmp_path / name
    src.write_text(code, encoding="utf-8")
    language = ["-x", "c++", "-std=c++17"] if name.endswith(".cpp") else ["-x", "c"]
    tu = _get_index().parse(str(src), args=language)
    facts = collect_slot_facts(tu, None, lambda p: Path(p).resolve())
    own = str(src)
    return {
        "sources": {(s.function, s.slot, s.line) for s in facts.sources if s.file == own},
        "flows": {(f.source, f.target, f.line) for f in facts.flows if f.file == own},
        "sites": {(s.caller, s.kind, s.slot, s.line) for s in facts.sites if s.file == own},
        "site_ordinals": {(s.slot, s.line, s.ordinal) for s in facts.sites if s.file == own},
        "arguments": {(a.index, a.value_kind, a.value, a.line) for a in facts.arguments if a.file == own},
        "argument_ordinals": {(a.value, a.line, a.ordinal) for a in facts.arguments if a.file == own},
        "types": {(s.function, s.function_type) for s in facts.sources if s.file == own},
        "escapes": {(e.slot, e.reason, e.line) for e in facts.escapes if e.file == own},
    }


@pytest.fixture(scope="module")
def c_facts(tmp_path_factory):
    return _facts(tmp_path_factory.mktemp("c"), "fp.c", _C)


@pytest.fixture(scope="module")
def cpp_facts(tmp_path_factory):
    return _facts(tmp_path_factory.mktemp("cpp"), "fp.cpp", _CPP)


@pytest.fixture(scope="module")
def c_review(tmp_path_factory):
    return _facts(tmp_path_factory.mktemp("c_review"), "review.c", _C_REVIEW)


@pytest.fixture(scope="module")
def cpp_review(tmp_path_factory):
    return _facts(tmp_path_factory.mktemp("cpp_review"), "review.cpp", _CPP_REVIEW)


@pytest.fixture(scope="module")
def c_review2(tmp_path_factory):
    return _facts(tmp_path_factory.mktemp("c_review2"), "review2.c", _C_REVIEW2)


@pytest.fixture(scope="module")
def cpp_review2(tmp_path_factory):
    return _facts(tmp_path_factory.mktemp("cpp_review2"), "review2.cpp", _CPP_REVIEW2)


class TestC:
    def test_a_designated_and_a_positional_initializer_fill_the_field(self, c_facts):
        field = "F|c:@S@dev@FI@on_rx"
        assert ("c:@F@read_flash", field, 9) in c_facts["sources"]
        assert ("c:@F@read_card", field, 12) in c_facts["sources"]

    def test_an_argument_of_a_direct_call_fills_the_parameter(self, c_facts):
        assert ("c:@F@read_card", "P|c:@F@set_cb|0", 11) in c_facts["sources"]

    def test_a_parameter_flows_into_a_global(self, c_facts):
        assert ("P|c:@F@set_cb|0", "V|c:fp.c@g_cb", 6) in c_facts["flows"]

    def test_the_calls_through_a_global_and_a_field_are_sites(self, c_facts):
        assert ("c:@F@fire", "pointer", "V|c:fp.c@g_cb", 7) in c_facts["sites"]
        assert ("c:@F@load", "pointer", "F|c:@S@dev@FI@on_rx", 8) in c_facts["sites"]

    def test_an_address_cast_to_void_pointer_has_no_slot(self, c_facts):
        assert ("c:@F@read_flash", "", 15) in c_facts["sources"]

    def test_the_address_of_a_slot_escapes(self, c_facts):
        assert ("V|c:fp.c@g_cb", "address taken", 17) in c_facts["escapes"]


class TestCpp:
    def test_a_lambda_goes_into_a_function_object(self, cpp_facts):
        lambda_key = "c:@S@Client@F@start#@lambda@fp.cpp:9:19"
        assert (lambda_key, "F|c:@S@Client@FI@on_done", 9) in cpp_facts["sources"]
        assert ("c:@S@Client@F@start#", "object", "F|c:@S@Client@FI@on_done", 11) in cpp_facts["sites"]

    def test_a_pointer_to_a_member_function(self, cpp_facts):
        assert ("c:@S@Client@F@handle#I#", "F|c:@S@Client@FI@method", 10) in cpp_facts["sources"]
        assert ("c:@S@Client@F@start#", "pointer", "F|c:@S@Client@FI@method", 12) in cpp_facts["sites"]

    def test_a_return_value_flows_into_a_variable(self, cpp_facts):
        assert ("c:@F@work#I#", "R|c:@F@pick#b#", 16) in cpp_facts["sources"]
        assert ("c:@F@other#I#", "R|c:@F@pick#b#", 16) in cpp_facts["sources"]
        flows = {(source, line) for source, _target, line in cpp_facts["flows"]}
        assert ("R|c:@F@pick#b#", 17) in flows

    def test_a_function_passed_to_an_indirect_call_is_an_argument(self, cpp_facts):
        """`chooser(true)` returns the pointer that the call at line 19 calls."""
        assert ("c:@F@work#I#", "P|c:@F@apply#*Fv(#I)#I#|0", 19) in cpp_facts["sources"]
        assert any(slot == "P|c:@F@relay#*F*Fv(#I)(#b)#|0" for _caller, _kind, slot, _line in cpp_facts["sites"])


class TestCReview:
    def test_each_operand_of_a_macro_expansion_keeps_its_place(self, c_review):
        """The operands of one expansion have one extent: the node, not the extent, tells them."""
        assert ("c:@F@on_gpio", "P|c:@F@gpio_isr_handler_add|1", 26) in c_review["sources"]
        assert ("c:@F@f1", "F|c:@S@ops@FI@init", 21) in c_review["sources"]
        assert ("c:@F@f2", "F|c:@S@ops@FI@run", 21) in c_review["sources"]
        assert ("c:@F@f1", "V|c:@g_a", 27) in c_review["sources"]
        assert ("P|c:@F@use|1", "V|c:@g_a", 27) in c_review["flows"]

    def test_two_sites_at_one_place_have_two_ordinals(self, c_review):
        assert {("V|c:@g_a", 28, 0), ("V|c:@g_b", 28, 1)} <= c_review["site_ordinals"]

    def test_a_call_through_the_result_of_a_call_is_a_site(self, c_review):
        assert ("c:@F@use", "pointer", "R|c:@F@pick", 29) in c_review["sites"]
        assert ("c:@F@use", "pointer", "R|c:@F@get_runner", 30) in c_review["sites"]
        assert ("c:@F@f2", 30, 0) in c_review["argument_ordinals"]
        assert not any(slot.startswith("P|c:@F@get_runner") for _f, slot, _l in c_review["sources"])

    def test_an_indirect_call_that_returns_a_struct_is_a_site(self, c_review):
        assert ("c:@F@use", "pointer", "V|c:review.c@readers", 31) in c_review["sites"]

    def test_a_pointer_into_an_array_of_slots_escapes(self, c_review):
        escapes = c_review["escapes"]
        assert ("V|c:review.c@handlers", "address taken", 33) in escapes
        assert ("V|c:review.c@handlers", "address taken", 34) in escapes
        assert ("F|c:@S@s@FI@cbs", "address taken", 35) in escapes
        assert ("V|c:review.c@handlers", "address taken", 38) in escapes

    def test_an_element_access_does_not_escape(self, c_review):
        assert ("c:@F@f2", "V|c:review.c@handlers", 37) in c_review["sources"]
        assert ("c:@F@use", "pointer", "V|c:review.c@handlers", 39) in c_review["sites"]
        assert not any(line in (37, 39) for _slot, _why, line in c_review["escapes"])


class TestCppReview:
    LAMBDA_ARGUMENT = "c:@F@setup#@lambda@review.cpp:20:24"
    LAMBDA_FIELD = "c:@F@setup#@lambda@review.cpp:21:22"

    def test_a_lambda_converted_to_a_pointer_goes_into_the_parameter(self, cpp_review):
        assert (self.LAMBDA_ARGUMENT, "P|c:@F@attachInterrupt#I#*Fv()#I#|1", 20) in cpp_review["sources"]
        assert (self.LAMBDA_ARGUMENT, "void ()") in cpp_review["types"]

    def test_a_lambda_in_a_positional_initializer_fills_the_field(self, cpp_review):
        assert (self.LAMBDA_FIELD, "F|c:@S@timer_args@FI@callback", 21) in cpp_review["sources"]
        assert (self.LAMBDA_FIELD, "void (void *)") in cpp_review["types"]

    def test_a_function_whose_name_starts_with_operator_is_a_source(self, cpp_review):
        assert ("c:@F@operatorSelect#", "V|c:review.cpp@g_isr", 23) in cpp_review["sources"]

    def test_a_member_of_a_template_instance_is_the_member_of_the_template(self, cpp_review):
        assert ("c:@F@h1#I#", "F|c:@ST>1#T@Box@FI@f", 25) in cpp_review["sources"]
        assert ("c:@F@h2#I#", "V|c:@ST>1#T@Box@s", 26) in cpp_review["sources"]
        assert ("c:@ST>1#T@Box@F@fire#", "pointer", "F|c:@ST>1#T@Box@FI@f", 14) in cpp_review["sites"]

    def test_a_member_of_an_explicit_specialization_keeps_its_own_key(self, cpp_review):
        assert ("c:@F@h2#I#", "F|c:@S@Box>#C@FI@f", 28) in cpp_review["sources"]
        assert ("c:@S@Box>#C@F@fire#", "pointer", "F|c:@S@Box>#C@FI@f", 16) in cpp_review["sites"]

    def test_an_argument_of_a_callable_object_is_an_argument_of_the_site(self, cpp_review):
        assert any(
            kind == "object" and slot.startswith("V|") and slot.endswith("@w") and line == 30
            for _caller, kind, slot, line in cpp_review["sites"]
        )
        assert (0, "function", "c:@F@h1#I#", 30) in cpp_review["arguments"]
        assert not any(line == 30 for _f, _slot, line in cpp_review["sources"])

    def test_an_operator_of_a_base_class_makes_a_callable_object(self, cpp_review):
        assert any(kind == "object" and line == 32 for _c, kind, _s, line in cpp_review["sites"])
        assert (0, "function", "c:@F@h2#I#", 32) in cpp_review["arguments"]

    def test_a_default_argument_fills_the_parameter(self, cpp_review):
        assert ("c:@F@h2#I#", "P|c:@F@attach#*Fv(#I)#|0", 13) in cpp_review["sources"]


class TestCReview2:
    HANDLERS = "V|c:review2.c@handlers"
    OTHER = "V|c:review2.c@other"

    def test_an_operand_that_is_not_evaluated_gives_no_fact(self, c_review2):
        """`ARRAY_SIZE`, `__typeof__` in a declaration and in a cast."""
        assert not any(line in (15, 16, 17) for _slot, _why, line in c_review2["escapes"])
        assert not any(source in (self.HANDLERS, self.OTHER) for source, _t, _l in c_review2["flows"])
        assert ("c:@F@h2", 16) in {(f, line) for f, _slot, line in c_review2["sources"]}

    def test_a_function_name_in_parentheses_is_a_direct_call(self, c_review2):
        assert not any(line in (19, 20) for *_rest, line in c_review2["sites"])
        assert not any(line in (19, 20) for *_rest, line in c_review2["sources"])

    def test_a_read_only_pointer_to_a_slot_does_not_escape(self, c_review2):
        escapes = {(slot, line) for slot, _why, line in c_review2["escapes"]}
        assert (self.OTHER, 22) in escapes
        assert (self.HANDLERS, 22) not in escapes
        assert (self.HANDLERS, 23) not in escapes
        assert not any(slot == "V|c:review2.c@fixed" for slot, _line in escapes)

    def test_a_table_passed_as_a_pointer_flows_into_the_parameter(self, c_review2):
        assert ("V|c:review2.c@fixed", "P|c:@F@dispatch|0", 21) in c_review2["flows"]

    def test_brace_elision_fills_each_element_of_an_array_field(self, c_review2):
        assert ("c:@F@h", "F|c:@S@s@FI@cbs", 11) in c_review2["sources"]
        assert ("c:@F@h2", "F|c:@S@s@FI@cbs", 11) in c_review2["sources"]


class TestCppReview2:
    HANDLERS = "V|c:review2.cpp@handlers"

    def test_an_accessor_reads_the_object_that_its_list_filled(self, cpp_review2):
        assert ("c:@F@h1#I#", self.HANDLERS, 6) in cpp_review2["sources"]
        assert ("c:@F@h2#I#", self.HANDLERS, 6) in cpp_review2["sources"]
        accessor = next(
            target for source, target, line in cpp_review2["flows"] if source == self.HANDLERS and line == 11
        )
        assert accessor.startswith("R|") and "operator[]" in accessor
        assert ("c:@F@fire#I#&$@S@Chain#", "pointer", accessor, 11) in cpp_review2["sites"]
        assert ("c:@F@h3#I#", accessor, 12) in cpp_review2["sources"]

    def test_a_range_for_copies_each_element_into_the_loop_variable(self, cpp_review2):
        assert any(
            source == "V|c:review2.cpp@g_tab" and target.endswith("@cb") and line == 13
            for source, target, line in cpp_review2["flows"]
        )

    def test_a_temporary_object_has_no_return_slot(self, cpp_review2):
        assert not any(line == 14 and slot for _c, _k, slot, line in cpp_review2["sites"])

    def test_a_method_that_gives_a_reference_keeps_its_parameter(self, cpp_review2):
        assert any(
            function == "c:@F@h1#I#" and slot.startswith("P|c:@S@Chain@F@add") and slot.endswith("|0")
            and line == 15
            for function, slot, line in cpp_review2["sources"]
        )


def test_a_purged_function_leaves_no_slot_fact(tmp_path: Path):
    from fw_context_mcp.indexer.db import insert_slot_facts, open_db, transaction
    from fw_context_mcp.indexer.db._files import _delete_dangling_incoming_refs

    conn = open_db(tmp_path / "index.db")
    try:
        with transaction(conn):
            insert_slot_facts(
                conn, "ch",
                [("a.c", 1, 1, "c:@F@gone", "void (int)", "V|c:@g"), ("a.c", 2, 1, "c:@F@kept", "void (int)", "V|c:@g")],
                [],
                [],
                [("a.c", 3, 1, 0, 0, "function", "c:@F@gone"), ("a.c", 3, 1, 0, 1, "slot", "c:@F@gone")],
                [],
            )
            _delete_dangling_incoming_refs(conn, "ch", ["c:@F@gone"])
        sources = [row[0] for row in conn.execute("SELECT function FROM slot_sources")]
        arguments = [tuple(row) for row in conn.execute("SELECT value_kind, value FROM slot_arguments")]
        assert sources == ["c:@F@kept"]
        assert arguments == [("slot", "c:@F@gone")]
    finally:
        conn.close()


def test_the_index_holds_the_facts_and_a_reparse_replaces_them(tmp_path: Path):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.db import (
        delete_slot_facts_for_files,
        open_db,
        transaction,
        upsert_build_config,
        upsert_project,
    )
    from fw_context_mcp.indexer.ops import store_symbols_for_unit

    src = tmp_path / "fp.c"
    src.write_text(_C, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c", clang_args=["-std=c11"])
    conn = open_db(tmp_path / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", "cc.json")
        store_symbols_for_unit(conn, unit, "ch", tmp_path, index_refs=True)
        conn.commit()
        sites = conn.execute("SELECT file, line, caller, slot FROM slot_sites ORDER BY line").fetchall()
        sources = conn.execute("SELECT COUNT(*) FROM slot_sources WHERE file = 'fp.c'").fetchone()[0]

        assert [tuple(row) for row in sites] == [
            ("fp.c", 7, "c:@F@fire", "V|c:fp.c@g_cb"),
            ("fp.c", 8, "c:@F@load", "F|c:@S@dev@FI@on_rx"),
        ]
        assert sources == 4

        with transaction(conn):
            delete_slot_facts_for_files(conn, "ch", ["fp.c"])
        left = sum(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("slot_sources", "slot_flows", "slot_sites", "slot_arguments", "slot_escapes")
        )
        assert left == 0
    finally:
        conn.close()
