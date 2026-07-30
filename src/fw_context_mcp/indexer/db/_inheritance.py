"""C++ class hierarchy storage helpers for fw-context-mcp index."""

import sqlite3


def insert_inheritance_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert inheritance edges.

    Each row: (config_hash, derived_usr, base_usr, access, is_virtual).
    Uses ON CONFLICT DO UPDATE so re-indexing the same derived class
    updates the access/virtual flags without duplication.

    Returns number of rows inserted or updated.
    """
    cur = conn.executemany(
        """INSERT INTO inheritance (config_hash, derived_usr, base_usr, access, is_virtual)
           VALUES (?,?,?,?,?)
           ON CONFLICT(config_hash, derived_usr, base_usr) DO UPDATE SET
               access = excluded.access,
               is_virtual = excluded.is_virtual""",
        rows,
    )
    return cur.rowcount


def delete_inheritance_for_file(conn: sqlite3.Connection, config_hash: str, file_id: int) -> None:
    """Delete inheritance records for classes defined in the given file.

    Called before re-indexing a TU to remove stale edges for classes
    whose definitions are in the file being re-parsed.
    """
    conn.execute(
        """DELETE FROM inheritance WHERE config_hash = ? AND derived_usr IN (
            SELECT usr FROM symbols WHERE config_hash = ? AND file_id = ?
        )""",
        (config_hash, config_hash, file_id),
    )


def get_direct_bases(conn: sqlite3.Connection, config_hash: str, usr: str) -> list[dict]:
    """Return direct base classes of *usr* (the class this one inherits from).

    Each result: {base_usr, derived_usr, access, is_virtual,
                   base_name, base_kind, base_file}
    """
    rows = conn.execute(
        """SELECT i.derived_usr, i.base_usr, i.access, i.is_virtual,
                  s.name AS base_name, s.kind AS base_kind,
                  s.file_path AS base_file
           FROM inheritance i
           LEFT JOIN symbols s ON s.usr = i.base_usr AND s.config_hash = i.config_hash
           WHERE i.config_hash = ? AND i.derived_usr = ?
           ORDER BY s.name""",
        (config_hash, usr),
    ).fetchall()
    return [dict(r) for r in rows]


def get_direct_derived(conn: sqlite3.Connection, config_hash: str, usr: str) -> list[dict]:
    """Return direct derived classes of *usr* (classes that inherit from this one).

    Each result: {base_usr, derived_usr, access, is_virtual,
                   derived_name, derived_kind, derived_file}
    """
    rows = conn.execute(
        """SELECT i.derived_usr, i.base_usr, i.access, i.is_virtual,
                  s.name AS derived_name, s.kind AS derived_kind,
                  s.file_path AS derived_file
           FROM inheritance i
           LEFT JOIN symbols s ON s.usr = i.derived_usr AND s.config_hash = i.config_hash
           WHERE i.config_hash = ? AND i.base_usr = ?
           ORDER BY s.name""",
        (config_hash, usr),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_overrides_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str]],
) -> int:
    """Insert or replace override relationships.

    Each row: (config_hash, derived_usr, base_usr).
    Deletes existing overrides for the same derived_usr before inserting,
    so re-analysis is idempotent. Returns number of rows inserted.
    """
    if rows:
        config_hash = rows[0][0]
        unique_derived = list({r[1] for r in rows})
        placeholders = ",".join("?" * len(unique_derived))
        conn.execute(
            f"DELETE FROM overrides WHERE config_hash = ? AND derived_usr IN ({placeholders})",
            (config_hash, *unique_derived),
        )
    cur = conn.executemany(
        """INSERT INTO overrides (config_hash, derived_usr, base_usr)
           VALUES (?, ?, ?)""",
        rows,
    )
    return cur.rowcount


def get_overrides_for_method(
    conn: sqlite3.Connection,
    config_hash: str,
    usr: str,
) -> dict[str, list[dict]]:
    """Return what *usr* overrides and what overrides *usr*.

    Returns:
        dict with ``overrides`` (base methods this method overrides) and
        ``overridden_by`` (derived methods that override this one).
    """
    overrides_rows = conn.execute(
        """SELECT o.base_usr, s.name, s.qualified_name, s.kind, s.file_path, s.line
           FROM overrides o
           JOIN symbols s ON s.usr = o.base_usr AND s.config_hash = ?
           WHERE o.config_hash = ? AND o.derived_usr = ?
           ORDER BY s.qualified_name""",
        (config_hash, config_hash, usr),
    ).fetchall()

    overridden_by_rows = conn.execute(
        """SELECT o.derived_usr, s.name, s.qualified_name, s.kind, s.file_path, s.line
           FROM overrides o
           JOIN symbols s ON s.usr = o.derived_usr AND s.config_hash = ?
           WHERE o.config_hash = ? AND o.base_usr = ?
           ORDER BY s.qualified_name""",
        (config_hash, config_hash, usr),
    ).fetchall()

    return {
        "overrides": [dict(r) for r in overrides_rows],
        "overridden_by": [dict(r) for r in overridden_by_rows],
    }


def get_class_members(
    conn: sqlite3.Connection,
    config_hash: str,
    parent_usr: str,
) -> list[sqlite3.Row]:
    """Return all symbols (methods, fields, nested types) belonging to a class/struct.

    Args:
        conn: Open database connection.
        config_hash: Build config hash.
        parent_usr: USR of the parent class/struct.

    Returns:
        List of sqlite3.Row objects with symbol fields, ordered by kind then name.
        Empty list if no members found (e.g. index predates parent_usr support).
    """
    return conn.execute(
        """SELECT name, qualified_name, kind, file_path, line, col AS column,
                  signature, is_definition, is_virtual, is_pure_virtual
           FROM symbols
           WHERE config_hash = ? AND parent_usr = ?
           ORDER BY kind, name""",
        (config_hash, parent_usr),
    ).fetchall()


def get_template_instances(
    conn: sqlite3.Connection,
    config_hash: str,
    template_usr: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Return all instantiated symbols for a given template USR.

    Each row is a full symbol row for an instantiation whose ``template_usr``
    matches the given template.
    """
    return conn.execute(
        """SELECT name, qualified_name, kind, file_path, line, col AS column,
                  signature, is_definition, parent_usr
           FROM symbols
           WHERE config_hash = ? AND template_usr = ?
           ORDER BY file_path, line
           LIMIT ?""",
        (config_hash, template_usr, limit),
    ).fetchall()
