"""The SDK model gives a caller to a function that an SDK starts (``indexer/_sdk_model.py``).

A task of FreeRTOS starts from a stack frame that the port writes as
integers, and a library of the ESP32 Arduino core has no source.  The slot
facts carry the function to the slot of its registration; a rule of the
model tells what runs it:

* a "call" rule: a function calls the slot (origin ``sdk``).  The rule
  applies when the build has no body of the function, or ``always`` when
  the slot facts cannot see the path to the call;
* a "flow" rule: code out of the AST gives the slot to the slot of a
  trigger in C (Zephyr ``arch_new_thread`` → ``z_thread_entry``), and the
  slot resolution gives the edge from the trigger (origin ``slot``).  The
  rule applies when the build has no body of the function of its source
  slot, or ``always`` when that body writes the value as data that the
  slot facts cannot follow.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests._toml import toml_string

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
typedef int (*tcp_connected_fn)(void *arg, struct tcp_pcb *tpcb, int err);
int tcp_connect(struct tcp_pcb *pcb, const void *ipaddr, uint16_t port, tcp_connected_fn connected);
typedef int pthread_once_t;
int pthread_once(pthread_once_t *once_control, void (*init_routine)(void));
typedef void nvm_callback(int status, void *context);
int platform_nvm_init(nvm_callback *callback, void *context);
int platform_nvm_finalize(nvm_callback *callback, void *context);
int platform_nvm_key_create(nvm_callback *callback, const char *key_name, uint16_t value_len, uint32_t flags,
                            void *context);
int platform_nvm_key_delete(nvm_callback *callback, const char *key_name, void *context);
int platform_nvm_write(nvm_callback *callback, const char *key_name, const void *data, uint16_t *data_len,
                       void *context);
int platform_nvm_read(nvm_callback *callback, const char *key_name, void *buf, uint16_t *buf_len, void *context);
int platform_nvm_flush(nvm_callback *callback, void *context);
struct __file { int (*put)(char c, struct __file *stream); };
typedef struct __file FILE;
int fputc(int c, FILE *stream);
int putc(int c, FILE *stream);
int putchar(int c);
int fputs(const char *s, FILE *stream);
int puts(const char *s);
unsigned long fwrite(const void *ptr, unsigned long size, unsigned long nmemb, FILE *stream);
int printf(const char *fmt, ...);
int fprintf(FILE *stream, const char *fmt, ...);
int vprintf(const char *fmt, __builtin_va_list ap);
int vfprintf(FILE *stream, const char *fmt, __builtin_va_list ap);
FILE *fdevopen(int (*put)(char c, FILE *stream), int (*get)(FILE *stream), int (*flush)(FILE *stream));
int fputs_P(const char *s, FILE *stream);
int puts_P(const char *s);
int printf_P(const char *fmt, ...);
int fprintf_P(FILE *stream, const char *fmt, ...);
int vfprintf_P(FILE *stream, const char *fmt, __builtin_va_list ap);
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
typedef void (*osThreadFunc_t)(void *argument);
static void rtx_idle(void *argument) { (void) argument; }
void *svcRtxThreadNew(osThreadFunc_t func, void *argument, const void *attr)
{
    (void) argument; (void) attr;
    fr.pc = (uint32_t) func;
    return 0;
}
void osRtxThreadStartup(void) { svcRtxThreadNew(rtx_idle, 0, 0); }
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
static int on_connected(void *arg, struct tcp_pcb *tpcb, int err) { (void) arg; (void) tpcb; return err; }
static void once_init(void) {}
static pthread_once_t once;
static void nvm_init_done(int status, void *context) { (void) status; (void) context; }
static void nvm_finalize_done(int status, void *context) { (void) status; (void) context; }
static void nvm_create_done(int status, void *context) { (void) status; (void) context; }
static void nvm_delete_done(int status, void *context) { (void) status; (void) context; }
static void nvm_write_done(int status, void *context) { (void) status; (void) context; }
static void nvm_read_done(int status, void *context) { (void) status; (void) context; }
static void nvm_flush_done(int status, void *context) { (void) status; (void) context; }
static int out_put(char c, FILE *stream) { (void) c; (void) stream; return 0; }
static FILE out = { .put = out_put };
static int dev_put(char c, FILE *stream) { (void) c; (void) stream; return 0; }
static void nvm_use(void)
{
    uint16_t len = 4;
    char buf[4];
    platform_nvm_init(nvm_init_done, 0);
    platform_nvm_finalize(nvm_finalize_done, 0);
    platform_nvm_key_create(nvm_create_done, "k", 4, 0, 0);
    platform_nvm_key_delete(nvm_delete_done, "k", 0);
    platform_nvm_write(nvm_write_done, "k", buf, &len, 0);
    platform_nvm_read(nvm_read_done, "k", buf, &len, 0);
    platform_nvm_flush(nvm_flush_done, 0);
}
void app_main(void)
{
    fputs("x", &out);
    printf("%s", fdevopen(dev_put, 0, 0) ? "y" : "n");
    tcp_connect(0, 0, 80, on_connected);
    pthread_once(&once, once_init);
    nvm_use();
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
void post(void (*f)(void)) { frame_word = (uint32_t) f; }
static void posted(void);
void dispatcher(void) { post(posted); }
void runner(void (*f)(void)) { f(); }
static void ran_job(void) {}
static void posted(void) {}
struct holder { void (*fn)(void); };
static struct holder h;
void store_fn(void (*f)(void)) { h.fn = f; }
void call_holder(void) { h.fn(); }
static void held(void) {}
void use(struct tcp_pcb *pcb)
{
    store_fn(held);
    tcp_recv(pcb, on_recv); my_task_start(my_task); port_thread(my_thread);
    my_trampoline(plain); reg_cycle(cycle_job); reg2(fa, fb); ra(mutual_job); outer(two_job); post(posted); runner(ran_job);
}
"""
OWN_RULES = """
[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "my_task_start"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos thread start"
from = { function = "port_thread", param = 0 }
to = { function = "my_trampoline", param = 0 }
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos store"
from = { function = "store_fn", param = 0 }
to = { field = "holder::fn" }

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "a rule with no slot"
function = "use"

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the cycle"
function = "reg_cycle"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "entry A"
function = "reg2"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "entry B"
function = "reg2"
param = 1
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "ra"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "rb"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "in1"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "in2"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos scheduler"
function = "outer"
param = 0
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos dispatcher"
function = "dispatcher"
slot = { function = "post", param = 0 }
always = true

[[call_graph.sdk]]
sdk = "my-rtos"
entry = "the my-rtos runner"
function = "runner"
param = 0
always = true
"""


