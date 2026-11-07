"""The knowledge of the SDKs that the code of a build does not show: what runs a registered function.

WHY this module exists: the slot facts carry a function to the slot of its
registration (`xTaskCreate(task, ...)` puts `task` into parameter 0), but
some SDKs start the function from data that the slot facts do not follow:

* A task or a thread starts from a stack frame that the port writes as
  integers, and assembly code loads the frame.
* A library has no source in the build (the ESP32 Arduino core has only
  the headers of FreeRTOS, ESP-IDF and lwIP), thus no body reads the slot.
* An event queue copies a callable object into the memory of an event,
  and its dispatch reads the object through `void *` (the mbed
  ``EventQueue``: ``new (e) F(f)``, then ``(*static_cast<F *>(p))()``).

Such a function had no caller.  The model gives the missing step, as data.
A rule has one of two shapes:

* ``Flow`` — the value of one slot goes into an other slot, and code out of
  the AST carries it.  A trigger in C exists: Zephyr gives parameter 3 of
  ``arch_new_thread`` to parameter 0 of ``z_thread_entry`` through a stack
  frame.  The slot resolution then gives the edge from the real trigger
  (origin ``SLOT``).  As a ``Call`` rule does, it applies when the build
  has no body of the function of its source slot, and with ``always``
  also with a body.  WHY: a body that stores the value in C gives the flow
  to the slot facts, and the rule then gave an ``entry`` to one part of
  the targets of a site (``fdevopen`` of picolibc from its source).
* ``Call`` — a function runs the function in a slot: a parameter of the
  function, a parameter of an other function, or a field.  The edge goes
  from the function to each target of the slot (origin ``SDK``).  The rule
  applies when the build has no body of the function (a library without
  source).  With ``always``, it applies also with a body: the slot facts
  cannot see the path.  Examples are a FreeRTOS task, an RTX thread, an
  interrupt vector that ``NVIC_SetVector`` writes, and the event memory
  of the mbed ``EventQueue``.  WHY not always: a body that calls the slot
  in C gives the edge from the real trigger (lwIP with its source calls
  ``pcb->recv``).

A ``Call`` rule gives no edge where the code already gives an edge from
its function to the target (other than by the type only): the mbed
``EventQueue::function_call`` calls a lambda through the instance of its
template.  That edge gets the ``entry`` of the rule instead.

A ``Call`` rule whose slot flows into the slot of an other ``Call`` rule
gives no edge (``_slot_resolution``): the inner rule gives it, thus a
wrapper gives no second caller.  ESP-IDF declares ``xTaskCreate`` as an
inline wrapper of ``xTaskCreatePinnedToCore``.  A wrapper that can call
two inner functions gives an edge from each one: each is a path.  A rule
whose slot flows back to itself, and two rules whose slots flow into each
other, give their edges: no one of them runs the functions in place of
the other.

Each rule has an ``entry``: the concept that starts the function (the
scheduler, an interrupt).  The tools show it with the edge.  The place of
the registration needs no column: it is the store of the function into the
slot (``slot_sources``), which ``find_indirect_targets`` gives.

A rule names a function or a field by its qualified name.  Each symbol of
the build with that name gets the rule: an overload, and a ``static
inline`` copy of a header (USR ``c:task.h~<key>@F@xTaskCreate``).  A rule whose
name is not in the build does nothing, thus one table can hold every SDK.

A project adds its own rules in ``[[call_graph.sdk]]`` of
``.fw-context/config.toml`` (``rules_from_config``).

Limits:

* A ``Call`` edge gives no argument to the parameters of its targets
  (`xTaskCreate(task, name, depth, arg)` does not give `arg` to `task`).
* A ``Flow`` rule cannot depend on a value.  Thus ``IRQ_CONNECT`` of
  Zephyr has no rule: ``IRQ_DIRECT_CONNECT`` writes the same field
  (``_isr_list::func``) with the flag ``ISR_FLAG_DIRECT``, and the
  hardware calls such a function, not ``_isr_wrapper``.
* A rule gives an edge from the function of the rule, not from a function
  that returns into the target.  RTX up to 5.7 writes ``osThreadExit`` as
  the return address of the frame of a thread, thus it runs when the
  function of the thread returns.  Its correct callers are those
  functions, and no rule shape gives them: ``osThreadExit`` has no caller.
* A rule cannot select a version of an SDK.  From CMSIS-RTX 5.8, the
  frame of a thread starts ``osThreadEntry``, which calls the function of
  the thread and ``osThreadExit`` in C.  The RTX rules give the edge from
  ``osThreadNew`` and ``svcRtxThreadNew``, and ``osThreadEntry`` has no
  caller.
* A rule needs the function in a slot.  The mbed ``EventQueue::event``
  gives its function to the constructor of ``Event`` in a ``return``
  statement, and the slot facts lose it there.  Thus a handler of
  ``queue.event(handler)`` gets no caller from the model.
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BUILTIN_RULES", "Call", "Field", "Flow", "Param", "Rule", "project_rules", "rules_from_config", "write_links",
]

log = logging.getLogger(__name__)

# The symbol kinds that a rule can name as a function.
_FUNCTION_KINDS = ("function", "method")


@dataclass(frozen=True)
class Param:
    """The slot of a parameter: *index* counts from zero, as in the slot facts."""

    function: str
    index: int


@dataclass(frozen=True)
class Field:
    """The slot of a field, named ``record::field``."""

    name: str


SlotName = Param | Field


@dataclass(frozen=True)
class Flow:
    """Code out of the AST gives the value of *source* to *target*.

    *always*: the rule applies also when the build has the body of the
    function of *source*: the body writes the value as data that the slot
    facts cannot follow (a stack frame of integers).  A field in *source*
    has no body, thus such a rule always applies.
    """

    sdk: str
    entry: str
    source: SlotName
    target: SlotName
    always: bool = False


@dataclass(frozen=True)
class Call:
    """*function* runs the function in *slot*: its own parameter (an index), a parameter of an other function, or a field.

    *always*: the slot facts cannot see the path from the slot to the
    call, thus the rule applies also when the build has the body of
    *function*.
    """

    sdk: str
    entry: str
    function: str
    slot: int | Param | Field
    always: bool = False


Rule = Flow | Call

_FREERTOS = "the FreeRTOS scheduler (vTaskStartScheduler)"
_TIMER_TASK = "the FreeRTOS timer service task"
_INTERRUPT = "an interrupt"
_EVENT_LOOP = "the ESP-IDF default event loop task"
_TCPIP = "the lwIP tcpip thread"
_NVM = "the end of an NVM operation of the platform"
_STDIO_LIBC = "picolibc / avr-libc"
_STDIO_WRITE = "a write to a FILE (stream->put)"
_ZEPHYR = "the Zephyr scheduler"
_RTX = "the RTX kernel (osKernelStart)"
_EVENT_QUEUE = "the mbed EventQueue (dispatch)"
_FUNCTION_CALL = "events::EventQueue::function_call"

BUILTIN_RULES: tuple[Rule, ...] = (
    # ── FreeRTOS, with the ESP-IDF names: the port writes the stack frame.
    Call("FreeRTOS", _FREERTOS, "xTaskCreate", 0, always=True),
    Call("FreeRTOS", _FREERTOS, "xTaskCreateStatic", 0, always=True),
    Call("FreeRTOS", _FREERTOS, "xTaskCreatePinnedToCore", 0, always=True),
    Call("FreeRTOS", _FREERTOS, "xTaskCreateStaticPinnedToCore", 0, always=True),
    Call("FreeRTOS", _TIMER_TASK, "xTimerCreate", 4),
    Call("FreeRTOS", _TIMER_TASK, "xTimerCreateStatic", 4),
    # ── ESP-IDF: the libraries of the ESP32 Arduino core have no source.
    Call("ESP-IDF", _INTERRUPT, "esp_intr_alloc", 2),
    Call("ESP-IDF", _INTERRUPT, "gpio_isr_handler_add", 1),
    Call("ESP-IDF", _INTERRUPT, "timer_isr_callback_add", 2),
    Call("ESP-IDF", _INTERRUPT, "touch_pad_isr_register", 0),
    Call("ESP-IDF", _EVENT_LOOP, "esp_event_handler_register", 2),
    Call("ESP-IDF", _EVENT_LOOP, "esp_event_handler_instance_register", 2),
    Call("ESP-IDF", "the ESP-IDF Wi-Fi driver", "esp_wifi_init", Field("wifi_init_config_t::event_handler")),
    Call(
        "ESP-IDF", "the ESP-IDF HTTP client (esp_http_client_perform)", "esp_http_client_init",
        Field("esp_http_client_config_t::event_handler"),
    ),
    Call("ESP-IDF", "the SNTP client", "sntp_set_time_sync_notification_cb", 0),
    Call("ESP-IDF", "the ROM function ets_printf", "ets_install_putc1", 0),
    Call("ESP-IDF", "the ROM function ets_printf", "ets_install_putc2", 0),
    # ── lwIP: with its source, the slots give these edges (``pcb->recv``).
    Call("lwIP", _TCPIP, "tcpip_api_call", 0),
    Call("lwIP", _TCPIP, "tcpip_callback", 0),
    Call("lwIP", _TCPIP, "tcp_connect", 3),
    Call("lwIP", _TCPIP, "tcp_accept", 1),
    Call("lwIP", _TCPIP, "tcp_recv", 1),
    Call("lwIP", _TCPIP, "tcp_sent", 1),
    Call("lwIP", _TCPIP, "tcp_err", 1),
    Call("lwIP", _TCPIP, "tcp_poll", 1),
    Call("lwIP", _TCPIP, "udp_recv", 1),
    Call("lwIP", _TCPIP, "dns_gethostbyname", 2),
    # ── POSIX threads: the pthread of ESP-IDF has no source in the ESP32
    # Arduino core.  The routine runs in the thread of the first call.
    Call("POSIX", "the first call of pthread_once", "pthread_once", 1),
    # ── Nanostack: `arm_hal_nvm.h` declares the NVM of the platform, and a
    # build can have no driver of it.  Each operation calls its callback
    # when it ends.
    Call("Nanostack", _NVM, "platform_nvm_init", 0),
    Call("Nanostack", _NVM, "platform_nvm_finalize", 0),
    Call("Nanostack", _NVM, "platform_nvm_key_create", 0),
    Call("Nanostack", _NVM, "platform_nvm_key_delete", 0),
    Call("Nanostack", _NVM, "platform_nvm_write", 0),
    Call("Nanostack", _NVM, "platform_nvm_read", 0),
    Call("Nanostack", _NVM, "platform_nvm_flush", 0),
    # ── picolibc and avr-libc: the two have a `struct __file` with the
    # field `put`.  Each output function writes each character through the
    # `put` function of its FILE (`stream->put`), directly or through
    # `vfprintf` and `putc`.  The `_P` functions of avr-libc read the
    # format or the text from the program memory.  A build can link the
    # library with no source of it (Zephyr, an AVR core).  The slot is the
    # field, thus each of these functions gets an edge to each function
    # that the build stores in `put`.  `fdevopen` stores its parameter 0
    # there.
    *(
        Call(_STDIO_LIBC, _STDIO_WRITE, name, Field("__file::put"))
        for name in (
            "fputc", "putc", "putchar", "fputs", "puts", "fwrite", "printf", "fprintf", "vprintf", "vfprintf",
            "fputs_P", "puts_P", "printf_P", "fprintf_P", "vfprintf_P",
        )
    ),
    Flow(_STDIO_LIBC, _STDIO_WRITE, Param("fdevopen", 0), Field("__file::put")),
    # ── Zephyr: `arch_new_thread` writes the stack frame in C, as
    # integers, and assembly starts `z_thread_entry`, which calls the entry
    # in C.  Static threads and `k_thread_create` go through
    # `z_setup_new_thread` to it.
    Flow("Zephyr", _ZEPHYR, Param("arch_new_thread", 3), Param("z_thread_entry", 0), always=True),
    Flow("Zephyr", _ZEPHYR, Param("arch_new_thread", 4), Param("z_thread_entry", 1), always=True),
    Flow("Zephyr", _ZEPHYR, Param("arch_new_thread", 5), Param("z_thread_entry", 2), always=True),
    Flow("Zephyr", _ZEPHYR, Param("arch_new_thread", 6), Param("z_thread_entry", 3), always=True),
    Flow("Zephyr", _ZEPHYR, Param("arch_switch_to_main_thread", 2), Param("z_thread_entry", 0), always=True),
    # ── CMSIS: the RTX kernel up to 5.7 (CMSIS 5, the copy of Mbed) starts
    # a thread from its stack frame, and the NVIC calls the vector that the
    # table of integers holds.  From CMSIS-RTX 5.8, the frame starts the C
    # function `osThreadEntry`, which calls the function of the thread: the
    # rules then give the edge from the function of the rule, and
    # `osThreadEntry` has no caller (a Flow into `osThreadEntry` needs a
    # rule that only one version applies).
    Call("CMSIS-RTOS2", _RTX, "osThreadNew", 0, always=True),
    # The kernel creates its idle and timer threads with `svcRtxThreadNew`,
    # which writes the frame.  `osThreadNew` reaches it through an SVC in
    # assembly, thus no slot flows from the one to the other, and the rule
    # of `osThreadNew` keeps the threads of the application.
    Call("CMSIS-RTOS2", _RTX, "svcRtxThreadNew", 0, always=True),
    Call("CMSIS", _INTERRUPT, "NVIC_SetVector", 1, always=True),
    Call("CMSIS", _INTERRUPT, "__NVIC_SetVector", 1, always=True),
    # ── mbed OS: the EventQueue copies the callable object into the memory
    # of the event, and `function_call<F>` calls it through `void *`.  The
    # function is in the parameter F of each overload that takes one
    # (`call(F f)`, `call_in(ms, F f)`, `call_every(ms, F f)`); the other
    # overloads give their function to these.  A parameter of an overload
    # at the same index that holds an object pointer holds no function.
    Call("Mbed OS", _EVENT_QUEUE, _FUNCTION_CALL, Param("events::EventQueue::call", 0), always=True),
    Call("Mbed OS", _EVENT_QUEUE, _FUNCTION_CALL, Param("events::EventQueue::call_in", 1), always=True),
    Call("Mbed OS", _EVENT_QUEUE, _FUNCTION_CALL, Param("events::EventQueue::call_every", 1), always=True),
)


# ── the rules of a project ──────────────────────────────────────────────────


def _text(table: Mapping[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: '{key}' must be a text that is not empty")
    return value


def _index(value: object, label: str) -> int:
    # WHY the bool check: TOML `true` is an int in Python.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label}: 'param' must be an integer of 0 or more")
    return value


def _slot_name(value: object, label: str) -> SlotName:
    """Read ``{function = "f", param = 3}`` or ``{field = "record::f"}``."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a table: {{function = \"f\", param = 0}} or {{field = \"record::f\"}}")
    if set(value) == {"field"}:
        return Field(_text(value, "field", label))
    if set(value) == {"function", "param"}:
        return Param(_text(value, "function", label), _index(value["param"], label))
    raise ValueError(f"{label} must hold 'function' and 'param', or 'field' only")


