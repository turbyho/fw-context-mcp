"""The indirect calls of a build, read from the slot facts and their resolution.

The walk records the slot facts (``indexer/_slots.py``: tables
``slot_sources``, ``slot_flows``, ``slot_sites``, ``slot_arguments``), and
the step ``slot_edges`` resolves them (``indexer/_slot_resolution.py``):
it writes the call edges (``refs`` with the origins ``SLOT``,
``TYPE_ONLY`` and ``SDK``), the targets of each slot (``slot_targets``) and
the slots that escape (``slot_escaped``).  The tools
``find_indirect_call_sites``, ``find_indirect_targets``, ``find_callers``
and ``get_symbol_context`` read them here.  The rules of the SDK model
that apply to the build are in ``sdk_links`` (``indexer/_sdk_model.py``):
an edge of a rule gets its ``entry`` (``sdk_entry``).

A symbol is a slot when it is a field (``F|<USR>``) or a variable
(``V|<USR>``).  A parameter is no symbol of the index: a call through it
shows with the function that makes it.  No query matches a name: the tool
gives the USR of each symbol that the name resolves to.
"""

from __future__ import annotations

import sqlite3

from ..models import RefOrigin
from ._schema import _table_exists

__all__ = [
    "CODE_KINDS",
    "POINTER_KINDS",
    "count_function_stores",
    "count_indirect_sites",
    "count_slot_targets",
    "describe_slot",
    "function_calls_by_slot",
    "function_stores",
    "indirect_sites",
    "sdk_entry",
    "site_targets",
    "slot_escapes",
    "slot_key",
    "slot_targets",
    "stored_places",
]

# The kinds of a symbol that holds a function: its slot is the symbol.
POINTER_KINDS: tuple[str, ...] = ("field", "variable", "varglobal", "varlocal")
# The kinds of a symbol that makes calls and that a slot can hold.
CODE_KINDS: tuple[str, ...] = ("function", "method", "constructor", "destructor", "lambda")
_RESOLVED = int(RefOrigin.SLOT | RefOrigin.TYPE_ONLY | RefOrigin.SDK)
# The slots that the walk back through `via` visits at most, for one
# function of one slot (`stored_places`).  Each way back ends at a source;
# the limit bounds the work of one row of an answer, and an answer that it
# cut says so.
_VIA_LIMIT = 256


def slot_key(usr: str, kind: str) -> str | None:
    """The slot of a symbol that holds a function, or None for an other kind."""
    if kind == "field":
        return "F|" + usr
    if kind in POINTER_KINDS:
        return "V|" + usr
    return None


def _reached_slots_sql() -> str:
    """A CTE `reached(slot)`: the slot of the parameter and each slot that its value flows into.

    WHY the flows too: `cb_t f = dev->on_rx; f(1);` calls the value of the
    field through a local copy.
    """
    return (
        "WITH RECURSIVE reached(slot) AS ("
        " SELECT ? UNION"
        " SELECT f.target FROM slot_flows f JOIN reached ON f.source = reached.slot"
        " WHERE f.config_hash = ?)"
    )


# The sites of one unit only, for each call: `s` is a row of slot_sites.
# WHY: each unit that uses a template instance gives its sites (column
# `unit`, ``db/_slots.py``), thus one call can have a row from each unit.
# The rows of the unit with the smallest key stay, and '' (the written code)
# is the smallest.  The rows of one unit keep their count: the two calls of
# one macro expansion are two sites.
# WHY the `+` of `+u.unit`: it keeps the planner off the index of the unit.
# With no statistics (after the migration, before `PRAGMA optimize`) the
# planner read that index for MIN, once for each row: 40 000 sites took
# 51 s.  The index of the file finds the rows of the place.  A row of the
# written code needs no look: no key is smaller than ''.
ONE_UNIT_SITES = (
    "(s.unit = '' OR s.unit = (SELECT MIN(+u.unit) FROM slot_sites u"
    " WHERE u.config_hash = s.config_hash AND u.file = s.file AND u.line = s.line AND u.col = s.col"
    " AND u.caller IS s.caller AND u.kind = s.kind AND u.slot = s.slot AND u.slot_type = s.slot_type"
    " AND u.reason = s.reason AND u.callee = s.callee))"
)


def _sites_filter(slot: str | None, caller: str | None) -> tuple[str, str, list]:
    """The CTE, the WHERE clause and the parameters of the sites of a slot, or of a caller."""
    if slot is not None:
        return _reached_slots_sql(), f"s.slot IN (SELECT slot FROM reached) AND {ONE_UNIT_SITES}", [slot]
    return "", f"s.caller = ? AND {ONE_UNIT_SITES}", [caller]