# An event queue of the shape of the mbed EventQueue: the callable object
# goes into the memory of the event, and `function_call<F>` calls it
# through `void *`.  A `std::function` carries its function as an
# `mbed::Callback` does; a lambda gets its call from the instance of the
# template.
QUEUE_CPP = """\
#include <functional>
#include <new>
namespace events {
typedef void (*cb_t)(void *);
void equeue_post(cb_t cb, void *p);
void *equeue_alloc(unsigned size);
class EventQueue {
public:
    template <typename F> int call(F f)
    {
        void *p = equeue_alloc(sizeof(F));
        new (p) F(f);
        equeue_post(&EventQueue::function_call<F>, p);
        return 1;
    }
    template <typename F> int call_every(int ms, F f) { (void) ms; return call(f); }
    template <typename F, typename A> int call(F f, A a) { return call(context<F, A>(f, a)); }
    template <typename T, typename R> int call(T *obj, R (T::*method)()) { (void) obj; (void) method; return 0; }
private:
    template <typename F> static void function_call(void *p) { (*static_cast<F *>(p))(); }
    template <typename F, typename A> struct context {
        F f; A a;
        context(F f, A a) : f(f), a(a) {}
        void operator()() { f(a); }
    };
};
}
static void with_arg(int x) { (void) x; }
struct Obj { void m() {} };
static Obj obj;
static void job() {}
static void every() {}
static int by_pointer() { return 0; }
void app(events::EventQueue &queue, int n)
{
    queue.call(&by_pointer);
    queue.call(std::function<void()>(job));
    queue.call_every(10, std::function<void()>(every));
    queue.call([n] { (void) n; });
    queue.call(&with_arg, 5);
    queue.call(&obj, &Obj::m);
}
"""


