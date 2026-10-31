"""Four gaps of the slot facts that real projects showed (phase 5e, before the SDK model).

Each test indexes a small project with the full pipeline and reads the call
edges of the slot resolution, as a user sees them:

* a pointer to a function that the code stores as an integer or as a
  `void *` and takes back (`h->handler = (uint32_t) cb;` ... `(cb_t)
  h->handler`): the place is the slot, and a copy of a `void *` place
  carries it;
* a function that a virtual call gives to an interface, which an override
  stores and calls;
* a positional table of a struct type that a header declares opaque
  (`typedef struct ops ops_t;`) and an other header defines;
* a table of tables of actions (a state machine).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401  (pysqlite3 redirect before any sqlite3)
from tests._usr_keys import base_names

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

INTEGER = """\
#include <stdint.h>
typedef void (*irq_t)(uint32_t, int);
typedef void (*entry_t)(void);
struct obj { uint32_t handler; uint32_t ctx; };
static struct obj g;
void reg(irq_t h, uint32_t id) { g.handler = (uint32_t) h; g.ctx = id; }
void fire(void) { irq_t cb = (irq_t) g.handler; cb(g.ctx, 1); }
void my_irq(uint32_t id, int e) { (void)id; (void)e; }
void boot_hook(void) {}
void setup(void) { reg(my_irq, 7); entry_t keep = boot_hook; (void)keep; }
void nul(void) { irq_t z = (irq_t) 0; z(0, 0); }
void boot(void) { entry_t app = (entry_t) *(uint32_t *)0x08004004; app(); }
uint32_t JumpAddress;
void iap(void) { JumpAddress = *(volatile uint32_t *)(0x08004000 + 4); entry_t j = (entry_t) JumpAddress; j(); }
struct scb { uint32_t VTOR; };
extern struct scb *SCB;
void vtor(void) { ((entry_t)(*(uint32_t *)(SCB->VTOR + 4)))(); }
struct ctx { void *user; uint32_t raw; };
static struct ctx c;
void my_user(uint32_t id, int e) { (void)id; (void)e; }
void reg_user(irq_t h) { c.user = (void *) h; c.raw = (uint32_t)(uintptr_t)(void *) h; }
void setup_user(void) { reg_user(my_user); }
void fire_void(void) { irq_t f = (irq_t) c.user; f(0, 1); }
void fire_nested(void) { irq_t f = (irq_t)(void *)(uintptr_t) c.raw; f(0, 2); }
typedef void (*runner_t)(irq_t, int);
static void run_it(irq_t f, int v) { f(0, v); }
runner_t g_runner = run_it;
void via_indirect(void) { g_runner((irq_t) g.handler, 1); }
"""

VIRTUAL = """\
typedef void (*cb_t)(int);
struct Queue { virtual void post(cb_t cb) = 0; virtual cb_t last() = 0; virtual ~Queue() {} };
struct Impl : Queue {
  cb_t pending = nullptr;
  void post(cb_t cb) override { pending = cb; }
  cb_t last() override { return pending; }
  void run() { if (pending) pending(1); }
};
static void on_event(int v) { (void)v; }
static void unrelated(int v) { (void)v; }
cb_t g_keep = unrelated;
void use(Queue &q) { q.post(on_event); cb_t l = q.last(); l(2); }
int main() { Impl i; use(i); i.run(); return 0; }
"""

OPAQUE_H = """\
typedef struct ops ops_t;
"""
OPAQUE_INTERNAL_H = """\
struct ops { int id; int (*open)(void); int (*close)(void); };
"""
OPAQUE = """\
#include "ops.h"
#include "ops_internal.h"
static int my_open(void) { return 0; }
static int my_close(void) { return 0; }
static const ops_t table = { 1, my_open, my_close };
int run(const ops_t *o) { return o->open() + o->close(); }
int main(void) { return run(&table); }
"""

TABLES = """\
typedef void (*act_t)(int *ccb);
static void a1(int *c) { (void)c; }
static void a2(int *c) { (void)c; }
static void b1(int *c) { (void)c; }
static const act_t slave_set[] = { a1, a2 };
static const act_t master_set[] = { b1 };
act_t *sets[2];
void init(void) { sets[0] = (act_t *) slave_set; sets[1] = (act_t *) master_set; }
void exec(int s, int a, int *c) { act_t *set = sets[s]; if (set) (*set[a])(c); }
int main(void) { init(); exec(0, 1, 0); return 0; }
"""


def _edges(tmp_path: Path, name: str, source: str, headers: dict[str, str] | None = None) -> set[tuple]:
    """Index *source* as a project and give the edges of the slot resolution: (caller, target, origin)."""
    import sqlite3  # after fw_context_mcp (module level): the pysqlite3 redirect

    from fw_context_mcp.indexer.models import RefOrigin, origin_names
    from fw_context_mcp.indexer.runner import run

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-gaps"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n', encoding="utf-8",
    )
    (root / name).write_text(source, encoding="utf-8")
    for header, text in (headers or {}).items():
        (root / header).write_text(text, encoding="utf-8")
    compiler, std = ("c++", "-std=c++17") if name.endswith(".cpp") else ("cc", "-std=gnu11")
    (root / "compile_commands.json").write_text(json.dumps([{
        "directory": str(root), "file": name, "arguments": [compiler, std, "-c", name, "-o", name + ".o"],
    }]), encoding="utf-8")
    db_path = tmp_path / "proj-gaps" / "index.db"
    db_path.parent.mkdir(parents=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id="proj-gaps", project_name="proj-gaps", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        names = {row["usr"]: row["name"] for row in conn.execute("SELECT usr, name FROM symbols")}
        return {
            (names.get(row["from_usr"], row["from_usr"]), names.get(row["to_usr"], row["to_usr"]),
             origin_names(row["origins"])[0])
            for row in conn.execute(
                "SELECT from_usr, to_usr, origins FROM refs WHERE ref_kind = 'call' AND (origins & ?) != 0",
                (int(RefOrigin.SLOT | RefOrigin.TYPE_ONLY),),
            )
        }
    finally:
        conn.close()


@pytest.fixture(scope="module")
def integer_edges(tmp_path_factory) -> set[tuple]:
    return _edges(tmp_path_factory.mktemp("integer"), "integer.c", INTEGER)


def test_a_function_stored_as_an_integer_is_called_through_its_place(integer_edges):
    assert ("fire", "my_irq", "slot") in integer_edges


def test_a_function_stored_as_a_void_pointer_is_called_through_its_place(integer_edges):
    assert {("fire_void", "my_user", "slot"), ("fire_nested", "my_user", "slot")} <= integer_edges


def test_a_function_taken_back_as_an_argument_of_an_indirect_call_reaches_the_target(integer_edges):
    assert ("run_it", "my_irq", "slot") in integer_edges


def test_a_jump_of_a_bootloader_reaches_no_function_of_the_build(integer_edges):
    """NULL, an address in flash, a variable that holds one, `SCB->VTOR`: no function of the build."""
    assert not {edge for edge in integer_edges if edge[0] in ("nul", "boot", "iap", "vtor")}, integer_edges


def test_an_argument_of_a_virtual_call_reaches_the_override(tmp_path):
    edges = _edges(tmp_path, "virtual.cpp", VIRTUAL)
    assert ("run", "on_event", "slot") in edges
    assert ("use", "on_event", "slot") in edges
    # A pure virtual method has no body by design: its return holds what
    # the overrides return, and no code out of the build writes it.
    assert not {edge for edge in edges if edge[1] == "unrelated"}, edges


TEMPLATE_INTERFACE = """\
typedef void (*cb_t)(int);
template <class T> struct IQueue { virtual void post(T cb) = 0; virtual ~IQueue() {} };
struct Impl : IQueue<cb_t> {
  cb_t pending = nullptr;
  void post(cb_t cb) override { pending = cb; }
  void run() { if (pending) pending(1); }
};
static void on_event(int v) { (void)v; }
void use(IQueue<cb_t> &q) { q.post(on_event); }
int main() { Impl i; use(i); i.run(); return 0; }
"""


def test_an_argument_of_a_virtual_call_of_a_template_interface_reaches_the_override(tmp_path):
    """The call names the member of the template `IQueue::post`, and so does the row of the override."""
    assert ("run", "on_event", "slot") in _edges(tmp_path, "template.cpp", TEMPLATE_INTERFACE)


def test_a_table_of_an_opaque_struct_type_fills_its_fields(tmp_path):
    edges = _edges(tmp_path, "opaque.c", OPAQUE, {"ops.h": OPAQUE_H, "ops_internal.h": OPAQUE_INTERNAL_H})
    assert {("run", "my_open", "slot"), ("run", "my_close", "slot")} <= edges


def test_a_table_of_tables_reaches_its_actions(tmp_path):
    edges = _edges(tmp_path, "tables.c", TABLES)
    assert {("exec", "a1", "slot"), ("exec", "a2", "slot"), ("exec", "b1", "slot")} <= edges


FORWARD = """\
typedef void (*cb_t)(int);
struct Wrap;
struct Conf { cb_t cb; };
struct Wrap { cb_t f_; operator cb_t() const { return f_; } };
static void on_w(int v) { (void)v; }
static Wrap w = { on_w };
static Conf conf = { w };
void fire_w() { conf.cb(1); }
"""


def test_a_class_declared_before_its_definition_converts_into_its_place(tmp_path):
    """The type gives the forward declaration, with no member: the conversion function is in the definition."""
    assert ("fire_w", "on_w", "slot") in _edges(tmp_path, "forward.cpp", FORWARD)


COPIES = """\
#include <stdint.h>
typedef void (*tick_t)(int);
struct timer { void *arg; uint32_t raw; };
static struct timer t;
static void timer_set(struct timer *x, void *arg) { x->arg = arg; }
static void *timer_arg(struct timer *x) { return x->arg; }
static void on_get(int v) { (void)v; }
static void on_copy(int v) { (void)v; }
static void on_raw(int v) { (void)v; }
static void on_tramp(int v) { (void)v; }
struct tmr { void (*fn)(void *); void *arg; };
static struct tmr q;
static void tmr_start(void (*fn)(void *), void *arg) { q.fn = fn; q.arg = arg; }
static void tmr_fire(void) { q.fn(q.arg); }
static void tramp(void *a) { ((tick_t) a)(4); }
static void on_elem(int v) { (void)v; }
static void on_first(int v) { (void)v; }
static void on_field(int v) { (void)v; }
static void *slots[2];
struct box { void *u[2]; };
static struct box b;
void start(void) {
  timer_set(&t, (void *) on_get);
  uint32_t r = (uint32_t)(uintptr_t) on_raw;
  t.raw = r;
  tmr_start(tramp, (void *) on_tramp);
  tmr_fire();
  slots[1] = (void *) on_elem;
  slots[0] = (void *) on_first;
  b.u[1] = (void *) on_field;
}
void fire_getter(void) { void *u = timer_arg(&t); ((tick_t) u)(1); }
void fire_copy(void) { struct timer local; local.arg = (void *) on_copy; void *p = local.arg; tick_t f = (tick_t) p; f(2); }
void fire_raw(void) { uint32_t copy = t.raw; ((tick_t)(uintptr_t) copy)(3); }
void fire_elem(void) { void *x = slots[1]; ((tick_t) x)(5); }
void fire_first(void) { void *x = *slots; ((tick_t) x)(6); }
void fire_field(void) { void *x = b.u[1]; ((tick_t) x)(7); }
"""


def test_a_copy_of_a_void_pointer_place_carries_its_functions(tmp_path):
    """A setter with a `void *` parameter, a getter, a local copy, a timer that gives its argument, an element of an array."""
    edges = _edges(tmp_path, "copies.c", COPIES)
    assert {
        ("fire_getter", "on_get", "slot"), ("fire_copy", "on_copy", "slot"), ("tramp", "on_tramp", "slot"),
        ("fire_elem", "on_elem", "slot"), ("fire_first", "on_first", "slot"), ("fire_field", "on_field", "slot"),
    } <= edges, edges
    # The function goes into the place of the copy only: no call reaches it by its type.
    assert not {edge for edge in edges if edge[2] == "type_only" and edge[1].startswith("on_")}, edges


def test_a_copy_of_an_integer_place_carries_nothing(tmp_path):
    """A known gap: each integer copy gave a flow, 300 times the flows of a project.  No call gets a guess."""
    edges = _edges(tmp_path, "copies.c", COPIES)
    assert not {edge for edge in edges if edge[0] == "fire_raw"}, edges


# ── a slot that code out of the build writes ────────────────────────────────

OUTSIDE_A = """\
typedef void (*cb_t)(int);
void on_a(int v) { (void)v; }
void on_b(int v) { (void)v; }
cb_t g_cb = on_a;
cb_t pick_b(void) { return on_b; }
"""
OUTSIDE_B = """\
typedef void (*cb_t)(int);
extern cb_t g_cb;
extern void *opaque;
cb_t library_cb(void);
void fire(void) { g_cb(1); }
void fire_opaque(void) { ((cb_t) opaque)(2); }
void fire_library(void) { library_cb()(3); }
"""


def _edges_units(tmp_path: Path, sources: dict[str, str]) -> set[tuple]:
    """As ``_edges``, with a unit for each file of *sources*."""
    import sqlite3  # after fw_context_mcp (module level): the pysqlite3 redirect

    from fw_context_mcp.indexer.models import RefOrigin, origin_names
    from fw_context_mcp.indexer.runner import run

    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-outside"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n', encoding="utf-8",
    )
    for name, text in sources.items():
        (root / name).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": name, "arguments": ["cc", "-std=gnu11", "-c", name, "-o", name + ".o"]}
        for name in sources
    ]), encoding="utf-8")
    db_path = tmp_path / "proj-outside" / "index.db"
    db_path.parent.mkdir(parents=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id="proj-outside", project_name="proj-outside", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        names = {row["usr"]: row["name"] for row in conn.execute("SELECT usr, name FROM symbols")}
        return {
            (names.get(row["from_usr"], row["from_usr"]), names.get(row["to_usr"], row["to_usr"]),
             origin_names(row["origins"])[0])
            for row in conn.execute(
                "SELECT from_usr, to_usr, origins FROM refs WHERE ref_kind = 'call' AND (origins & ?) != 0",
                (int(RefOrigin.SLOT | RefOrigin.TYPE_ONLY),),
            )
        }
    finally:
        conn.close()


def test_a_slot_that_no_unit_defines_reaches_its_calls_by_type(tmp_path):
    """`extern void *opaque` and a function with no body: code out of the build writes them."""
    edges = _edges_units(tmp_path, {"a.c": OUTSIDE_A, "b.c": OUTSIDE_B})
    assert {("fire_opaque", "on_a", "type_only"), ("fire_opaque", "on_b", "type_only")} <= edges
    assert {("fire_library", "on_a", "type_only"), ("fire_library", "on_b", "type_only")} <= edges


BOOT_OUTSIDE = """\
#include <stdint.h>
typedef void (*entry_t)(void);
extern uint32_t __app_vectors[];
extern uint32_t APP_ENTRY;
extern const uint32_t *app_table;
uint32_t rom_get_entry(void);
void isr_a(void) {}
void isr_b(void) {}
entry_t g_handlers[] = { isr_a, isr_b };
void jump_array(void) { entry_t e = (entry_t) __app_vectors[1]; e(); }
void jump_value(void) { ((entry_t) APP_ENTRY)(); }
void jump_ptr(void) { ((entry_t) app_table[1])(); }
void jump_rom(void) { ((entry_t) rom_get_entry())(); }
void jump_void(void) { ((entry_t)(void *) APP_ENTRY)(); }
void jump_void_array(void) { entry_t e = (entry_t)(void *)(uintptr_t) __app_vectors[1]; e(); }
"""


def test_a_jump_through_a_linker_symbol_or_the_rom_reaches_no_function_of_the_build(tmp_path):
    """Code out of the build writes a number into an integer place, not a function: no edge by type.

    A cast through `void *` reads the integer place too: the type of the
    place decides, not the type of the last cast.
    """
    edges = _edges(tmp_path, "boot.c", BOOT_OUTSIDE)
    assert not {edge for edge in edges if edge[0].startswith("jump_")}, edges


VOID_OUTSIDE = """\
#include <stdint.h>
#include <string.h>
typedef void (*cb_t)(int);
extern void *opaque;
extern void *tbl[];
void *lib_ctx(void);
int lib_recv(void **out);
static void *slots[4];
int q_send(void *item) { slots[0] = item; return 0; }
int q_recv(void *out) { memcpy(out, &slots[0], sizeof(void *)); return 0; }
void on_a(int v) { (void)v; }
cb_t g_keep = on_a;
void on_msg(int v) { (void)v; }
void producer(void) { q_send((void *) on_msg); }
void fire_copy(void) { void *p = opaque; ((cb_t) p)(2); }
void fire_lib_copy(void) { void *u = lib_ctx(); ((cb_t) u)(4); }
void worker(void) { void *msg; q_recv(&msg); ((cb_t) msg)(1); }
void worker_lib(void) { void *m; lib_recv(&m); ((cb_t) m)(2); }
void fire_table(void) { ((cb_t)(uintptr_t) tbl[2])(3); }
"""


def test_a_void_pointer_place_that_unseen_code_writes_reaches_its_calls_by_type(tmp_path):
    """`extern void *`, a library return, a write through `&msg`: a copy of the place keeps the type answer.

    A cast through an integer reads the `void *` place too (`fire_table`).
    """
    edges = _edges(tmp_path, "void_outside.c", VOID_OUTSIDE)
    assert {
        ("fire_copy", "on_a", "type_only"), ("fire_lib_copy", "on_a", "type_only"),
        ("worker", "on_msg", "type_only"), ("worker_lib", "on_msg", "type_only"),
        ("fire_table", "on_a", "type_only"),
    } <= edges, edges


ROWS = """\
#include <stdint.h>
typedef void (*cb_t)(int);
extern void *opaque;
void *lib_ctx(void);
void *lib_copy(void *dst, const void *src, unsigned n);
void *user;
void fire_user(void) { cb_t c = (cb_t) user; c(1); }
void fire_nested(void) { ((cb_t)(void *)(uintptr_t) opaque)(4); }
void fire_lib(void) { ((cb_t) lib_ctx())(5); }
void copy(void *a, const void *b) { lib_copy(a, b, 4); }
extern void *flag;
int has(void) { return flag != 0; }
static void *slots[4];
void (*sink)(void *, const void *, unsigned);
void clear(void) { sink(slots, 0, sizeof slots); lib_copy(slots, 0, sizeof slots); }
void pass(void *a[]) { sink(a, 0, 0); }
static void *cells[2];
void fire_cell(void) { cb_t c = (cb_t) cells[1]; c(2); }
void **lib_table(void);
void fire_table(void) { cb_t c = (cb_t) lib_table()[1]; c(3); }
#define READ_ONCE(x) (*(const volatile __typeof__(x) *)&(x))
#define WRITE_ONCE(x, v) (*(volatile __typeof__(x) *)&(x) = (v))
static void *once;
static void *ctab[2];
void fire_once(void) { void *x = READ_ONCE(once); ((cb_t) x)(6); void *y = *&ctab[1]; ((cb_t) y)(7); }
cb_t g_read;
cb_t g_write;
void typed_once(cb_t f) { cb_t r = READ_ONCE(g_read); r(8); WRITE_ONCE(g_write, f); }
"""


def _fact_rows(tmp_path: Path, name: str, source: str):
    """The slot facts of one unit, each row as the walk gives it (with its column)."""
    from fw_context_mcp.indexer._slots import collect_slot_facts
    from fw_context_mcp.indexer.symbols import _get_index

    src = tmp_path / name
    src.write_text(source, encoding="utf-8")
    unit = _get_index().parse(str(src), args=["-x", "c", "-std=gnu11"])
    return base_names(collect_slot_facts(unit, None, lambda p: Path(p).resolve()), src)


def test_a_read_of_a_void_pointer_place_gives_each_fact_one_time(tmp_path):
    """The reference and the cast back read one place: one flow, and one `OUTSIDE` row for each place.

    `flag != 0` reads a `void *` place that code out of the build writes,
    and the value goes nowhere: no `OUTSIDE` row.
    """
    from fw_context_mcp.indexer._slots import OUTSIDE

    facts = _fact_rows(tmp_path, "rows.c", ROWS)
    assert len([flow for flow in facts.flows if flow.source == "V|c:@user"]) == 1, facts.flows
    assert len([flow for flow in facts.flows if flow.source.endswith("@cells")]) == 1, facts.flows
    # `lib_table()[1]`: no visit reads an element through a pointer, but the cast back.
    assert len([flow for flow in facts.flows if flow.source == "R|c:@F@lib_table"]) == 1, facts.flows
    outside = [escape.slot for escape in facts.escapes if escape.reason == OUTSIDE]
    assert sorted(outside) == ["R|c:@F@lib_ctx", "R|c:@F@lib_table", "V|c:@opaque"], facts.escapes


def test_a_discarded_return_of_a_library_function_gives_no_escape(tmp_path):
    """`lib_copy(a, b, 4);`: the `void *` that the call gives goes nowhere, thus no row."""
    facts = _fact_rows(tmp_path, "rows.c", ROWS)
    assert not [escape for escape in facts.escapes if escape.slot == "R|c:@F@lib_copy"], facts.escapes


def test_an_array_of_void_pointers_as_a_whole_carries_no_function(tmp_path):
    """`sink(slots, …)` and `sink(a, …)` with `void *a[]` give a pointer to the place, not a value that it holds.

    No flow and no argument of the indirect call.  The escape "address
    taken" tells a write through it.
    """
    facts = _fact_rows(tmp_path, "rows.c", ROWS)
    places = ("@slots", "@pass|0")
    assert not [argument for argument in facts.arguments if argument.value.endswith(places)], facts.arguments
    assert not [flow for flow in facts.flows if flow.source.endswith(places)], facts.flows
    assert "V|c:rows.c@slots" in {escape.slot for escape in facts.escapes if escape.reason == "address taken"}


def test_a_read_through_the_address_of_a_void_pointer_place_goes_on(tmp_path):
    """`READ_ONCE(once)` and `*&ctab[1]` read the value: it goes on into the local.

    `WRITE_ONCE(g_write, f)` writes through the pointer: an escape.  The
    read gives the escape too (a known gap: ``address_taken`` does not
    tell a read through the pointer from a write), which adds targets by
    the type and loses none.
    """
    facts = _fact_rows(tmp_path, "rows.c", ROWS)
    for place in ("@once", "@ctab"):
        assert len([flow for flow in facts.flows if flow.source.endswith(place)]) == 1, facts.flows
    taken = {escape.slot for escape in facts.escapes if escape.reason == "address taken"}
    assert "V|c:@g_write" in taken, taken


RANGE = """\
typedef void (*tick_t)(int);
static void on_each(int v) { (void)v; }
static void *cbs[2];
void start() { cbs[0] = (void *) on_each; }
void fire_each() { for (void *p : cbs) ((tick_t) p)(1); }
"""


def test_a_range_for_over_a_void_pointer_array_carries_its_functions(tmp_path):
    edges = _edges(tmp_path, "range.cpp", RANGE)
    assert ("fire_each", "on_each", "slot") in edges, edges


def test_an_extern_variable_that_an_other_unit_defines_does_not_escape(tmp_path):
    """`extern cb_t g_cb` in b.c, `cb_t g_cb = on_a` in a.c: the call reaches on_a, and on_b by no guess."""
    edges = _edges_units(tmp_path, {"a.c": OUTSIDE_A, "b.c": OUTSIDE_B})
    assert {edge for edge in edges if edge[0] == "fire"} == {("fire", "on_a", "slot")}
