"""The slot facts of the indirect calls: write and delete (tables slot_*).

The facts and the model: ``indexer/_slots.py``.  Each row is one place of
the unit that writes it, keyed by its file, thus a re-parse of a file
deletes its rows first, as it does for its references.  A fact that only
the body of a template instance gives has the key of its listing in the
column ``unit`` (``slot_unit_key``): a new parse of the listing deletes it by
that key, because it can lie in a header that an other unit owns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from ._chunking import chunked

__all__ = [
    "SLOT_TABLES",
    "delete_slot_facts_for_config",
    "delete_slot_facts_for_files",
    "delete_slot_facts_of_unit",
    "insert_slot_facts",
    "slot_unit_key",
]

# The tables of the slot facts.  A literal list: a table name is never
# caller data.
SLOT_TABLES: tuple[str, ...] = (
    "slot_sources", "slot_flows", "slot_sites", "slot_arguments", "slot_escapes",
)
# The result of the resolution of the build, and the rules of the SDK model
# that apply to it: no row belongs to a file, thus
# the resolution replaces them at each run and a purge of the build deletes
# them.
RESOLUTION_TABLES: tuple[str, ...] = ("slot_targets", "slot_escaped", "sdk_links")


def slot_unit_key(listing: tuple[str, str]) -> str:
    """The value of the column ``unit`` for the listing ``(normalized path, flags_hash)``.

    The hash comes first: it has a fixed form, thus a ``|`` in the path
    cannot make two listings one key.
    """
    path, flags_hash = listing
    return f"{flags_hash}|{path}"


def insert_slot_facts(
    conn: sqlite3.Connection,
    config_hash: str,
    sources: Sequence[tuple],
    flows: Sequence[tuple],
    sites: Sequence[tuple],
    arguments: Sequence[tuple],
    escapes: Sequence[tuple],
    unit: str = "",
) -> None:
    """Write the slot facts of one unit.

    Each row starts with (file, line, col); the rest follows the columns of
    its table (``_schema.py``).  *file* is relative to the project root, as
    in ``refs``.  *unit* is "" for the facts of the written code, and the
    ``slot_unit_key`` of the listing for the facts of its template instances.
    """
    conn.executemany(
        "INSERT INTO slot_sources (config_hash, file, line, col, function, function_type, slot, unit)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(config_hash, *row, unit) for row in sources],
    )
    conn.executemany(
        "INSERT INTO slot_flows (config_hash, file, line, col, source, target, unit) VALUES (?,?,?,?,?,?,?)",
        [(config_hash, *row, unit) for row in flows],
    )
    conn.executemany(
        "INSERT INTO slot_sites"
        " (config_hash, file, line, col, ordinal, caller, kind, slot, slot_type, reason, callee, unit)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(config_hash, *row, unit) for row in sites],
    )
    conn.executemany(
        "INSERT INTO slot_arguments (config_hash, file, line, col, ordinal, arg_index, value_kind, value, unit)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(config_hash, *row, unit) for row in arguments],
    )
    conn.executemany(
        "INSERT INTO slot_escapes (config_hash, file, line, col, slot, reason, unit) VALUES (?,?,?,?,?,?,?)",
        [(config_hash, *row, unit) for row in escapes],
    )


def delete_slot_facts_for_files(
    conn: sqlite3.Connection, config_hash: str, files: Sequence[str], *, every_unit: bool = False,
) -> None:
    """Delete the slot facts of the written code of *files* (relative paths) in the build *config_hash*.

    WHY each re-parse: the tables have no unique key (one line can hold two
    sources of one function), thus a fact of a header that a later run
    parses again would gain a duplicate row at each run.  The facts of the
    template instances stay: their listing deletes them
    (``delete_slot_facts_of_unit``).

    *every_unit*: the files left the build, thus the facts of the template
    instances in them go too.  Kept, they gave call edges into a file that
    is gone until each unit that used the instances was parsed again.
    """
    paths = list(dict.fromkeys(files))
    unit_filter = "" if every_unit else "AND unit = '' "
    for batch in chunked(paths, 500):
        placeholders = ",".join("?" * len(batch))
        for table in SLOT_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE config_hash = ? {unit_filter}AND file IN ({placeholders})",
                (config_hash, *batch),
            )


def delete_slot_facts_of_unit(conn: sqlite3.Connection, config_hash: str, unit: str) -> None:
    """Delete the slot facts of the template instances that the listing *unit* gave.

    The caller deletes them before a new parse of the listing, and when the
    listing leaves the build, as ``release_ref_producer`` does for the edges
    of the instances.
    """
    for table in SLOT_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE config_hash = ? AND unit = ?", (config_hash, unit))


def delete_slot_facts_for_config(conn: sqlite3.Connection, config_hash: str) -> None:
    """Delete every slot fact of the build *config_hash*, and the result of its resolution."""
    for table in (*SLOT_TABLES, *RESOLUTION_TABLES):
        conn.execute(f"DELETE FROM {table} WHERE config_hash = ?", (config_hash,))
