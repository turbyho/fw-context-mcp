"""The slot facts of the interrupt registrations that a Zephyr build records.

WHY this module exists: ``IRQ_CONNECT`` writes one ``struct _isr_list`` into
the ``.intList`` section, and ``gen_isr_tables.py`` makes the software table
``_sw_isr_table`` from the linked ``zephyr_pre0.elf``.  The generated C holds
an ADDRESS in each slot in use (``{(const void *)0x2e293, (ISR)0x2e1cb}``),
thus no slot fact of the parse names the handler.  The dispatcher
``_isr_wrapper`` calls ``entry->isr(entry->arg)``, and its site had only the
spurious stub as a target.  Measured on an nRF52840 application: 6
handlers, and 2 functions in the argument of ``nrfx_isr``, had no caller.
The slot fact of ``IRQ_CONNECT`` in the source is not enough: a macro of the
device tree hid 3 of the 6 registrations from the parse.

The registrations of the artifact (``intlist.read_isr_registrations``) are
the input of the generator, thus they give the table by name.  For each
registration that is not direct, this writes two slot facts:

* the handler goes into the field ``_isr_table_entry::isr``, which the site
  of the dispatcher reads;
* a function in the argument goes into parameter 0 of the handler, because
  the dispatcher gives the argument to the handler (``nrfx_isr`` calls its
  parameter 0).

A direct registration (``ISR_FLAG_DIRECT``) goes into the hardware vector
table, and no table entry calls it.

The slot resolution then gives the edges from the real sites (origin
``SLOT``).  The rows have the unit ``BUILD_UNIT`` and the path of the
artifact, thus a reader sees where a target came from.  Each index run
writes them again (``_postprocess._step_resolve_slots``).

Limits:

* Without the linked artifact (no build, a build that stopped before the
  link), the facts are not there, as before.
* An interrupt that the code connects at run time (``irq_connect_dynamic``)
  is not in the section.
* The artifact is the one of the last build.  The index run builds first,
  thus the two agree unless that build failed.
* A `static` handler of C++ has a mangled name in the artifact, thus no
  definition of the index matches it.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .compile_commands import compile_directory
from .intlist import ISR_FLAG_DIRECT, ElfSymbol, read_isr_registrations

log = logging.getLogger(__name__)

# The unit of the rows that this module writes.  A unit of the parse is ""
# or the path of a unit, thus this value cannot collide with one.
BUILD_UNIT = "build:.intList"

# The field that the dispatcher of Zephyr calls (`struct _isr_table_entry`
# of `sw_isr_table.h`).
_TABLE_FIELD = "_isr_table_entry::isr"

_FUNCTION_KINDS = ("function", "method")


def build_directory(conn: sqlite3.Connection, config_hash: str) -> Path | None:
    """Return the build tree of the build *config_hash*, or None.

    The tree is what holds the artifacts a build records, and it is NOT
    the directory of compile_commands.json.  fw-context generates its own
    copy of that file under `.fw-context/build/`, so the parent of the
    path in the database is a directory with no artifact in it.  The
    build itself names the tree, in the `directory` field of every entry,
    which `compile_directory` reads.

    Measured on the Zephyr project before this read the field: every one
    of the nine images answered "no registrations", because
    `read_isr_registrations` looked for `zephyr/zephyr_pre0.elf` under
    `.fw-context/build`.  The artifact was there all along, two
    directories away, with its `.intList` section intact.

    The parent is still the answer when the file names no directory.  A
    build system that writes compile_commands.json into its own tree —
    which is where CMake puts it — is right either way.
    """
    found = conn.execute(
        "SELECT compile_commands_path FROM build_configs WHERE config_hash = ? LIMIT 1",
        (config_hash,),
    ).fetchone()
    if found is None or not found[0]:
        return None
    path = Path(str(found[0]))
    return compile_directory(path) or path.parent


def write_isr_slot_facts(conn: sqlite3.Connection, config_hash: str, project_root: Path | None) -> int:
    """Write the slot facts of the interrupt registrations of the build *config_hash*; return the count.

    The rows of the last run go first: a registration can leave the build.
    """
    from .ops import _normalize_file_path

    conn.execute("DELETE FROM slot_sources WHERE config_hash = ? AND unit = ?", (config_hash, BUILD_UNIT))
    tree = build_directory(conn, config_hash)
    table = read_isr_registrations(tree) if tree is not None else None
    if table is None or not table.registrations:
        return 0
    field = _unique(conn.execute(
        "SELECT usr FROM symbols WHERE config_hash = ? AND kind = 'field' AND qualified_name = ?",
        (config_hash, _TABLE_FIELD),
    ).fetchall())
    if field is None:
        return 0
    file = _normalize_file_path(table.artifact, project_root) if project_root is not None else table.artifact
    facts: set[tuple[str, str]] = set()
    for registration in table.registrations:
        if registration.flags & ISR_FLAG_DIRECT or registration.handler_symbol is None:
            continue
        handler = _definition(conn, config_hash, registration.handler_symbol)
        if handler is None:
            continue
        facts.add(("F|" + field, handler))
        argument = registration.argument_symbol
        if argument is not None and argument.function:
            target = _definition(conn, config_hash, argument)
            if target is not None:
                facts.add((f"P|{handler}|0", target))
    conn.executemany(
        "INSERT INTO slot_sources (config_hash, file, line, col, function, function_type, slot, unit)"
        " VALUES (?, ?, 0, 0, ?, '', ?, ?)",
        [(config_hash, file, function, slot, BUILD_UNIT) for slot, function in sorted(facts)],
    )
    log.debug("Interrupt registrations of the build: %d slot fact(s) from %s", len(facts), table.artifact)
    return len(facts)


def _definition(conn: sqlite3.Connection, config_hash: str, symbol: ElfSymbol) -> str | None:
    """The USR of the function definition that the symbol *symbol* of the artifact names, or None.

    A global symbol is the link name of a definition: the linker made it
    unique.  The index can hold a weak and a strong definition of one link
    name (`.weak` in assembly, the handler in C), and the linker keeps the
    strong one, thus the one strong definition wins.

    A local symbol is unique only in its object file, thus its definition
    must have the name and the file of the symbol.  The file is the base
    name only.  That is enough: libclang gives a `static` function of C the
    USR of the base name of its file (`c:drv.c@F@isr`), thus the index holds
    one symbol for two such functions in files of one base name.

    No match, or more than one, gives None: a wrong target is worse than
    none.  A `static` function of a header has the file of its unit in the
    artifact, and the header in the index, thus it gets None too.
    """
    kinds = ",".join("?" * len(_FUNCTION_KINDS))
    if not symbol.local:
        rows = conn.execute(
            f"SELECT usr, is_weak FROM symbols WHERE config_hash = ? AND link_name = ? AND is_definition = 1"
            f" AND kind IN ({kinds})",
            (config_hash, symbol.name, *_FUNCTION_KINDS),
        ).fetchall()
        strong = [row for row in rows if not row[1]]
        return _unique(strong if len(rows) > 1 else rows)
    if not symbol.file:
        return None
    rows = conn.execute(
        f"SELECT usr, file_path FROM symbols WHERE config_hash = ? AND name = ? AND is_definition = 1"
        f" AND kind IN ({kinds})",
        (config_hash, symbol.name, *_FUNCTION_KINDS),
    ).fetchall()
    file = Path(symbol.file).name
    return _unique([row for row in rows if Path(row[1]).name == file])


def _unique(rows: list) -> str | None:
    """The first column of the one row of *rows*, or None for no row or for more than one."""
    return str(rows[0][0]) if len(rows) == 1 else None