def _index(root: Path, files: dict[str, str], name: str, config: str = "") -> tuple[sqlite3.Connection, str]:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    (root / ".fw-context").mkdir(parents=True)
    db_dir = root.parent / f"db-{name}"
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{name}"\n\n[build]\n\n[index]\ndb_dir = {toml_string(db_dir)}\n' + config, encoding="utf-8",
    )
    for file, text in files.items():
        (root / file).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": file,
         "arguments": ["c++", "-std=c++17", "-c", file] if file.endswith(".cpp") else ["cc", "-std=gnu11", "-c", file]}
        for file in files if file.endswith((".c", ".cpp"))
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
        edge["origins"], edge["ref_kind"], edge["from_usr"], edge["from_file"], edge["from_line"], edge["from_col"],
        edge["to_usr"],
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
def queue(tmp_path_factory) -> tuple[sqlite3.Connection, str]:
    root = tmp_path_factory.mktemp("queue") / "proj"
    root.mkdir()
    conn, config_hash = _index(root, {"queue.cpp": QUEUE_CPP}, "proj-queue")
    yield conn, config_hash
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


_NVM_END = "the end of an NVM operation of the platform"


@pytest.mark.parametrize("callback, function, entry", [
    ("on_connected", "tcp_connect", "the lwIP tcpip thread"),
    ("once_init", "pthread_once", "the first call of pthread_once"),
    ("nvm_init_done", "platform_nvm_init", _NVM_END),
    ("nvm_finalize_done", "platform_nvm_finalize", _NVM_END),
    ("nvm_create_done", "platform_nvm_key_create", _NVM_END),
    ("nvm_delete_done", "platform_nvm_key_delete", _NVM_END),
    ("nvm_write_done", "platform_nvm_write", _NVM_END),
    ("nvm_read_done", "platform_nvm_read", _NVM_END),
    ("nvm_flush_done", "platform_nvm_flush", _NVM_END),
])
def test_a_callback_of_a_library_api_without_body_gets_the_function_of_the_api(without_source, callback, function, entry):
    """The function of a library API calls the callback: `tcp_connect`, `pthread_once` and `platform_nvm_*`.

    Measured on an ESP32 Arduino build (lwIP and pthread have no source) and
    on an Mbed build (no driver of the NVM): these callbacks had no caller.
    """
    conn, config_hash, _ = without_source
    assert _callers(conn, callback) == [(function, ["sdk"])]
    assert _entry(conn, config_hash, callback) == entry


_OUTPUT_FUNCTIONS = sorted([
    "fputc", "putc", "putchar", "fputs", "puts", "fwrite", "printf", "fprintf", "vprintf", "vfprintf",
    "fputs_P", "puts_P", "printf_P", "fprintf_P", "vfprintf_P",
])


def test_each_output_function_of_the_stdio_calls_the_put_function_of_its_file(without_source):
    """picolibc and avr-libc write each character through `stream->put`; a build can link them with no source.

    The slot is the field, thus each output function of the build gets the
    edge, and not only the one that the build calls.  Measured on a Zephyr
    build: 4 functions in `__file::put` had no caller.
    """
    from fw_context_mcp.indexer.db._indirect import sdk_entry

    conn, config_hash, _ = without_source
    assert _callers(conn, "out_put") == [(function, ["sdk"]) for function in _OUTPUT_FUNCTIONS]
    rows = conn.execute(
        "SELECT r.* FROM refs r JOIN symbols t ON t.usr = r.to_usr WHERE t.name = 'out_put' AND r.ref_kind = 'call'"
    ).fetchall()
    assert {
        sdk_entry(conn, config_hash, (
            row["origins"], row["ref_kind"], row["from_usr"], row["from_file"], row["from_line"], row["from_col"],
            row["to_usr"],
        ))
        for row in rows
    } == {"a write to a FILE (stream->put)"}


