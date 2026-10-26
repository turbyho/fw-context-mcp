"""The slot facts of the indirect calls: write and delete (tables slot_*).

The facts and the model: ``indexer/_slots.py``.  Each row is one place of
the unit that writes it, keyed by its file, thus a re-parse of a file
deletes its rows first, as it does for its references.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from ._chunking import chunked

__all__ = [
    "SLOT_TABLES",
    "delete_slot_facts_for_config",
    "delete_slot_facts_for_files",
    "insert_slot_facts",
]

# The tables of the slot facts.  A literal list: a table name is never
# caller data.
SLOT_TABLES: tuple[str, ...] = (
    "slot_sources", "slot_flows", "slot_sites", "slot_arguments", "slot_escapes",
)


def insert_slot_facts(
    conn: sqlite3.Connection,
    config_hash: str,
    sources: Sequence[tuple],
    flows: Sequence[tuple],
    sites: Sequence[tuple],
    arguments: Sequence[tuple],
    escapes: Sequence[tuple],
) -> None:
    """Write the slot facts of one unit.

    Each row starts with (file, line, col); the rest follows the columns of
    its table (``_schema.py``).  *file* is relative to the project root, as
    in ``refs``.
    """
    conn.executemany(
        "INSERT INTO slot_sources (config_hash, file, line, col, function, function_type, slot)"
        " VALUES (?,?,?,?,?,?,?)",
        [(config_hash, *row) for row in sources],
    )
    conn.executemany(
        "INSERT INTO slot_flows (config_hash, file, line, col, source, target) VALUES (?,?,?,?,?,?)",
        [(config_hash, *row) for row in flows],
    )
    conn.executemany(
        "INSERT INTO slot_sites (config_hash, file, line, col, ordinal, caller, kind, slot, slot_type, reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(config_hash, *row) for row in sites],
    )
    conn.executemany(
        "INSERT INTO slot_arguments (config_hash, file, line, col, ordinal, arg_index, value_kind, value)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(config_hash, *row) for row in arguments],
    )
    conn.executemany(
        "INSERT INTO slot_escapes (config_hash, file, line, col, slot, reason) VALUES (?,?,?,?,?,?)",
        [(config_hash, *row) for row in escapes],
    )


def delete_slot_facts_for_files(
    conn: sqlite3.Connection, config_hash: str, files: Sequence[str],
) -> None:
    """Delete the slot facts of *files* (relative paths) in the build *config_hash*.

    WHY each re-parse: the tables have no unique key (one line can hold two
    sources of one function), thus a fact of a header that a later run
    parses again would gain a duplicate row at each run.
    """
    paths = list(dict.fromkeys(files))
    for batch in chunked(paths, 500):
        placeholders = ",".join("?" * len(batch))
        for table in SLOT_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE config_hash = ? AND file IN ({placeholders})",
                (config_hash, *batch),
            )


def delete_slot_facts_for_config(conn: sqlite3.Connection, config_hash: str) -> None:
    """Delete every slot fact of the build *config_hash*."""
    for table in SLOT_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE config_hash = ?", (config_hash,))