def _rule(table: object, label: str) -> Rule:
    if not isinstance(table, Mapping):
        raise ValueError(f"{label} must be a table")
    sdk = _text(table, "sdk", label)
    entry = _text(table, "entry", label)
    keys = set(table) - {"sdk", "entry"}
    always = table.get("always", False)
    if not isinstance(always, bool):
        raise ValueError(f"{label}: 'always' must be true or false")
    if keys - {"always"} == {"from", "to"}:
        return Flow(
            sdk, entry, _slot_name(table["from"], f"{label} 'from'"), _slot_name(table["to"], f"{label} 'to'"),
            always=always,
        )
    if "function" in keys and keys <= {"function", "param", "field", "slot", "always"}:
        function = _text(table, "function", label)
        if len(keys & {"param", "field", "slot"}) != 1:
            raise ValueError(f"{label}: give one of 'param', 'field' and 'slot'")
        slot: int | Param | Field
        if "param" in table:
            slot = _index(table["param"], label)
        elif "field" in table:
            slot = Field(_text(table, "field", label))
        else:
            # WHY a parameter only: `field` already names a field, and one
            # fact must have one key.
            value = table["slot"]
            if not isinstance(value, Mapping) or set(value) != {"function", "param"}:
                raise ValueError(
                    f"{label} 'slot' must be a table {{function = \"f\", param = 0}}: a field is the key 'field'",
                )
            slot = Param(_text(value, "function", f"{label} 'slot'"), _index(value["param"], f"{label} 'slot'"))
        return Call(sdk, entry, function, slot, always=always)
    raise ValueError(
        f"{label}: a rule holds 'sdk', 'entry' and either 'from' and 'to' (a flow), or 'function' with "
        "one of 'param', 'field' and 'slot' (a call), and an optional 'always'",
    )