def test_the_put_function_of_fdevopen_goes_into_the_field_of_the_file(without_source):
    """`fdevopen` stores its parameter 0 in `put` of the new FILE, and the library has no source to show it.

    Before, the slot facts did not see the store, thus the put function had
    no caller.
    """
    conn, _, _ = without_source
    assert _callers(conn, "dev_put") == [(function, ["sdk"]) for function in _OUTPUT_FUNCTIONS]


def test_a_thread_that_the_rtx_kernel_creates_gets_its_caller(without_source):
    """The kernel creates its idle and timer threads with `svcRtxThreadNew`, which writes the stack frame as integers.

    `osThreadNew` reaches `svcRtxThreadNew` through an SVC in assembly, thus
    its own rule keeps the threads of the application.  Measured on an
    Mbed build: `osRtxIdleThread` and `osRtxTimerThread` had no caller.
    """
    conn, config_hash, _ = without_source
    assert _callers(conn, "rtx_idle") == [("svcRtxThreadNew", ["sdk"])]
    assert _entry(conn, config_hash, "rtx_idle") == "the RTX kernel (osKernelStart)"


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


def test_a_callable_object_of_an_event_queue_gets_its_caller(queue):
    """`function_call<F>` calls the object through `void *`: the slots stop at `call(F f)`."""
    conn, config_hash = queue
    assert _callers(conn, "job") == [("events::EventQueue::function_call", ["sdk"])]
    assert _callers(conn, "every") == [("events::EventQueue::function_call", ["sdk"])]
    assert _entry(conn, config_hash, "job") == "the mbed EventQueue (dispatch)"


def test_an_edge_by_the_type_only_does_not_stop_the_edge_of_the_rule(queue):
    """`function_call<int (*)()>` calls through a pointer with no slot: each `int()` function by its type only."""
    from fw_context_mcp.indexer.db._indirect import sdk_entry
    from fw_context_mcp.indexer.models import origin_names

    conn, config_hash = queue
    assert _callers(conn, "by_pointer") == [
        ("events::EventQueue::function_call", ["sdk"]), ("events::EventQueue::function_call", ["type_only"]),
    ]
    entries = {
        tuple(origin_names(row["origins"])): sdk_entry(conn, config_hash, (
            row["origins"], row["ref_kind"], row["from_usr"], row["from_file"], row["from_line"], row["from_col"],
            row["to_usr"],
        ))
        for row in conn.execute(
            "SELECT r.* FROM refs r JOIN symbols t ON t.usr = r.to_usr WHERE t.name = 'by_pointer' AND r.ref_kind = 'call'"
        )
    }
    assert entries == {("sdk",): "the mbed EventQueue (dispatch)", ("type_only",): None}, "a guess gets no entry"


def test_an_overload_of_the_queue_gives_its_function_on_and_no_second_edge(queue):
    """`call(F f, A a)` gives the function to `call(F f)` through a context object: one edge.

    `call(T *obj, R (T::*method)())` holds an object pointer at parameter 0:
    no function reaches the slot of the rule there.
    """
    conn, _ = queue
    assert _callers(conn, "with_arg") == [("events::EventQueue::function_call", ["sdk"])]
    [method] = conn.execute("SELECT usr FROM symbols WHERE qualified_name = 'Obj::m'").fetchall()
    assert conn.execute(
        "SELECT COUNT(*) FROM refs WHERE to_usr = ? AND ref_kind = 'call'", (method[0],),
    ).fetchone()[0] == 0


def test_a_call_that_the_code_gives_gets_no_second_edge_but_the_entry(queue):
    """The instance of `function_call<F>` calls a lambda: the rule adds no edge at the declaration."""
    conn, config_hash = queue
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE kind = 'lambda'")]
    rows = conn.execute(
        "SELECT r.*, c.qualified_name AS caller FROM refs r JOIN symbols c ON c.usr = r.from_usr"
        " WHERE r.to_usr = ? AND r.ref_kind = 'call'", (usr,),
    ).fetchall()
    from fw_context_mcp.indexer.models import origin_names

    assert [(row["caller"], origin_names(row["origins"])) for row in rows] == [
        ("events::EventQueue::function_call", ["instantiation"]),
    ]
    from fw_context_mcp.indexer.db._indirect import sdk_entry

    [row] = rows
    assert sdk_entry(conn, config_hash, (
        row["origins"], row["ref_kind"], row["from_usr"], row["from_file"], row["from_line"], row["from_col"], usr,
    )) == "the mbed EventQueue (dispatch)"


