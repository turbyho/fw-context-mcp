"""The owner of each header: the translation unit that stores its rows.

The index run parses the units in a fixed order (``runner._in_claim_order``).
The first unit that walks a header claims it: that unit writes the rows of
the header, and each later unit skips its subtree (``skip_files``).  A header
that a C unit and a C++ unit include, or that two units read with different
defines, has different rows for each unit.  Thus the owner decides the
content of the index.

An incremental run parses only the changed units.  Before these tables, its
skip set started empty, and the first changed unit claimed every header that
it walked, also the headers that an earlier, unchanged unit owns in a full
run.  It deleted and wrote again the rows of those headers in its own
context.  Measured on an ESP32 project: one edited unit rewrote 457 files and
8 973 embeddings, and the result was different from a full run.

Two tables keep the claims of the last run:

* ``header_owners`` — one row for each claimed file: its path and the unit
  that claimed it.  The key is the path, not ``files.id``: a unit can claim a
  header that has no ``files`` row (only forward declarations), and that
  claim must also stop a later unit.
* ``owner_units`` — one row for each unit whose claims are recorded.  A unit
  that claims no file has no ``header_owners`` row.  Without this marker, a
  run cannot tell such a unit from a unit whose claims nobody recorded (an
  index from an older version).  A unit without the marker is parsed again.

A unit is the pair ``(path, flags_hash)``: compile_commands.json can list one
file more than once with different flags, and each listing claims files.

The tables are created when first used, not in ``_SCHEMA``:
``_derive_schema_version`` hashes the tables of ``_SCHEMA``, and a new table
there would set ``reindex_needed`` on every index.  An index without the
tables reads as "no claims recorded", and the next run parses each unit one
time.  An older version ignores the tables.

The writes of one unit occur in the transaction that stores its rows, thus
the claims and the rows always agree, also after a run that stopped.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

UnitKey = tuple[str, str]
"""``(normalized path, flags_hash)`` of one listing of compile_commands.json."""

_TABLES = (
    """CREATE TABLE IF NOT EXISTS header_owners (
        config_hash      TEXT NOT NULL,
        path             TEXT NOT NULL,
        owner_path       TEXT NOT NULL,
        owner_flags_hash TEXT NOT NULL,
        PRIMARY KEY (config_hash, path)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_header_owners_owner
        ON header_owners(config_hash, owner_path, owner_flags_hash)""",
    """CREATE TABLE IF NOT EXISTS owner_units (
        config_hash      TEXT NOT NULL,
        owner_path       TEXT NOT NULL,
        owner_flags_hash TEXT NOT NULL,
        PRIMARY KEY (config_hash, owner_path, owner_flags_hash)
    )""",
)


@dataclass(frozen=True)
class Ownership:
    """The claims of one build, as the last run recorded them.

    *recorded* holds each unit whose claims are known.  *owned* gives the
    files that each unit claimed.  A recorded unit that claimed nothing is
    in *recorded* and not in *owned*.
    """

    recorded: frozenset[UnitKey] = frozenset()
    owned: dict[UnitKey, frozenset[str]] = field(default_factory=dict)

    def files_of(self, unit: UnitKey) -> frozenset[str]:
        """Return the files that *unit* claimed, empty when it claimed none."""
        return self.owned.get(unit, frozenset())

    @cached_property
    def _owner_by_path(self) -> dict[str, UnitKey]:
        # One map for all lookups: a caller asks for each header of a unit,
        # and a large build has hundreds of headers and of owners.
        return {path: unit for unit, paths in self.owned.items() for path in paths}

    def owner_of(self, path: str) -> UnitKey | None:
        """Return the listing that claimed *path*, or None when no listing did."""
        return self._owner_by_path.get(path)


def skip_form(paths: Iterable[str], project_root: Path) -> set[str]:
    """Return *paths* in the form of ``skip_files``: resolved and absolute.

    These tables hold normalized paths (relative inside the project, see
    ``ops._normalize_file_path``).  ``extract_all`` compares the resolved
    path of each cursor with ``skip_files``.

    A normalized path is the resolved path relative to the resolved root,
    thus the join with the resolved root gives the resolved path again, and
    no file system call is necessary for each path.
    """
    root = project_root.resolve()
    return {path if os.path.isabs(path) else str(root / path) for path in paths}


def unclaimed(paths: Iterable[str], skip_files: set[str] | frozenset[str], project_root: Path) -> set[str]:
    """Return the normalized *paths* that are not in *skip_files*.

    A path in *skip_files* has an owner in this run: an earlier listing
    claimed it.
    """
    root = project_root.resolve()
    return {
        path for path in paths
        if (path if os.path.isabs(path) else str(root / path)) not in skip_files
    }


def number_listings(keys: Iterable[UnitKey]) -> list[UnitKey]:
    """Give each repeated ``(path, flags_hash)`` its own key, in the order of *keys*.

    compile_commands.json can list one file more than once with the same
    flags (only ``-o`` differs, and the flags hash drops it): Zephyr builds
    ``misc/empty_file.c`` so, and so does a CMake object library that two
    targets use.  The second listing claims nothing new, because the first
    one claimed its files.  With one key for the two, the claims of the
    second replaced the claims of the first, and the shared headers had no
    owner.  The second listing gets ``flags_hash#0002``, the third ``#0003``.
    """
    seen: dict[UnitKey, int] = {}
    numbered: list[UnitKey] = []
    for key in keys:
        seen[key] = seen.get(key, 0) + 1
        numbered.append(listing_key(key, seen[key]))
    return numbered


def listing_key(key: UnitKey, occurrence: int) -> UnitKey:
    """Return the key of the *occurrence*-th listing of *key* (1 for the first).

    The number has leading zeros: the keys sort as strings (the bisect of
    the run and ``files_claimed_before``), and ``#10`` sorted before ``#2``.
    """
    return key if occurrence == 1 else (key[0], f"{key[1]}#{occurrence:04d}")


def files_claimed_before(ownership: Ownership, unit: UnitKey, exclude: Iterable[UnitKey] = ()) -> set[str]:
    """Return the files that the listings before *unit* in the claim order own.

    The claim order is the order of the keys (``runner._in_claim_order``).
    *exclude* holds listings that the caller parses again: their claims of
    the last run do not count, because the new parse gives them.
    """
    skip = set(exclude)
    claimed: set[str] = set()
    for owner, paths in ownership.owned.items():
        if owner < unit and owner not in skip:
            claimed.update(paths)
    return claimed


def _tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('header_owners', 'owner_units')"
    ).fetchone()
    return row[0] == 2


def ensure_owner_tables(conn: sqlite3.Connection) -> None:
    """Create the two tables when they are not there.

    Plain ``execute`` calls, not ``executescript``: ``executescript`` commits
    the open transaction first, and a caller can be inside one.
    """
    for statement in _TABLES:
        conn.execute(statement)


def load_ownership(conn: sqlite3.Connection, config_hash: str) -> Ownership:
    """Return the claims that the last runs recorded for *config_hash*.

    An index without the tables gives an empty result: no unit is recorded,
    thus the run parses each unit and records its claims.
    """
    if not _tables_exist(conn):
        return Ownership()
    recorded = frozenset(
        (row[0], row[1])
        for row in conn.execute(
            "SELECT owner_path, owner_flags_hash FROM owner_units WHERE config_hash = ?",
            (config_hash,),
        )
    )
    owned: dict[UnitKey, set[str]] = {}
    for row in conn.execute(
        "SELECT path, owner_path, owner_flags_hash FROM header_owners WHERE config_hash = ?",
        (config_hash,),
    ):
        owned.setdefault((row[1], row[2]), set()).add(row[0])
    return Ownership(
        recorded=recorded,
        owned={unit: frozenset(paths) for unit, paths in owned.items()},
    )


def record_claims(
    conn: sqlite3.Connection, config_hash: str, unit: UnitKey, paths: Iterable[str],
) -> None:
    """Record that *unit* claimed *paths* in this run, and nothing else.

    The rows of the earlier claims of *unit* go first: a file that the unit
    does not claim now has no owner until a later unit claims it.  A file
    that another unit owned before moves to *unit* (``INSERT OR REPLACE``):
    the run gave it to *unit*, thus the earlier owner lost it.
    """
    ensure_owner_tables(conn)
    owner_path, owner_flags_hash = unit
    conn.execute(
        "DELETE FROM header_owners WHERE config_hash = ? AND owner_path = ? AND owner_flags_hash = ?",
        (config_hash, owner_path, owner_flags_hash),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO header_owners (config_hash, path, owner_path, owner_flags_hash) "
        "VALUES (?, ?, ?, ?)",
        [(config_hash, path, owner_path, owner_flags_hash) for path in paths],
    )
    conn.execute(
        "INSERT OR IGNORE INTO owner_units (config_hash, owner_path, owner_flags_hash) "
        "VALUES (?, ?, ?)",
        (config_hash, owner_path, owner_flags_hash),
    )


def forget_unit(conn: sqlite3.Connection, config_hash: str, unit: UnitKey) -> None:
    """Remove the claims and the marker of a unit that left the build."""
    if not _tables_exist(conn):
        return
    params = (config_hash, *unit)
    conn.execute(
        "DELETE FROM header_owners WHERE config_hash = ? AND owner_path = ? AND owner_flags_hash = ?",
        params,
    )
    conn.execute(
        "DELETE FROM owner_units WHERE config_hash = ? AND owner_path = ? AND owner_flags_hash = ?",
        params,
    )


def release_files(conn: sqlite3.Connection, config_hash: str, paths: Iterable[str]) -> None:
    """Remove the owner of *paths*: no unit of the build claims them now."""
    if not _tables_exist(conn):
        return
    conn.executemany(
        "DELETE FROM header_owners WHERE config_hash = ? AND path = ?",
        [(config_hash, path) for path in paths],
    )


def files_without_owner(conn: sqlite3.Connection, config_hash: str) -> set[str]:
    """Return the path of each file that has symbol or macro rows and no owner.

    A full run gives each such file an owner, or leaves it without rows.
    Thus a file in this set is a left-over: a run that stopped after a
    listing lost the file and before the next includer claimed it, or rows
    from a version without these tables.  The index run gives each one to
    its first includer (``runner._hand_over``).

    The rows of the assembly pass (``asm:``) and of the linker scripts
    (``ld:``) have no owner by design; the caller keeps only the files that
    a listing includes.
    """
    with_rows = {row[0] for row in conn.execute(_FILES_WITH_ROWS, (config_hash,))}
    if not with_rows or not _tables_exist(conn):
        return with_rows
    owned = {
        row[0]
        for row in conn.execute("SELECT path FROM header_owners WHERE config_hash = ?", (config_hash,))
    }
    return with_rows - owned


_FILES_WITH_ROWS = """
    SELECT f.path FROM files f
    WHERE f.config_hash = ?
      AND (EXISTS (SELECT 1 FROM symbols s WHERE s.file_id = f.id
                   AND s.usr NOT LIKE 'asm:%' AND s.usr NOT LIKE 'ld:%')
           OR EXISTS (SELECT 1 FROM macros m WHERE m.file_id = f.id))
"""


def delete_ownership_for_build(conn: sqlite3.Connection, config_hash: str) -> None:
    """Delete the claims of *config_hash*, for ``delete_build_data``."""
    if not _tables_exist(conn):
        return
    conn.execute("DELETE FROM header_owners WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM owner_units WHERE config_hash = ?", (config_hash,))
