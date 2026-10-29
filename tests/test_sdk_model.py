"""The SDK model gives a caller to a function that an SDK starts (``indexer/_sdk_model.py``).

A task of FreeRTOS starts from a stack frame that the port writes as
integers, and a library of the ESP32 Arduino core has no source.  The slot
facts carry the function to the slot of its registration; a rule of the
model tells what runs it:

* a "call" rule: the registration function calls the slot (origin ``sdk``),
  when the build has no body of it, or always when the start is out of C;
* a "flow" rule: code out of the AST gives the slot to the slot of a
  trigger in C (Zephyr ``arch_new_thread`` → ``z_thread_entry``), and the
  slot resolution gives the edge from the trigger (origin ``slot``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

SDK_H = """\
#include <stdint.h>
typedef void (*TaskFunction_t)(void *);
int xTaskCreatePinnedToCore(TaskFunction_t code, const char *name, uint32_t depth, void *arg, int prio,
                            void *handle, int core);
static inline int xTaskCreate(TaskFunction_t code, const char *name, uint32_t depth, void *arg, int prio,
                              void *handle)
{ return xTaskCreatePinnedToCore(code, name, depth, arg, prio, handle, 0); }
struct tcp_pcb;
typedef int (*tcp_recv_fn)(void *arg, struct tcp_pcb *pcb, void *p, int err);
void tcp_recv(struct tcp_pcb *pcb, tcp_recv_fn recv);
typedef struct { const char *url; int (*event_handler)(void *evt); } esp_http_client_config_t;
void *esp_http_client_init(const esp_http_client_config_t *config);
void NVIC_SetVector(int irq, uint32_t vector);
typedef void (*k_thread_entry_t)(void *p1, void *p2, void *p3);
void k_thread_create(void *thread, k_thread_entry_t entry);
"""
KERNEL_C = """\
#include "sdk.h"
void z_thread_entry(k_thread_entry_t entry, void *p1, void *p2, void *p3) { entry(p1, p2, p3); }
struct frame { uint32_t pc, a1, a2, a3, a4; };
static struct frame fr;
void arch_new_thread(void *thread, void *stack, char *ptr, k_thread_entry_t entry, void *p1, void *p2, void *p3)
{
    (void) thread; (void) stack; (void) ptr;
    fr.pc = (uint32_t) z_thread_entry;
    fr.a1 = (uint32_t) entry; fr.a2 = (uint32_t) p1; fr.a3 = (uint32_t) p2; fr.a4 = (uint32_t) p3;
}
void k_thread_create(void *thread, k_thread_entry_t entry) { arch_new_thread(thread, 0, 0, entry, 0, 0, 0); }
static uint32_t vectors[64];
void NVIC_SetVector(int irq, uint32_t vector) { vectors[irq + 16] = vector; }
"""
APP_C = """\
#include "sdk.h"
static void task_a(void *arg) { (void) arg; }
static void task_b(void *arg) { (void) arg; }
static int on_recv(void *arg, struct tcp_pcb *pcb, void *p, int err) { (void) arg; (void) pcb; (void) p; return err; }
static int on_http(void *evt) { (void) evt; return 0; }
static void uart_isr(void) {}
static void thread_main(void *a, void *b, void *c) { (void) a; (void) b; (void) c; }
void app_main(void)
{
    xTaskCreatePinnedToCore(task_a, "a", 1024, 0, 1, 0, 1);
    xTaskCreate(task_b, "b", 1024, 0, 1, 0);
    tcp_recv(0, on_recv);
    esp_http_client_config_t cfg = { .url = "x", .event_handler = on_http };
    esp_http_client_init(&cfg);
    NVIC_SetVector(5, (uint32_t) uart_isr);
    k_thread_create(0, thread_main);
}
"""

# lwIP with its source, and an SDK of the project that config.toml describes.
LWIP_C = """\
#include <stdint.h>
struct tcp_pcb;
typedef int (*tcp_recv_fn)(void *arg, struct tcp_pcb *pcb, void *p, int err);
struct tcp_pcb { tcp_recv_fn recv; };
void tcp_recv(struct tcp_pcb *pcb, tcp_recv_fn recv) { pcb->recv = recv; }
void tcp_input(struct tcp_pcb *pcb) { pcb->recv(0, pcb, 0, 0); }
static uint32_t frame_word, frame_word2;
void my_task_start(void (*fn)(void)) { frame_word = (uint32_t) fn; }
void port_thread(void (*fn)(void)) { frame_word2 = (uint32_t) fn; }
void my_trampoline(void (*fn)(void)) { fn(); }
static int on_recv(void *arg, struct tcp_pcb *pcb, void *p, int err) { (void) arg; (void) pcb; (void) p; return err; }
static void my_task(void) {}
static void my_thread(void) {}
static void plain(void) {}
static void (*saved)(void);
void reg_cycle(void (*f)(void)) { saved = f; }
void again(void) { reg_cycle(saved); }
static void cycle_job(void) {}
void reg2(void (*a)(void), void (*b)(void)) { frame_word = (uint32_t) a; frame_word2 = (uint32_t) b; }
static void fa(void) {}
static void fb(void) {}
void rb(void (*f)(void));
void ra(void (*f)(void)) { frame_word = (uint32_t) f; if (!f) rb(f); }
void rb(void (*f)(void)) { frame_word2 = (uint32_t) f; if (!f) ra(f); }
static void mutual_job(void) {}
void in1(void (*f)(void)) { frame_word = (uint32_t) f; }
void in2(void (*f)(void)) { frame_word2 = (uint32_t) f; }
void outer(void (*f)(void)) { if (f) in1(f); else in2(f); }
static void two_job(void) {}
void use(struct tcp_pcb *pcb)
{
    tcp_recv(pcb, on_recv); my_task_start(my_task); port_thread(my_thread);
    my_trampoline(plain); reg_cycle(cycle_job); reg2(fa, fb); ra(mutual_job); outer(two_job);
}
"""
OWN_RULES = """
[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "my_task_start"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos thread start"
from = { function = "port_thread", param = 0 }
to = { function = "my_trampoline", param = 0 }

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "a rule with no slot"
function = "use"

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the cycle"
function = "reg_cycle"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "entry A"
function = "reg2"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "entry B"
function = "reg2"
param = 1
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "ra"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "rb"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "in1"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "in2"
param = 0
outside_c = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "outer"
param = 0
outside_c = true
"""


def _index(root: Path, files: dict[str, str], name: str, config: str = "") -> tuple[sqlite3.Connection, str]:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    (root / ".fw-context").mkdir(parents=True)
    db_dir = root.parent / f"db-{name}"
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{name}"\n\n[build]\n\n[index]\ndb_dir = "{db_dir}"\n' + config, encoding="utf-8",
    )
    for file, text in files.items():
        (root / file).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": file, "arguments": ["cc", "-std=gnu11", "-c", file]}
        for file in files if file.endswith(".c")
    ]), encoding="utf-8")
    db_path = db_dir / name / "index.db"
    db_path.parent.mkdir(parents=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id=name, project_name=name, index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    [config_hash] = [row[0] for row in conn.execute("SELECT DISTINCT config_hash FROM symbols")]
    return conn, config_hash


def _callers(conn: sqlite3.Connection, name: str) -> list[tuple[str, list[str]]]:
    from fw_context_mcp.indexer.models import origin_names

    return sorted(
        (row["caller"], origin_names(row["origins"]))
        for row in conn.execute(
            "SELECT c.qualified_name AS caller, r.origins FROM refs r JOIN symbols t ON t.usr = r.to_usr"
            " LEFT JOIN symbols c ON c.usr = r.from_usr WHERE t.name = ? AND r.ref_kind = 'call'",
            (name,),
        )
    )


def _entry(conn: sqlite3.Connection, config_hash: str, name: str) -> str | None:
    from fw_context_mcp.indexer.db._indirect import sdk_entry

    edge = _edge(conn, name)
    return sdk_entry(conn, config_hash, (
        edge["origins"], edge["from_usr"], edge["from_file"], edge["from_line"], edge["from_col"], edge["to_usr"],
    ))


def _edge(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    [row] = conn.execute(
        "SELECT r.* FROM refs r JOIN symbols t ON t.usr = r.to_usr WHERE t.name = ? AND r.ref_kind = 'call'",
        (name,),
    ).fetchall()
    return row


@pytest.fixture(scope="module")
def without_source(tmp_path_factory) -> tuple[sqlite3.Connection, str, Path]:
    root = tmp_path_factory.mktemp("sdk") / "proj"
    root.mkdir()
    conn, config_hash = _index(root, {"sdk.h": SDK_H, "kernel.c": KERNEL_C, "app.c": APP_C}, "proj-sdk")
    yield conn, config_hash, root
    conn.close()


@pytest.fixture(scope="module")
def with_source(tmp_path_factory) -> tuple[sqlite3.Connection, str]:
    root = tmp_path_factory.mktemp("lwip") / "proj"
    root.mkdir()
    conn, config_hash = _index(root, {"lwip.c": LWIP_C}, "proj-lwip", OWN_RULES)
    yield conn, config_hash
    conn.close()


def test_a_task_gets_the_registration_function_as_its_caller(without_source):
    conn, _, _ = without_source
    assert _callers(conn, "task_a") == [("xTaskCreatePinnedToCore", ["sdk"])]


def test_the_edge_stands_at_the_declaration_of_the_registration_function(without_source):
    conn, _, _ = without_source
    edge = _edge(conn, "task_a")
    assert (edge["from_file"], edge["from_line"]) == ("sdk.h", 3)


def test_an_inline_wrapper_gives_no_second_edge(without_source):
    """`xTaskCreate` gives its task to `xTaskCreatePinnedToCore`: one registration, one caller."""
    conn, _, _ = without_source
    assert _callers(conn, "task_b") == [("xTaskCreatePinnedToCore", ["sdk"])]


def test_a_callback_of_a_library_without_source_gets_its_caller(without_source):
    conn, _, _ = without_source
    assert _callers(conn, "on_recv") == [("tcp_recv", ["sdk"])]


def test_a_field_of_a_config_struct_is_a_slot_of_a_rule(without_source):
    conn, _, _ = without_source
    assert _callers(conn, "on_http") == [("esp_http_client_init", ["sdk"])]


def test_a_start_out_of_c_gives_the_edge_also_with_a_body(without_source):
    """`NVIC_SetVector` writes the vector as an integer; the NVIC calls it."""
    conn, _, _ = without_source
    assert _callers(conn, "uart_isr") == [("NVIC_SetVector", ["sdk"])]


def test_a_flow_rule_gives_the_edge_from_the_trigger_in_c(without_source):
    conn, _, _ = without_source
    assert _callers(conn, "thread_main") == [("z_thread_entry", ["slot"])]


def test_an_edge_of_a_rule_tells_its_entry(without_source):
    conn, config_hash, _ = without_source

    assert _entry(conn, config_hash, "task_a") == "the FreeRTOS scheduler (vTaskStartScheduler)"
    assert _entry(conn, config_hash, "thread_main") == "the Zephyr scheduler"
    assert _entry(conn, config_hash, "arch_new_thread") is None, "a plain call has no entry"


def test_the_rows_of_the_callers_carry_the_entry(without_source):
    """``find_callers`` and ``get_symbol_context`` read ``refs_for_symbol``: its rows must give the target."""
    from fw_context_mcp.indexer.db import refs_for_symbol
    from fw_context_mcp.mcp.handlers._indirect_rows import entry_field

    conn, config_hash, _ = without_source
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE name = 'task_a'")]
    [row] = refs_for_symbol(conn, config_hash, usr, "function", ref_kind=["call"], limit=10)

    assert entry_field(conn, config_hash, row) == {"entry": "the FreeRTOS scheduler (vTaskStartScheduler)"}


def test_the_resolution_of_reindex_file_writes_the_links_again(without_source):
    """``reindex_file`` resolves the slots again, and the links of the model come first."""
    from fw_context_mcp.mcp.handlers.maintenance import _resolve_slot_edges_locked

    conn, config_hash, root = without_source
    with conn:
        conn.execute("DELETE FROM sdk_links")
    result: dict = {}
    _resolve_slot_edges_locked(conn, config_hash, root.parent / "db-proj-sdk" / "proj-sdk", root, result)

    assert result == {}
    assert _callers(conn, "task_a") == [("xTaskCreatePinnedToCore", ["sdk"])]


def test_a_library_with_its_source_gets_the_edge_from_the_slots(with_source):
    """lwIP calls `pcb->recv` in C: the rule of `tcp_recv` gives no edge, the slot does."""
    conn, _ = with_source
    assert _callers(conn, "on_recv") == [("tcp_input", ["slot"])]


def test_a_call_rule_of_the_project_applies(with_source):
    conn, _ = with_source
    assert _callers(conn, "my_task") == [("my_task_start", ["sdk"])]


def test_a_flow_rule_of_the_project_applies(with_source):
    conn, _ = with_source
    assert _callers(conn, "my_thread") == [("my_trampoline", ["slot"])]


def test_a_flow_rule_tells_no_entry_for_a_plain_call_of_the_trigger(with_source):
    """`my_trampoline(plain)` is plain C: no rule started `plain`."""
    conn, config_hash = with_source
    assert _callers(conn, "plain") == [("my_trampoline", ["slot"])]
    assert _entry(conn, config_hash, "plain") is None
    assert _entry(conn, config_hash, "my_thread") == "the my-rtos thread start"


def test_a_slot_that_flows_back_to_itself_keeps_its_edge(with_source):
    """`reg_cycle(f)` stores `f`, and `reg_cycle(saved)` registers it again: no other rule runs it."""
    conn, _ = with_source
    assert _callers(conn, "cycle_job") == [("reg_cycle", ["sdk"])]


def test_two_rules_whose_slots_flow_into_each_other_give_their_edges(with_source):
    """`ra` and `rb` call each other: no one of them runs the job in place of the other."""
    conn, _ = with_source
    assert _callers(conn, "mutual_job") == [("ra", ["sdk"]), ("rb", ["sdk"])]


def test_a_wrapper_of_two_inner_rules_gives_an_edge_from_each(with_source):
    """`outer` calls `in1` or `in2`: each is a path, and `outer` gives no edge."""
    conn, _ = with_source
    assert _callers(conn, "two_job") == [("in1", ["sdk"]), ("in2", ["sdk"])]


def test_two_rules_of_one_function_tell_each_its_own_entry(with_source):
    conn, config_hash = with_source
    assert _entry(conn, config_hash, "fa") == "entry A"
    assert _entry(conn, config_hash, "fb") == "entry B"


def test_a_wrong_rule_of_the_project_is_left_out(with_source):
    conn, config_hash = with_source
    entries = {row[0] for row in conn.execute("SELECT entry FROM sdk_links WHERE config_hash = ?", (config_hash,))}
    assert "a rule with no slot" not in entries
    assert {"the my-rtos scheduler", "the my-rtos thread start"} <= entries
