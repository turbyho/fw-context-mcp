"""One USR for a function with internal linkage that C units and C++ units of a build both compile.

WHY this module exists: clang gives a `static` function more than one USR.
A C unit gives `c:hdr.h@F@f`, and a C++ unit gives `c:hdr.h@F@f#I#` with
the signature, also inside `extern "C"`.  The file in the USR is the file
of the first declaration that the unit reads, thus a prototype in an other
header gives an other USR in the same language.  A header with `static
inline` functions (CMSIS, the low-layer drivers of a HAL, the FreeRTOS of
ESP-IDF) has its symbols from the first unit that walks it; the other
units skip its body.  A call from such a unit then named a USR that is no
symbol, and ``find_callers`` did not see it.  Measured: an Mbed project had
1195 such calls (1123 from C to the C++ symbol, 72 back), an STM32 project
305, and an ESP32 project lost the flow of two tasks through the inline
`xTaskCreate` of ESP-IDF.

The match is a fact of the compiler: each unit records, for each such
function that it names, the place of the definition and the name
(``symbols._note_internal_function``, table ``internal_decls``).  A USR
that is no symbol of the build goes to the symbol of a function with the
same name, defined at the same place (file, line, column).  WHY not the
text of the USR: C++ can overload a `static` function, thus the part
before the signature can name more than one symbol (measured: 13 such
names in the Mbed project).

The step runs at each index run and at each ``reindex_file``, before the
resolution of the slots, because each parse writes the USR of its own
language again.  It rewrites the references (``refs.to_usr``) and the
slot facts (the keys ``P|<usr>|<n>`` and ``R|<usr>``, and the columns that
name a function).  For each unit that recorded an alias, it records the
USR of the symbol too: the rows of the unit now hold that USR, and an
other unit can own the header later and give the symbol the USR of its
own language.  Without that record, an unchanged unit kept a USR that no
unit recorded, and an incremental run gave an other graph than a full run.

Limits:

* A macro can define more overloads in C++ than in C at one place
  (`ov(int)` in C, `ov(int)` and `ov(double)` in C++).  When a C unit owns
  the header and the C++ code calls only `ov(double)`, its one alias goes
  to `ov(int)` of C: no unit records the other overload, thus the place
  and the signature cannot show the conflict.
* A rename stays in the rows of an unchanged unit.  When a later run adds
  the evidence that stops a match (a second signature or a second place
  of the alias), those rows keep the USR of the symbol until their unit is
  parsed again; a full run leaves them on the alias.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict

__all__ = ["unify_internal_functions"]

log = logging.getLogger(__name__)

# (table, column, kind): "key" holds a slot key, "function" a USR, and
# "value" a slot key or a USR (slot_arguments.value, by value_kind).
_SLOT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("slot_sources", "function", "function"),
    ("slot_sources", "slot", "key"),
    ("slot_flows", "source", "key"),
    ("slot_flows", "target", "key"),
    ("slot_sites", "slot", "key"),
    ("slot_sites", "caller", "function"),
    ("slot_sites", "callee", "function"),
    ("slot_arguments", "value", "value"),
    ("slot_escapes", "slot", "key"),
)


def _signature(usr: str) -> str | None:
    """The name and the signature of a C++ USR (``f#I#``), or None for a USR of C, which has no signature."""
    _file, separator, rest = usr.partition("@F@")
    return rest if separator and "#" in rest else None


def _aliases(conn: sqlite3.Connection, config_hash: str) -> dict[str, str]:
    """Map each recorded USR that is no symbol to the one function symbol with its name at its place.

    No match:

    * An alias recorded at two places: the USR names the file by its base
      name only, thus `dirA/util.h` and `dirB/util.h` give one USR to two
      functions.
    * An alias with two symbols of its name at its place: the place does
      not tell them apart (a macro that defines two overloads).
    * A symbol whose C++ USRs (its own one and its aliases) hold two
      signatures: two C++ overloads (`f(int)`, `f(double)`) that a macro
      defines at the place where it defines `f(int)` in C.  The place
      cannot tell which one the function of C is.  USRs of C and USRs of
      other files of one signature are one function.
    """
    places: defaultdict[str, set[tuple[str, int, int]]] = defaultdict(set)
    candidates: defaultdict[str, set[str]] = defaultdict(set)
    for alias, decl_file, line, col, symbol in conn.execute(
        """SELECT d.usr, d.decl_file, d.line, d.col, s.usr FROM internal_decls d
             LEFT JOIN symbols s ON s.config_hash = d.config_hash AND s.file_path = d.decl_file
                                AND s.line = d.line AND s.col = d.col AND s.name = d.name AND s.kind = 'function'
            WHERE d.config_hash = ?
              AND NOT EXISTS (SELECT 1 FROM symbols x WHERE x.config_hash = d.config_hash AND x.usr = d.usr)""",
        (config_hash,),
    ):
        places[alias].add((decl_file, line, col))
        if symbol is not None:
            candidates[alias].add(symbol)
    single = {
        alias: next(iter(symbols)) for alias, symbols in candidates.items()
        if len(symbols) == 1 and len(places[alias]) == 1
    }
    signatures: defaultdict[str, set[str]] = defaultdict(set)
    for alias, symbol in single.items():
        for usr in (alias, symbol):
            signature = _signature(usr)
            if signature is not None:
                signatures[symbol].add(signature)
    return {alias: symbol for alias, symbol in single.items() if len(signatures[symbol]) <= 1}


def _rename_refs(conn: sqlite3.Connection, config_hash: str, aliases: dict[str, str]) -> int:
    """Move each reference to an alias onto its symbol; give the count of the rows.

    A site that already has a row to the symbol keeps that row: it gets the
    origins and the producing units of the row of the alias, which goes.
    """
    moved = 0
    for alias, usr in sorted(aliases.items()):
        rows = conn.execute(
            "SELECT id, from_file, from_line, from_col, ref_kind, slot_index, from_usr, origins FROM refs"
            " WHERE config_hash = ? AND to_usr = ?",
            (config_hash, alias),
        ).fetchall()
        for row in rows:
            twin = conn.execute(
                "SELECT id FROM refs WHERE config_hash = ? AND to_usr = ? AND from_file = ? AND from_line = ?"
                " AND from_col = ? AND ref_kind = ? AND ifnull(slot_index, -1) = ifnull(?, -1)"
                " AND ifnull(from_usr, '') = ifnull(?, '')",
                (config_hash, usr, row[1], row[2], row[3], row[4], row[5], row[6]),
            ).fetchone()
            if twin is None:
                conn.execute("UPDATE refs SET to_usr = ? WHERE id = ?", (usr, row[0]))
            else:
                conn.execute("UPDATE refs SET origins = origins | ? WHERE id = ?", (row[7], twin[0]))
                conn.execute(
                    "INSERT OR IGNORE INTO ref_producers (ref_id, config_hash, unit_path, unit_flags_hash)"
                    " SELECT ?, config_hash, unit_path, unit_flags_hash FROM ref_producers WHERE ref_id = ?",
                    (twin[0], row[0]),
                )
                conn.execute("DELETE FROM ref_producers WHERE ref_id = ?", (row[0],))
                conn.execute("DELETE FROM refs WHERE id = ?", (row[0],))
            moved += 1
    return moved


def _rename_slot_facts(conn: sqlite3.Connection, config_hash: str, aliases: dict[str, str]) -> int:
    """Rename the USR of each alias in the slot facts; give the count of the changed values.

    A column that holds a USR takes all aliases in one statement, through
    a temporary table: `slot_sites.callee` has no index, and a statement
    for each alias read the whole table again.  A key ``P|<alias>|<n>`` is
    a range of text for each alias (``|`` is followed by ``}`` in ASCII),
    and ``R|<alias>`` is equal to the value; both use the index of the
    column of a key.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS internal_aliases (alias TEXT PRIMARY KEY, usr TEXT NOT NULL)")
    conn.execute("DELETE FROM internal_aliases")
    conn.executemany("INSERT INTO internal_aliases (alias, usr) VALUES (?, ?)", sorted(aliases.items()))
    changed = 0
    try:
        for table, column, kind in _SLOT_COLUMNS:
            if kind in ("function", "value"):
                functions = " AND value_kind = 'function'" if kind == "value" else ""
                changed += conn.execute(
                    f"UPDATE {table} SET {column} = (SELECT a.usr FROM internal_aliases a WHERE a.alias = {column})"
                    f" WHERE config_hash = ? AND {column} IN (SELECT alias FROM internal_aliases){functions}",
                    (config_hash,),
                ).rowcount
            if kind not in ("key", "value"):
                continue
            keys = " AND value_kind = 'slot'" if kind == "value" else ""
            for alias, usr in sorted(aliases.items()):
                low, high = f"P|{alias}|", f"P|{alias}}}"
                changed += conn.execute(
                    f"UPDATE {table} SET {column} = ? || substr({column}, ?)"
                    f" WHERE config_hash = ? AND {column} >= ? AND {column} < ?{keys}",
                    (f"P|{usr}|", len(low) + 1, config_hash, low, high),
                ).rowcount
                changed += conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE config_hash = ? AND {column} = ?{keys}",
                    (f"R|{usr}", config_hash, f"R|{alias}"),
                ).rowcount
    finally:
        conn.execute("DELETE FROM internal_aliases")
    return changed


def _record_symbols(conn: sqlite3.Connection, config_hash: str, aliases: dict[str, str]) -> None:
    """For each unit that recorded an alias, record the USR of the symbol too, at the same place.

    The rows of the unit hold that USR now.  When an other unit owns the
    header later, the symbol can get the USR of an other language, and
    this record then maps the rows of the unchanged unit to it.
    """
    for alias, usr in sorted(aliases.items()):
        conn.execute(
            """INSERT INTO internal_decls (config_hash, usr, name, decl_file, line, col, from_file)
               SELECT DISTINCT d.config_hash, ?, d.name, d.decl_file, d.line, d.col, d.from_file
                 FROM internal_decls d
                WHERE d.config_hash = ? AND d.usr = ?
                  AND NOT EXISTS (SELECT 1 FROM internal_decls e
                                   WHERE e.config_hash = d.config_hash AND e.usr = ? AND e.from_file = d.from_file)""",
            (usr, config_hash, alias, usr),
        )


def unify_internal_functions(conn: sqlite3.Connection, config_hash: str) -> dict[str, int]:
    """Give each recorded USR of a function with internal linkage the USR of its symbol, in the build *config_hash*.

    Gives the counts for the log: the aliases, the moved references and
    the changed values of the slot facts.  The caller holds the
    transaction.
    """
    aliases = _aliases(conn, config_hash)
    if not aliases:
        return {"aliases": 0, "refs": 0, "slot_values": 0}
    counts = {
        "aliases": len(aliases),
        "refs": _rename_refs(conn, config_hash, aliases),
        "slot_values": _rename_slot_facts(conn, config_hash, aliases),
    }
    _record_symbols(conn, config_hash, aliases)
    return counts
