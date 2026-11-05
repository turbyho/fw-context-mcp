"""The definitions of the types that the ODR can merge, and the decisions of the build about them.

``cursor_usr`` writes the place of the definition of each named type into
its USR (``indexer/_usr.py``).  In C++ a type with external linkage has one
identity in the program (the ODR): two definitions of it in two files (a
copy of a header in the project) are one type, and the linker joins the
methods of the two by name.  The index run decides the types to merge, and
the place of each class template that a unit declares and does not define,
from all units (``indexer/_odr.py``).  It parses again each unit whose rows
must change.  Five tables keep the state between runs:

* ``type_places`` — one row for each definition of a type with a merge key
  (``_usr.merge_key``) that a unit stored: the file, the merge key, the
  line and the column.  A type in the body of a type has no row: it
  follows the merge of that type (``_usr.odr_place``).  A C tag at the scope of a file has external
  linkage too, thus it has rows: the build reads the files that C++ units
  read, and a header can be one of them.  The rows of a file go with the
  other rows of the file (``_files._delete_rows_owned_by``).  A file with
  such a row is no orphan (``_files.delete_orphan_files``).
* ``odr_merged_types`` — the ``(merge key, file key)`` of each definition
  that the rows of the build merge: their USRs leave out its place.
* ``odr_template_places`` — the place of each class template that a unit
  declared and did not define, by its merge key: the USRs of its
  specializations take it.  A template with no single place in the build
  has no row.
* ``odr_pending_units`` — the units that a run must parse again because the
  decisions changed.  A run that stops before it parsed them leaves them
  here, and the next run parses them first: the rows of the build and the
  decisions then agree again.
* ``odr_unit_places`` — one row for each listing that a parse stored: the
  file keys of the places that its USRs hold, and the merge keys of the
  templates whose place it looked for (``_usr.type_decisions``).  A change
  of the decisions parses again only the listings whose rows hold a file
  or a template of the change.  The row is written in the transaction of
  the rows of the listing, thus the two agree also after a run that
  stopped.

The tables are created when first used, not in ``_SCHEMA``, for the reason
that ``_owners.py`` gives: a new table there would change the schema version
of every index.  An index without them reads as "no definition, no merge".
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import NamedTuple

_TABLES = (
    """CREATE TABLE IF NOT EXISTS type_places (
        config_hash TEXT    NOT NULL,
        file_id     INTEGER NOT NULL,
        merge_key   TEXT    NOT NULL,
        line        INTEGER NOT NULL,
        col         INTEGER NOT NULL,
        PRIMARY KEY (config_hash, file_id, merge_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_type_places_file ON type_places(file_id)",
    """CREATE TABLE IF NOT EXISTS odr_merged_types (
        config_hash TEXT NOT NULL,
        merge_key   TEXT NOT NULL,
        file_key    TEXT NOT NULL,
        PRIMARY KEY (config_hash, merge_key, file_key)
    )""",
    """CREATE TABLE IF NOT EXISTS odr_template_places (
        config_hash TEXT NOT NULL,
        merge_key   TEXT    NOT NULL,
        file_key    TEXT    NOT NULL,
        line        INTEGER NOT NULL,
        col         INTEGER NOT NULL,
        PRIMARY KEY (config_hash, merge_key)
    )""",
    """CREATE TABLE IF NOT EXISTS odr_pending_units (
        config_hash TEXT NOT NULL,
        path        TEXT NOT NULL,
        PRIMARY KEY (config_hash, path)
    )""",
    """CREATE TABLE IF NOT EXISTS odr_unit_places (
        config_hash     TEXT NOT NULL,
        unit_path       TEXT NOT NULL,
        unit_flags_hash TEXT NOT NULL,
        file_keys       TEXT NOT NULL,
        template_keys   TEXT NOT NULL,
        PRIMARY KEY (config_hash, unit_path, unit_flags_hash)
    )""",
)
_TABLE_NAMES = ("type_places", "odr_merged_types", "odr_template_places", "odr_pending_units", "odr_unit_places")


class UnitPlaces(NamedTuple):
    """What the stored rows of one listing read from the decisions of the build (``odr_unit_places``)."""

    files: frozenset[str]
    templates: frozenset[str]


def ensure_type_place_tables(conn: sqlite3.Connection) -> None:
    """Create the tables when they are not there.

    Plain ``execute`` calls: ``executescript`` commits the open transaction
    first, and a caller can be inside one.
    """
    for statement in _TABLES:
        conn.execute(statement)


def _tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?, ?)", _TABLE_NAMES,
    ).fetchone()
    return row[0] == len(_TABLE_NAMES)


def insert_type_places(conn: sqlite3.Connection, rows: Iterable[tuple[str, int, str, int, int]]) -> None:
    """Store ``(config_hash, file_id, merge key, line, column)`` rows of one unit.

    ``INSERT OR IGNORE``: one file holds one definition of one type, and a
    unit stores a file once.
    """
    batch = list(rows)
    if not batch:
        return
    ensure_type_place_tables(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO type_places (config_hash, file_id, merge_key, line, col) VALUES (?, ?, ?, ?, ?)",
        batch,
    )


def delete_type_places_for_files(conn: sqlite3.Connection, file_ids: Iterable[int]) -> None:
    """Remove the definitions of *file_ids*: their rows are about to go, or to be stored again."""
    if not _tables_exist(conn):
        return
    conn.executemany("DELETE FROM type_places WHERE file_id = ?", [(file_id,) for file_id in file_ids])


def load_type_places(conn: sqlite3.Connection, config_hash: str) -> list[tuple[str, str, int, int]]:
    """Return ``(file path, merge key, line, column)`` of each stored definition of *config_hash*."""
    if not _tables_exist(conn):
        return []
    return [
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT f.path, p.merge_key, p.line, p.col FROM type_places p JOIN files f ON f.id = p.file_id "
            "WHERE p.config_hash = ?",
            (config_hash,),
        )
    ]