def indirect_sites(
    conn: sqlite3.Connection, config_hash: str, *, slot: str | None = None, caller: str | None = None,
    limit: int, offset: int = 0,
) -> list[sqlite3.Row]:
    """The indirect calls through *slot* (and the slots its value flows into), or the ones that *caller* makes.

    The order is the place, the ordinal at the place, the unit and the row:
    a total order, thus two pages never overlap.  WHY the unit: each unit
    counts the ordinals of its instance sites, thus two units can show two
    sites with one place and one ordinal.
    """
    cte, where, params = _sites_filter(slot, caller)
    cte_params = [params[0], config_hash] if slot is not None else []
    return conn.execute(
        f"""{cte}
            SELECT s.file, s.line, s.col, s.ordinal, s.caller, s.kind, s.slot, s.slot_type, s.reason,
                   c.qualified_name AS caller_qname, c.name AS caller_name, c.kind AS caller_kind
            FROM slot_sites s
            LEFT JOIN symbols c ON c.config_hash = s.config_hash AND c.usr = s.caller
            WHERE s.config_hash = ? AND {where}
            ORDER BY s.file, s.line, s.col, s.ordinal, s.unit, s.id
            LIMIT ? OFFSET ?""",
        (*cte_params, config_hash, *([] if slot is not None else params), limit, offset),
    ).fetchall()


def count_indirect_sites(
    conn: sqlite3.Connection, config_hash: str, *, slot: str | None = None, caller: str | None = None,
) -> int:
    cte, where, params = _sites_filter(slot, caller)
    cte_params = [params[0], config_hash] if slot is not None else []
    return int(conn.execute(
        f"{cte} SELECT COUNT(*) FROM slot_sites s WHERE s.config_hash = ? AND {where}",
        (*cte_params, config_hash, *([] if slot is not None else params)),
    ).fetchone()[0])