def rules_from_config(value: object) -> tuple[list[Rule], list[tuple[int | None, str]]]:
    """Read the array ``[[call_graph.sdk]]``: the rules, and a message for each table that is not a rule.

    Each message comes with the position of its table in the array, or
    None when the value is no array.  WHY the messages and not an
    exception: a wrong rule loses only its own edges, thus the other rules
    apply and the run continues (``config_value_problems`` gives the same
    messages as warnings, and ``doctor --fix`` removes the table at the
    position).
    """
    if not isinstance(value, list):
        return [], [(None, "[[call_graph.sdk]] must be an array of tables")]
    rules: list[Rule] = []
    problems: list[tuple[int | None, str]] = []
    for position, table in enumerate(value):
        try:
            rules.append(_rule(table, f"[[call_graph.sdk]] number {position + 1}"))
        except ValueError as error:
            problems.append((position, str(error)))
    return rules, problems


def project_rules(project_root: Path | None) -> list[Rule]:
    """Give the built-in rules and the rules of ``[[call_graph.sdk]]`` in the ``config.toml`` of the project.

    WHY ``config.toml`` only: a rule tells how an SDK of the project runs
    its code, thus it is a fact of the project for each person, as the
    documentation of ``[call_graph]`` says.  A file that cannot be read,
    and a table that is not a rule, give a warning: the built-in rules
    and the other rules still apply.
    """
    rules: list[Rule] = list(BUILTIN_RULES)
    if project_root is None:
        return rules
    config_path = project_root / ".fw-context" / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        log.warning("The SDK rules of %s are not used: %s", config_path, error)
        return rules
    graph = data.get("call_graph")
    if not isinstance(graph, dict) or "sdk" not in graph:
        return rules
    own, problems = rules_from_config(graph["sdk"])
    for _position, problem in problems:
        log.warning("%s: %s", config_path, problem)
    return rules + own


