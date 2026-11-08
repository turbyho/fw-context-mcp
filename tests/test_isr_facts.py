"""The interrupt registrations of the build artifact give the dispatcher of Zephyr its targets (``indexer._isr_facts``).

The generated ``_sw_isr_table`` holds addresses, thus the parse gives the
site ``entry->isr(entry->arg)`` of ``_isr_wrapper`` no handler.  The
``.intList`` section of ``zephyr/zephyr_pre0.elf`` names each one.  The
ELF here is built by the test (``tests.test_intlist._elf``).
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

import pytest

from tests._toml import toml_string
from tests.test_intlist import _elf

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

# The symbol types and bindings of ELF, in the `st_info` byte.
_LOCAL_FUNC, _GLOBAL_FUNC, _GLOBAL_OBJECT, _FILE = 0x02, 0x12, 0x11, 0x04

SOURCES = {
    "table.h": (
        "#pragma once\n"
        "struct _isr_table_entry { const void *arg; void (*isr)(const void *); };\n"
        "extern const struct _isr_table_entry _sw_isr_table[4];\n"
    ),
    "isr_wrapper.c": (
        '#include "table.h"\n'
        "void _isr_wrapper(int irq) {\n"
        "    const struct _isr_table_entry *entry = &_sw_isr_table[irq];\n"
        "    entry->isr(entry->arg);\n"
        "}\n"
    ),
    "glue.c": "void shim(const void *handler) { ((void (*)(void))handler)(); }\n",
    "drv.c": (
        "void clock_handler(void) {}\n"
        "static void local_isr(const void *arg) { (void)arg; }\n"
        "void direct_isr(const void *arg) { (void)arg; }\n"
        "void strong_isr(const void *arg) { (void)arg; }\n"
        "void take(void (*f)(const void *)) { (void)f; }\n"
        "void use(void) { take(local_isr); }\n"
    ),
    "other.c": (
        "static void local_isr(const void *arg) { (void)arg; }\n"
        "void take(void (*f)(const void *));\n"
        "void use_other(void) { take(local_isr); }\n"
    ),
    # A weak definition of the strong handler of drv.c, as a startup file has.
    "weak.S": ".weak strong_isr\nstrong_isr:\n  nop\n",
}


def _intlist(entries: list[tuple[int, int, int, int]]) -> bytes:
    """A header and (irq, flags, func, param) entries, as Zephyr writes them."""
    raw = struct.pack("<II", 4, 0)
    for irq, flags, func, param in entries:
        raw += struct.pack("<iiII", irq, flags, func, param)
    return raw


SYMBOLS = [
    ("glue.c", 0, _FILE),
    ("drv.c", 0, _FILE),
    ("local_isr", 0x301, _LOCAL_FUNC),
    ("shim", 0x101, _GLOBAL_FUNC),
    ("clock_handler", 0x201, _GLOBAL_FUNC),
    ("direct_isr", 0x501, _GLOBAL_FUNC),
    ("device_0", 0x401, _GLOBAL_OBJECT),
    ("strong_isr", 0x601, _GLOBAL_FUNC),
]
REGISTRATIONS = [
    (0, 0, 0x101, 0x201),   # shim, with clock_handler as its argument
    (1, 0, 0x301, 0x401),   # the static local_isr of drv.c, with a device
    (2, 1, 0x501, 0),       # direct_isr: ISR_FLAG_DIRECT, in the vector table
    (3, 0, 0x601, 0x201),   # strong_isr, which does not call its argument
]


def _project(root: Path, registrations=REGISTRATIONS, symbols=SYMBOLS) -> None:
    (root / ".fw-context").mkdir(parents=True, exist_ok=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-isr"\n\n[build]\n\n[index]\ndb_dir = {toml_string(root.parent / "db")}\n', encoding="utf-8",
    )
    for name, text in SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": name, "arguments": ["cc", "-std=gnu11", "-c", name]}
        for name in SOURCES if name.endswith((".c", ".S"))
    ]), encoding="utf-8")
    (root / "zephyr").mkdir(exist_ok=True)
    (root / "zephyr" / "zephyr_pre0.elf").write_bytes(_elf(_intlist(registrations), symbols))


def _db(root: Path) -> Path:
    return root.parent / "db" / "proj-isr" / "index.db"


def _run(root: Path) -> None:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    _db(root).parent.mkdir(parents=True, exist_ok=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=_db(root), project_root=root,
        project_id="proj-isr", project_name="proj-isr", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )


def _calls(root: Path) -> set[tuple[str, str, str]]:
    """(caller, callee, file of the callee) of each call edge."""
    conn = sqlite3.connect(_db(root))
    try:
        return set(conn.execute(
            "SELECT a.name, b.name, b.file_path FROM refs r"
            " JOIN symbols a ON a.usr = r.from_usr AND a.config_hash = r.config_hash"
            " JOIN symbols b ON b.usr = r.to_usr AND b.config_hash = r.config_hash"
            " WHERE r.ref_kind = 'call'"
        ).fetchall())
    finally:
        conn.close()


def _facts(root: Path) -> list[tuple[str, str, str]]:
    """(function, slot, file) of each fact of the build; a slot is `isr` or `P:<handler>|0`."""
    from fw_context_mcp.indexer._isr_facts import BUILD_UNIT

    conn = sqlite3.connect(_db(root))
    try:
        names = dict(conn.execute("SELECT usr, name FROM symbols"))
        rows = conn.execute(
            "SELECT s.name, f.slot, f.file FROM slot_sources f JOIN symbols s ON s.usr = f.function"
            " WHERE f.unit = ? ORDER BY s.name, f.slot",
            (BUILD_UNIT,),
        ).fetchall()
    finally:
        conn.close()

    def short(slot: str) -> str:
        if slot.startswith("F|"):
            return slot.rsplit("@", 1)[-1]
        code, _, index = slot[2:].rpartition("|")
        return f"P:{names.get(code, code)}|{index}"

    return sorted((name, short(slot), file) for name, slot, file in rows)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def test_the_dispatcher_calls_each_registered_handler(root):
    _project(root)
    _run(root)
    calls = _calls(root)
    assert ("_isr_wrapper", "shim", "glue.c") in calls
    assert ("_isr_wrapper", "local_isr", "drv.c") in calls, "the static handler of drv.c, by its file symbol"
    assert ("_isr_wrapper", "local_isr", "other.c") not in calls
    assert ("_isr_wrapper", "direct_isr", "drv.c") not in calls, "a direct interrupt is in the vector table"
    assert ("_isr_wrapper", "strong_isr", "drv.c") in calls, "the strong definition, not the weak one"


def test_a_handler_that_does_not_call_its_parameter_gives_its_argument_no_caller(root):
    """`strong_isr` gets `clock_handler` in parameter 0 and ignores it: a flow fact, not a call."""
    _project(root)
    _run(root)
    assert not {call for call in _calls(root) if call[:2] == ("strong_isr", "clock_handler")}


def test_a_function_in_the_argument_goes_into_parameter_0_of_its_handler(root):
    _project(root)
    _run(root)
    assert ("shim", "clock_handler", "drv.c") in _calls(root)


def test_the_facts_name_the_artifact_and_a_run_writes_them_again(root):
    _project(root)
    _run(root)
    first = _facts(root)
    elf = "zephyr/zephyr_pre0.elf"
    assert first == [
        ("clock_handler", "P:shim|0", elf),
        ("clock_handler", "P:strong_isr|0", elf),
        ("local_isr", "isr", elf),
        ("shim", "isr", elf),
        ("strong_isr", "isr", elf),
    ]
    _run(root)
    assert _facts(root) == first


def test_a_registration_that_leaves_the_build_leaves_the_index(root):
    _project(root)
    _run(root)
    _project(root, registrations=REGISTRATIONS[1:])
    (root / "glue.c").write_text(SOURCES["glue.c"] + "\n", encoding="utf-8")
    _run(root)
    calls = _calls(root)
    assert ("_isr_wrapper", "shim", "glue.c") not in calls
    assert ("shim", "clock_handler", "drv.c") not in calls
    assert ("_isr_wrapper", "local_isr", "drv.c") in calls


def test_the_file_symbol_picks_the_static_handler(root):
    """The artifact puts `local_isr` after the file symbol of `other.c`: that one is the handler."""
    _project(root, symbols=[("other.c", 0, _FILE) if s == ("drv.c", 0, _FILE) else s for s in SYMBOLS])
    _run(root)
    calls = _calls(root)
    assert ("_isr_wrapper", "local_isr", "other.c") in calls
    assert ("_isr_wrapper", "local_isr", "drv.c") not in calls


def test_two_static_handlers_in_files_of_one_base_name_give_no_target(root):
    """`drv.c` and `sub/drv.c` both define `static local_isr`: the file symbol `drv.c` names neither one."""
    _project(root)
    (root / "sub").mkdir()
    (root / "sub" / "drv.c").write_text(
        "static void local_isr(const void *arg) { (void)arg; }\n"
        "void take(void (*f)(const void *));\n"
        "void use_sub(void) { take(local_isr); }\n",
        encoding="utf-8",
    )
    entries = json.loads((root / "compile_commands.json").read_text(encoding="utf-8"))
    entries.append({"directory": str(root), "file": "sub/drv.c", "arguments": ["cc", "-std=gnu11", "-c", "sub/drv.c"]})
    (root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    _run(root)
    assert not {call for call in _calls(root) if call[:2] == ("_isr_wrapper", "local_isr")}


def test_a_local_symbol_with_no_file_symbol_gives_no_target(root):
    """Two `static local_isr`: without the file symbol of the artifact, no definition is the one."""
    _project(root, symbols=[s for s in SYMBOLS if s[2] != _FILE])
    _run(root)
    assert not {call for call in _calls(root) if call[:2] == ("_isr_wrapper", "local_isr")}


def test_a_fact_of_the_build_has_no_type(root):
    """The artifact names a function, not its type: no such fact goes to the guesses by type."""
    from fw_context_mcp.indexer._slot_resolution import _load
    from fw_context_mcp.indexer.db import open_db

    _project(root)
    _run(root)
    conn = open_db(_db(root))
    try:
        config_hash = conn.execute("SELECT config_hash FROM symbols LIMIT 1").fetchone()[0]
        _slots, by_type, _sites, _arguments, _calls_of_rules = _load(conn, config_hash)
    finally:
        conn.close()
    assert "" not in by_type


def test_find_indirect_targets_names_the_registration_of_the_build(root):
    from fw_context_mcp.mcp.handlers.callgraph import find_indirect_targets

    _project(root)
    _run(root)
    rows = find_indirect_targets("clock_handler", project_root=str(root))
    stored = [row for row in rows if row.get("relation") == "stored" and row["file"].endswith("zephyr_pre0.elf")]
    assert stored
    assert {row["context"] for row in stored} == {"<interrupt registration of the build>"}
    assert {row["line"] for row in stored} == {0}


def test_no_artifact_writes_no_fact(root):
    _project(root)
    (root / "zephyr" / "zephyr_pre0.elf").unlink()
    _run(root)
    assert _facts(root) == []