def site_targets(
    conn: sqlite3.Connection, config_hash: str, site: sqlite3.Row, limit: int,
) -> tuple[list[dict], int]:
    """The functions that the call edges of *site* reach: (at most *limit* of them, the total).

    The edges of the resolution are ``refs`` rows at the place of the
    site.  Two sites at one place (`f(x)(y)`, one macro expansion) share
    their edges.
    """
    where = (
        "r.config_hash = ? AND r.from_file = ? AND r.from_line = ? AND r.from_col = ?"
        " AND ifnull(r.from_usr, '') = ifnull(?, '') AND r.ref_kind = 'call' AND (r.origins & ?) != 0"
    )
    params = (config_hash, site["file"], site["line"], site["col"], site["caller"], _RESOLVED)
    total = int(conn.execute(f"SELECT COUNT(*) FROM refs r WHERE {where}", params).fetchone()[0])
    rows = conn.execute(
        f"""SELECT r.to_usr, r.origins, t.qualified_name
            FROM refs r LEFT JOIN symbols t ON t.config_hash = r.config_hash AND t.usr = r.to_usr
            WHERE {where}
            ORDER BY (r.origins & {int(RefOrigin.SLOT)}) DESC, t.qualified_name, r.to_usr
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [
        {"name": row["qualified_name"] or row["to_usr"], "origin": _origin(row["origins"])}
        for row in rows
    ], total


def _origin(origins: int) -> str:
    return "slot" if origins & RefOrigin.SLOT else "type_only"


def describe_slot(conn: sqlite3.Connection, config_hash: str, slot: str) -> str:
    """A slot as a reader names it: `Dev::on_rx`, `parameter 0 of attach`, `return of pick`.

    The key of a slot is a USR, which a reader cannot read; "" stays "".
    """
    if not slot:
        return ""
    prefix, rest = slot[0], slot[2:]
    if prefix == "P":
        code, _, index = rest.rpartition("|")
        return f"parameter {index} of {_name(conn, config_hash, code)}"
    if prefix == "R":
        return f"return of {_name(conn, config_hash, rest)}"
    return _name(conn, config_hash, rest)


def _name(conn: sqlite3.Connection, config_hash: str, usr: str) -> str:
    row = conn.execute(
        "SELECT qualified_name FROM symbols WHERE config_hash = ? AND usr = ? LIMIT 1", (config_hash, usr),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else usr


def slot_escapes(conn: sqlite3.Connection, config_hash: str, slot: str) -> bool:
    """Code out of view can write *slot* (its address escapes, or such a slot flows into it)."""
    return conn.execute(
        "SELECT 1 FROM slot_escaped WHERE config_hash = ? AND slot = ? LIMIT 1", (config_hash, slot),
    ).fetchone() is not None


def slot_targets(
    conn: sqlite3.Connection, config_hash: str, slot: str, *, limit: int, offset: int = 0,
) -> list[sqlite3.Row]:
    """The functions that reach *slot* (``slot_targets``, one row each), with their symbols, by qualified name."""
    return conn.execute(
        """SELECT t.function, s.qualified_name, s.name, s.kind, s.file_path, s.line
           FROM (SELECT DISTINCT function FROM slot_targets WHERE config_hash = ? AND slot = ?) t
           LEFT JOIN symbols s ON s.config_hash = ? AND s.usr = t.function
           ORDER BY ifnull(s.qualified_name, t.function), t.function
           LIMIT ? OFFSET ?""",
        (config_hash, slot, config_hash, limit, offset),
    ).fetchall()


def count_slot_targets(conn: sqlite3.Connection, config_hash: str, slot: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(DISTINCT function) FROM slot_targets WHERE config_hash = ? AND slot = ?",
        (config_hash, slot),
    ).fetchone()[0])


def stored_places(
    conn: sqlite3.Connection, config_hash: str, slot: str, function: str,
) -> tuple[list[str], list[sqlite3.Row], bool]:
    """Where the code stored *function* on its ways to *slot*: (the slots it stored it in, the places).

    The resolution records each slot that gives a target to a slot
    (``via``); each way back ends at a slot whose source is the function.
    A place is a row of ``slot_sources``, or of ``slot_arguments`` for a
    function that an indirect call passes into a parameter of its target:
    only a call whose slot holds that target (`ptr_a(h)` gives `h` to the
    targets of `ptr_a`, and not to the targets of `ptr_b`).  WHY the slot
    of the call and not its call edges: two calls at one place (one macro
    expansion) share their edges.

    *capped* is True when the walk stopped at ``_VIA_LIMIT`` slots: the
    places are then a part of them.
    """
    roots: list[str] = []
    seen = {slot}
    todo = [slot]
    capped = False
    while todo:
        if len(seen) > _VIA_LIMIT:
            capped = True
            break
        current = todo.pop()
        vias = [row[0] for row in conn.execute(
            "SELECT DISTINCT via FROM slot_targets WHERE config_hash = ? AND slot = ? AND function = ?",
            (config_hash, current, function),
        )]
        for via in vias:
            if not via:
                roots.append(current)
            elif via not in seen:
                seen.add(via)
                todo.append(via)
    places: list[sqlite3.Row] = []
    for root in sorted(roots):
        places.extend(conn.execute(
            """SELECT file, line FROM slot_sources WHERE config_hash = ? AND slot = ? AND function = ?""",
            (config_hash, root, function),
        ).fetchall())
        if root.startswith("P|"):
            code, _, index = root[2:].rpartition("|")
            places.extend(conn.execute(
                """SELECT a.file, a.line FROM slot_arguments a
                   JOIN slot_sites s ON s.config_hash = a.config_hash AND s.file = a.file
                    AND s.line = a.line AND s.col = a.col AND s.ordinal = a.ordinal AND s.unit = a.unit
                   WHERE a.config_hash = ? AND a.value_kind = 'function' AND a.value = ? AND a.arg_index = ?
                     AND (s.callee = ? OR EXISTS (
                          SELECT 1 FROM slot_targets t
                          WHERE t.config_hash = s.config_hash AND t.slot = s.slot AND t.function = ?))""",
                (config_hash, function, int(index), code, code),
            ).fetchall())
    places.sort(key=lambda place: (place["file"], place["line"]))
    unique = list({(place["file"], place["line"]): place for place in places}.values())
    return sorted(roots), unique, capped


def function_stores(
    conn: sqlite3.Connection, config_hash: str, function: str, *, limit: int, offset: int = 0,
) -> list[sqlite3.Row]:
    """The places where the code takes the address of *function* and the slot it goes into ('' = none known).

    The context is the innermost code that holds the place.  A lambda holds
    its own line and cannot take its own address, thus it is no context of
    itself; a function that stores itself (`schedule(blink)` in `blink`)
    is.
    """
    return conn.execute(
        """SELECT DISTINCT so.file, so.line, so.col, so.slot, so.unit,
                  (SELECT c.qualified_name FROM symbols c
                   WHERE c.config_hash = so.config_hash AND c.kind IN ({kinds})
                     AND NOT (c.kind = 'lambda' AND c.usr = so.function)
                     AND c.file_path = so.file AND c.line <= so.line AND c.end_line >= so.line
                   ORDER BY c.line DESC LIMIT 1) AS context
           FROM slot_sources so
           WHERE so.config_hash = ? AND so.function = ?
           ORDER BY so.file, so.line, so.col, so.slot
           LIMIT ? OFFSET ?""".format(kinds=",".join("?" * len(CODE_KINDS))),
        (*CODE_KINDS, config_hash, function, limit, offset),
    ).fetchall()


def count_function_stores(conn: sqlite3.Connection, config_hash: str, function: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT file, line, col, slot FROM slot_sources"
        " WHERE config_hash = ? AND function = ?)",
        (config_hash, function),
    ).fetchone()[0])


def function_calls_by_slot(
    conn: sqlite3.Connection, config_hash: str, function: str, *, limit: int, offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    """The indirect calls that reach *function*: the call edges of the resolution, with their callers."""
    where = "r.config_hash = ? AND r.to_usr = ? AND r.ref_kind = 'call' AND (r.origins & ?) != 0"
    params = (config_hash, function, _RESOLVED)
    total = int(conn.execute(f"SELECT COUNT(*) FROM refs r WHERE {where}", params).fetchone()[0])
    rows = conn.execute(
        f"""SELECT r.from_file, r.from_line, r.from_col, r.origins, r.ref_kind, r.from_usr, r.to_usr,
                   c.qualified_name AS caller_qname, c.name AS caller_name, c.kind AS caller_kind
            FROM refs r LEFT JOIN symbols c ON c.config_hash = r.config_hash AND c.usr = r.from_usr
            WHERE {where}
            ORDER BY r.from_file, r.from_line, r.from_col, r.rowid
            LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    return rows, total


def sdk_entry(
    conn: sqlite3.Connection, config_hash: str, edge: tuple[int, str, str | None, str, int, int, str],
) -> str | None:
    """The concept that starts the target of an edge (the scheduler, an interrupt), when a rule of the SDK model tells it.

    *edge* is ``(origins, ref_kind, caller, file, line, col, target)`` of a
    row of ``refs``.  Only a ``call`` edge can have an entry: an
    ``indirect`` row of the function of a rule takes an address, it calls
    nothing.  Two cases:

    * The caller is the function of a "call" rule.  The edge is its
      ``SDK`` edge, or an edge that the code gives (the mbed
      ``EventQueue::function_call`` calls a lambda through the instance of
      its template, and the rule then adds no edge).  An edge by the type
      only gets no entry: it is a guess, and the rule then gives its own
      ``SDK`` edge (``_slot_resolution._known_calls``).
    * An edge from a site whose slot a "flow" rule reaches: the site is the
      trigger in C (``z_thread_entry``), and the rule tells what runs the
      trigger.  WHY no test of the origin: the flow gives each function of
      the source slot to the slot of the site, thus the row of the site to
      such a target always holds the origin ``SLOT``.  A row has one key for
      each site, and the origins of the analyses that found it are bits of
      that row.

    In the two cases the target must be in the slot of the rule
    (``slot_targets``).  WHY: a trigger can also get a function from plain
    C code (`my_trampoline(plain)`), and one function can have two rules
    on two parameters; a rule tells only what starts the functions that
    went through its own slot.  None for every other edge.  WHY a text of
    each entry: two rules can give one target.

    An index of an older version has no table ``sdk_links``, and the read
    paths of the MCP server do not migrate it (see ``_memory.
    get_memory_regions``): it gives None, as an index without a rule.
    """
    origins, ref_kind, caller, file, line, col, target = edge
    if ref_kind != "call" or not _table_exists(conn, "sdk_links"):
        return None
    rows = conn.execute(
        """SELECT l.entry FROM sdk_links l
            WHERE ? AND l.config_hash = ? AND l.kind = 'call' AND l.target = ?
              AND EXISTS (SELECT 1 FROM slot_targets t
                           WHERE t.config_hash = l.config_hash AND t.slot = l.slot AND t.function = ?)
           UNION
           SELECT l.entry FROM sdk_links l
             JOIN slot_sites s ON s.config_hash = l.config_hash AND s.slot = l.target
            WHERE l.config_hash = ? AND l.kind = 'flow' AND s.file = ? AND s.line = ? AND s.col = ?
              AND ifnull(s.caller, '') = ifnull(?, '')
              AND EXISTS (SELECT 1 FROM slot_targets t
                           WHERE t.config_hash = l.config_hash AND t.slot = l.slot AND t.function = ?)
           ORDER BY 1""",
        (
            bool(origins & ~RefOrigin.TYPE_ONLY), config_hash, caller or "", target,
            config_hash, file, line, col, caller, target,
        ),
    ).fetchall()
    return "; ".join(row[0] for row in rows) or None
