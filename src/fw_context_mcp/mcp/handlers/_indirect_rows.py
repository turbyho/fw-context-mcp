"""The rows of the answers about indirect calls, shared by the call graph and the source tools.

``find_indirect_call_sites``, ``find_indirect_targets`` and
``get_symbol_context`` give one shape for one fact: an indirect call site,
a function that reaches a slot, a place that stores the address of a
function.  The data come from ``indexer/db/_indirect.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from ...indexer.db import _indirect as indirect_db
from ...utils import abs_path

# The targets that a site row lists; ``targets_total`` counts all of them.
SITE_TARGETS_SHOWN = 10


def entry_field(conn: sqlite3.Connection, config_hash: str, edge: sqlite3.Row) -> dict:
    """``{"entry": ...}`` for a caller row of an edge that a rule of the SDK model gives, else ``{}``.

    *edge* is a row of ``refs`` with ``origins``, ``ref_kind``,
    ``from_usr``, ``from_file``, ``from_line``, ``from_col`` and ``to_usr``.  WHY no key
    for the other edges: the field tells what starts the target (the
    scheduler, an interrupt), and a plain call has no such thing.
    """
    entry = indirect_db.sdk_entry(conn, config_hash, (
        edge["origins"], edge["ref_kind"], edge["from_usr"], edge["from_file"], edge["from_line"], edge["from_col"],
        edge["to_usr"],
    ))
    return {"entry": entry} if entry else {}


def site_row(conn: sqlite3.Connection, config_hash: str, root: Path, site: sqlite3.Row) -> dict:
    """One indirect call site: its place, its caller, its slot and the functions it reaches.

    ``kind`` is "pointer" (a call through a function pointer), "object"
    (the ``operator()`` of a callable object) or "address" (a call of an
    address that is a number, such as a bootloader that starts the
    application: ``address`` holds its text, and no code of the build is
    a target).  ``no_slot`` says why the call names no slot; such a call
    reaches the functions of its type.  ``slot_escapes`` marks a slot that
    code out of view can write: the call then reaches the functions of its
    type too.
    """
    targets, total = indirect_db.site_targets(conn, config_hash, site, SITE_TARGETS_SHOWN)
    row: dict = {
        "file": abs_path(root, site["file"]),
        "line": site["line"],
        "col": site["col"],
        "caller": site["caller_qname"] or site["caller_name"] or site["caller"] or "<file scope>",
        "caller_kind": site["caller_kind"],
        "kind": site["kind"],
        "slot": indirect_db.describe_slot(conn, config_hash, site["slot"]),
        "type": site["slot_type"],
        "targets_total": total,
        "targets": targets,
    }
    if site["kind"] == "address":
        row["address"] = site["reason"]
    elif not site["slot"]:
        row["no_slot"] = site["reason"]
    elif indirect_db.slot_escapes(conn, config_hash, site["slot"]):
        row["slot_escapes"] = True
    return row


def target_row(
    conn: sqlite3.Connection, config_hash: str, root: Path, slot: str, target: sqlite3.Row,
) -> dict:
    """One function that reaches *slot*: the function, and where the code stored it.

    ``stored_file`` and ``stored_line`` are the first place that stores
    the function, and ``stored_places`` counts the places of every way to
    *slot*.  ``stored_in`` names the slots that the code stored the
    function in, when one of them is not *slot* itself (the value flowed
    from it: an argument into a parameter, a parameter into a field).
    """
    roots, places, capped = indirect_db.stored_places(conn, config_hash, slot, target["function"])
    place = places[0] if places else None
    row: dict = {
        "function": target["qualified_name"] or target["function"],
        "kind": target["kind"],
        "file": abs_path(root, target["file_path"]) if target["file_path"] else None,
        "line": target["line"],
        "origin": "slot",
        "stored_file": abs_path(root, place["file"]) if place is not None else None,
        "stored_line": place["line"] if place is not None else None,
        "stored_places": len(places),
    }
    if roots != [slot]:
        row["stored_in"] = [indirect_db.describe_slot(conn, config_hash, stored) for stored in roots]
    if capped:
        row["stored_places_capped"] = True
    return row


# One part of an answer that pages over several queries: its count, and a
# fetch of (limit, offset) that gives its rows.
Part = tuple[int, Callable[[int, int], list[dict]]]


def page_parts(parts: list[Part], limit: int, offset: int) -> list[dict]:
    """The rows of one page over *parts* in their order, as one list that pages as a whole."""
    rows: list[dict] = []
    skip = offset
    for count, fetch in parts:
        if len(rows) >= limit:
            break
        if skip >= count:
            skip -= count
            continue
        rows.extend(fetch(limit - len(rows), skip))
        skip = 0
    return rows