def test_a_library_with_its_source_gets_the_edge_from_the_slots(with_source):
    """lwIP calls `pcb->recv` in C: the rule of `tcp_recv` gives no edge, the slot does."""
    conn, _ = with_source
    assert _callers(conn, "on_recv") == [("tcp_input", ["slot"])]


def test_a_call_rule_of_the_project_applies(with_source):
    conn, _ = with_source
    assert _callers(conn, "my_task") == [("my_task_start", ["sdk"])]


def test_a_call_rule_of_the_project_with_a_slot_of_an_other_function_applies(with_source):
    conn, config_hash = with_source
    assert _callers(conn, "posted") == [("dispatcher", ["sdk"])]
    assert _entry(conn, config_hash, "posted") == "the my-rtos dispatcher"


def test_a_slot_edge_from_the_function_of_a_rule_stops_the_edge_of_the_rule(with_source):
    """`runner` calls `f()` in C: its site gives the edge, and the rule only gives the entry."""
    conn, config_hash = with_source
    assert _callers(conn, "ran_job") == [("runner", ["slot"])]
    assert _entry(conn, config_hash, "ran_job") == "the my-rtos runner"


def test_a_row_that_takes_an_address_gets_no_entry(with_source):
    """`dispatcher` takes the address of `posted` (`indirect`): that is no call."""
    from fw_context_mcp.indexer.db._indirect import sdk_entry

    conn, config_hash = with_source
    [row] = conn.execute(
        "SELECT r.* FROM refs r JOIN symbols t ON t.usr = r.to_usr JOIN symbols c ON c.usr = r.from_usr"
        " WHERE t.name = 'posted' AND c.name = 'dispatcher' AND r.ref_kind = 'indirect'"
    ).fetchall()
    assert sdk_entry(conn, config_hash, (
        row["origins"], row["ref_kind"], row["from_usr"], row["from_file"], row["from_line"], row["from_col"],
        row["to_usr"],
    )) is None


def test_an_index_without_the_table_of_the_links_gives_no_entry(with_source):
    """An index of an older version has no table sdk_links, and a read path does not migrate it."""
    from fw_context_mcp.indexer.db._indirect import sdk_entry

    conn, config_hash = with_source
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    conn.backup(old)
    old.execute("DROP TABLE sdk_links")
    edge = _edge(old, "my_task")
    assert sdk_entry(old, config_hash, (
        edge["origins"], edge["ref_kind"], edge["from_usr"], edge["from_file"], edge["from_line"], edge["from_col"],
        edge["to_usr"],
    )) is None
    old.close()


def test_a_flow_rule_of_the_project_applies(with_source):
    conn, _ = with_source
    assert _callers(conn, "my_thread") == [("my_trampoline", ["slot"])]


def test_a_flow_rule_tells_no_entry_for_a_plain_call_of_the_trigger(with_source):
    """`my_trampoline(plain)` is plain C: no rule started `plain`."""
    conn, config_hash = with_source
    assert _callers(conn, "plain") == [("my_trampoline", ["slot"])]
    assert _entry(conn, config_hash, "plain") is None
    assert _entry(conn, config_hash, "my_thread") == "the my-rtos thread start"


def test_a_flow_rule_does_not_apply_to_a_source_function_with_a_body(with_source):
    """The body of `store_fn` stores the value in C, and the slot facts give the flow.

    Before, the rule applied also then, and gave its entry only to the
    targets that came through `store_fn`, not to the other targets of the
    site (`fdevopen` of picolibc with its source).
    """
    conn, config_hash = with_source
    assert _callers(conn, "held") == [("call_holder", ["slot"])]
    assert _entry(conn, config_hash, "held") is None


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