def file_ids_with_places(conn: sqlite3.Connection, config_hash: str) -> set[int]:
    """Return the ``files.id`` of each file that holds a stored definition of *config_hash*."""
    if not _tables_exist(conn):
        return set()
    return {
        row[0] for row in conn.execute("SELECT DISTINCT file_id FROM type_places WHERE config_hash = ?", (config_hash,))
    }


def load_merged_types(conn: sqlite3.Connection, config_hash: str) -> frozenset[tuple[str, str]]:
    """Return the ``(merge key, file key)`` of each definition that the rows of *config_hash* merge."""
    if not _tables_exist(conn):
        return frozenset()
    return frozenset(
        (row[0], row[1])
        for row in conn.execute(
            "SELECT merge_key, file_key FROM odr_merged_types WHERE config_hash = ?", (config_hash,),
        )
    )


def store_merged_types(conn: sqlite3.Connection, config_hash: str, merged: Iterable[tuple[str, str]]) -> None:
    """Replace the merged definitions of *config_hash* with *merged*."""
    ensure_type_place_tables(conn)
    conn.execute("DELETE FROM odr_merged_types WHERE config_hash = ?", (config_hash,))
    conn.executemany(
        "INSERT INTO odr_merged_types (config_hash, merge_key, file_key) VALUES (?, ?, ?)",
        [(config_hash, key, file) for key, file in merged],
    )


def load_template_places(conn: sqlite3.Connection, config_hash: str) -> dict[str, tuple[str, int, int]]:
    """Return ``(file key, line, column)`` of each template of *config_hash* that a unit looked for and the build placed."""
    if not _tables_exist(conn):
        return {}
    return {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT merge_key, file_key, line, col FROM odr_template_places WHERE config_hash = ?", (config_hash,),
        )
    }


def store_template_places(
    conn: sqlite3.Connection, config_hash: str, places: Mapping[str, tuple[str | None, int, int]],
) -> None:
    """Replace the places of the templates of *config_hash* with *places*."""
    ensure_type_place_tables(conn)
    conn.execute("DELETE FROM odr_template_places WHERE config_hash = ?", (config_hash,))
    conn.executemany(
        "INSERT INTO odr_template_places (config_hash, merge_key, file_key, line, col) VALUES (?, ?, ?, ?, ?)",
        [(config_hash, key, *place) for key, place in places.items()],
    )


def load_pending_units(conn: sqlite3.Connection, config_hash: str) -> frozenset[str]:
    """Return the normalized path of each unit that a run must parse again for the decisions."""
    if not _tables_exist(conn):
        return frozenset()
    return frozenset(
        row[0] for row in conn.execute("SELECT path FROM odr_pending_units WHERE config_hash = ?", (config_hash,))
    )


def add_pending_units(conn: sqlite3.Connection, config_hash: str, paths: Iterable[str]) -> None:
    """Add *paths* (normalized paths of units) to the units that a run must parse again."""
    ensure_type_place_tables(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO odr_pending_units (config_hash, path) VALUES (?, ?)",
        [(config_hash, path) for path in paths],
    )


def clear_pending_units(conn: sqlite3.Connection, config_hash: str, paths: Iterable[str]) -> None:
    """Remove *paths* from the pending units: their rows agree with the decisions now."""
    if not _tables_exist(conn):
        return
    conn.executemany(
        "DELETE FROM odr_pending_units WHERE config_hash = ? AND path = ?", [(config_hash, path) for path in paths],
    )


def store_unit_places(
    conn: sqlite3.Connection, config_hash: str, listing: tuple[str, str],
    file_keys: Iterable[str], template_keys: Iterable[str],
) -> None:
    """Replace what the rows of *listing* read from the decisions: the file keys of their places, the keys of their templates."""
    ensure_type_place_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO odr_unit_places (config_hash, unit_path, unit_flags_hash, file_keys, template_keys) "
        "VALUES (?, ?, ?, ?, ?)",
        (config_hash, listing[0], listing[1], json.dumps(sorted(file_keys)), json.dumps(sorted(template_keys))),
    )


def load_unit_places(conn: sqlite3.Connection, config_hash: str) -> dict[tuple[str, str], UnitPlaces]:
    """Return what the rows of each listing read from the decisions, for each listing whose parse stored it.

    A listing with no row has rows of an index without this table: the
    caller cannot tell what they read.
    """
    if not _tables_exist(conn):
        return {}
    return {
        (row[0], row[1]): UnitPlaces(frozenset(json.loads(row[2])), frozenset(json.loads(row[3])))
        for row in conn.execute(
            "SELECT unit_path, unit_flags_hash, file_keys, template_keys FROM odr_unit_places WHERE config_hash = ?",
            (config_hash,),
        )
    }


def delete_unit_places(conn: sqlite3.Connection, config_hash: str, listing: tuple[str, str]) -> None:
    """Remove the row of *listing*: it left the build, and its rows go."""
    if not _tables_exist(conn):
        return
    conn.execute(
        "DELETE FROM odr_unit_places WHERE config_hash = ? AND unit_path = ? AND unit_flags_hash = ?",
        (config_hash, listing[0], listing[1]),
    )


def delete_type_places_for_build(conn: sqlite3.Connection, config_hash: str) -> None:
    """Delete the definitions, the decisions, the pending units and the rows of the units of *config_hash*.

    For ``delete_build_data``.
    """
    if not _tables_exist(conn):
        return
    conn.execute("DELETE FROM type_places WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM odr_merged_types WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM odr_template_places WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM odr_pending_units WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM odr_unit_places WHERE config_hash = ?", (config_hash,))