# ── the links of a build ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Symbol:
    usr: str
    is_definition: bool
    file: str
    line: int
    col: int


def _symbols(conn: sqlite3.Connection, config_hash: str, names: set[str], kinds: tuple[str, ...]) -> dict[str, list[_Symbol]]:
    found: dict[str, list[_Symbol]] = {}
    if not names:
        return found
    marks = ",".join("?" * len(names))
    kind_marks = ",".join("?" * len(kinds))
    for row in conn.execute(
        f"SELECT qualified_name, usr, is_definition, file_path, line, col FROM symbols"
        f" WHERE config_hash = ? AND kind IN ({kind_marks}) AND qualified_name IN ({marks})"
        " ORDER BY usr",
        (config_hash, *kinds, *sorted(names)),
    ):
        found.setdefault(row[0], []).append(_Symbol(row[1], bool(row[2]), row[3], row[4], row[5]))
    return found


def write_links(conn: sqlite3.Connection, config_hash: str, rules: Iterable[Rule]) -> int:
    """Replace the links of the build (table ``sdk_links``): each rule for each symbol of its names.

    A ``flow`` row gives the source slot and the target slot.  A ``call``
    row gives the slot, the function of the rule (*target*) and the place
    of its declaration, where the edge stands: the SDK runs the function
    from there, and the build has no line that the slot facts see.  Gives
    the count of the rows.
    """
    rules = list(rules)
    function_names = {rule.function for rule in rules if isinstance(rule, Call)}
    field_names: set[str] = set()
    for rule in rules:
        names = (rule.source, rule.target) if isinstance(rule, Flow) else (rule.slot,)
        for name in names:
            if isinstance(name, Param):
                function_names.add(name.function)
            elif isinstance(name, Field):
                field_names.add(name.name)
    functions = _symbols(conn, config_hash, function_names, _FUNCTION_KINDS)
    fields = _symbols(conn, config_hash, field_names, ("field",))

    def slots(name: SlotName, with_body: bool = True) -> list[str]:
        """The slots of *name*; with *with_body* False, no parameter of a function that the build defines."""
        if isinstance(name, Field):
            return [f"F|{symbol.usr}" for symbol in fields.get(name.name, [])]
        return [
            f"P|{symbol.usr}|{name.index}" for symbol in functions.get(name.function, [])
            if with_body or not symbol.is_definition
        ]

    rows: set[tuple] = set()
    for rule in rules:
        if isinstance(rule, Flow):
            for source in slots(rule.source, with_body=rule.always):
                for target in slots(rule.target):
                    rows.add((config_hash, "flow", source, target, rule.entry, rule.sdk, "", 0, 0))
            continue
        for symbol in functions.get(rule.function, []):
            if symbol.is_definition and not rule.always:
                continue
            targets = [f"P|{symbol.usr}|{rule.slot}"] if isinstance(rule.slot, int) else slots(rule.slot)
            for slot in targets:
                rows.add((
                    config_hash, "call", slot, symbol.usr, rule.entry, rule.sdk, symbol.file, symbol.line, symbol.col,
                ))
    conn.execute("DELETE FROM sdk_links WHERE config_hash = ?", (config_hash,))
    conn.executemany(
        "INSERT INTO sdk_links (config_hash, kind, slot, target, entry, sdk, file, line, col)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        sorted(rows),
    )
    return len(rows)
