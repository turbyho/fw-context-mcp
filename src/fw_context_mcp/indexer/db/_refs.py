"""Reference tracking (refs, indirect_call_sites, fp_assignments)."""

from __future__ import annotations

import sqlite3


def insert_refs_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert reference rows for the cross-reference / call graph.

    Each row: (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
    Returns number of rows inserted.
    """
    cur = conn.executemany(
        """INSERT INTO refs (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount


def delete_refs_for_file(conn: sqlite3.Connection, config_hash: str, from_file: str) -> None:
    """Delete refs for *from_file*.
    Path normalization depends on _rel() and _normalize_file_path() staying
    consistent — any future change to normalization would leave orphaned rows."""
    conn.execute(
        "DELETE FROM refs WHERE config_hash=? AND from_file=?",
        (config_hash, from_file),
    )


# ---------------------------------------------------------------------------
# Indirect call sites (Phase 2) — function pointer invocation tracking
# ---------------------------------------------------------------------------


def insert_indirect_call_sites_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert indirect call site rows.

    Each row: (config_hash, from_file, from_line, from_usr, expr_text,
    target_usr, target_name, fn_ptr_type).
    Returns number of rows inserted.
    """
    cur = conn.executemany(
        """INSERT INTO indirect_call_sites
           (config_hash, from_file, from_line, from_usr, expr_text, target_usr, target_name, fn_ptr_type)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount


def delete_indirect_call_sites_for_file(conn: sqlite3.Connection, config_hash: str, from_file: str) -> None:
    """Delete all indirect call sites originating in a given file (for incremental reindex)."""
    conn.execute(
        "DELETE FROM indirect_call_sites WHERE config_hash=? AND from_file=?",
        (config_hash, from_file),
    )


def find_indirect_call_sites(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find indirect call sites for a function pointer field or variable by name.

    Three-tier name resolution (same pattern as ``find_refs``): exact name,
    exact qualified_name, suffix LIKE.
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    return conn.execute(
        """SELECT ics.*,
                  caller.name            AS caller_name,
                  caller.qualified_name  AS caller_qname,
                  caller.kind            AS caller_kind,
                  caller.file_path       AS caller_file,
                  caller.is_project      AS caller_is_project
           FROM indirect_call_sites ics
           LEFT JOIN symbols caller
             ON caller.config_hash = ics.config_hash AND caller.usr = ics.from_usr
           WHERE ics.config_hash = ?
             AND (
                 -- Match via indexed symbols (covers FIELD_DECL, VAR_DECL)
                 ics.target_usr IN (
                     SELECT usr FROM symbols
                     WHERE config_hash = ?
                       AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\')
                 )
                 OR
                 -- Fallback: direct name match (covers PARM_DECL which are
                 -- not in the symbols table)
                 ics.target_name = ?
             )
           ORDER BY ics.from_file, ics.from_line
           LIMIT ?""",
        (config_hash, config_hash, name, name, suffix_pattern, name, limit),
    ).fetchall()


def count_indirect_call_sites(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total indirect call site count (0 when not indexed)."""
    return conn.execute(
        "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash=?",
        (config_hash,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Function pointer assignment helpers — Phase 3 linking
# ---------------------------------------------------------------------------


def insert_fp_assignments_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert function pointer assignment records.

    Each row: (config_hash, from_file, from_line, lhs_usr, lhs_name,
    rhs_usr, rhs_name, fn_ptr_type, method, from_usr).
    Uses OR IGNORE to handle re-indexing of the same file.
    """
    cur = conn.executemany(
        """INSERT OR IGNORE INTO fp_assignments
           (config_hash, from_file, from_line, lhs_usr, lhs_name,
            rhs_usr, rhs_name, fn_ptr_type, method, from_usr)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return cur.rowcount


def delete_fp_assignments_for_file(
    conn: sqlite3.Connection,
    config_hash: str,
    from_file: str,
) -> None:
    """Delete fp_assignments rows for *from_file* under *config_hash*.

    Called before re-indexing a TU to remove stale entries."""
    conn.execute(
        "DELETE FROM fp_assignments WHERE config_hash = ? AND from_file = ?",
        (config_hash, from_file),
    )


def find_indirect_targets(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find functions assigned to a function pointer field/variable/parameter.

    Joins ``fp_assignments`` with ``indirect_call_sites`` on
    ``lhs_usr = target_usr`` to link assignment sites to call sites.
    Three-tier name resolution: exact name, exact qualified, suffix LIKE.
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    return conn.execute(
        """SELECT fpa.rhs_usr, fpa.rhs_name, fpa.fn_ptr_type, fpa.method,
                  fpa.from_file AS assign_file, fpa.from_line AS assign_line,
                  fpa.from_usr AS assign_from_usr,
                  ics.from_file AS call_file, ics.from_line AS call_line,
                  ics.expr_text AS call_expr_text,
                  rhs_sym.qualified_name AS rhs_qname,
                  caller_sym.name AS assign_caller,
                  caller_sym.file_path AS caller_file,
                  caller_sym.is_project AS caller_is_project
           FROM fp_assignments fpa
           LEFT JOIN indirect_call_sites ics
             ON ics.target_usr = fpa.lhs_usr
            AND ics.config_hash = fpa.config_hash
           LEFT JOIN symbols rhs_sym
             ON rhs_sym.usr = fpa.rhs_usr
            AND rhs_sym.config_hash = fpa.config_hash
           LEFT JOIN symbols caller_sym
             ON caller_sym.usr = fpa.from_usr
            AND caller_sym.config_hash = fpa.config_hash
           WHERE fpa.config_hash = ?
             AND (
                 -- Match via indexed symbols (covers FIELD_DECL, VAR_DECL)
                 fpa.lhs_usr IN (
                     SELECT usr FROM symbols
                     WHERE config_hash = ?
                       AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE ?)
                 )
                 OR
                 -- Fallback: direct name match (covers PARM_DECL which are
                 -- not in the symbols table)
                 fpa.lhs_name = ?
             )
           ORDER BY fpa.from_file, fpa.from_line
           LIMIT ?""",
        (config_hash, config_hash, name, name, suffix_pattern, "\\", name, limit),
    ).fetchall()


def count_fp_assignments(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total fp_assignment count (0 when not indexed)."""
    return conn.execute(
        "SELECT COUNT(*) FROM fp_assignments WHERE config_hash=?",
        (config_hash,),
    ).fetchone()[0]


def _build_kind_filter(ref_kind: str | list[str] | None) -> tuple[str, list[str]]:
    """Build a SQL kind-filter clause and parameter list for ref_kind.

    Returns ``(sql_fragment, params)`` — the fragment is empty when
    *ref_kind* is None, a single ``= ?`` for a string, or an ``IN (...)``
    clause for a list.
    """
    if ref_kind is None:
        return "", []
    if isinstance(ref_kind, list):
        if len(ref_kind) == 0:
            return "AND 1=0", []
        placeholders = ", ".join("?" * len(ref_kind))
        return f"AND r.ref_kind IN ({placeholders})", list(ref_kind)
    return "AND r.ref_kind = ?", [ref_kind]


def find_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    ref_kind: str | list[str] | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find references to a symbol by name.

    Resolves the target name → its USR(s) via symbols, then joins refs.to_usr.
    Each result carries the referencing location and, when known, the enclosing
    caller symbol (joined from refs.from_usr → symbols.usr).

    Name resolution uses a three-tier match so partially-qualified names work:
    1. Exact ``name`` match (e.g. ``send`` matches bare name ``send``)
    2. Exact ``qualified_name`` match (``zbox::ZMODEM_DRIVER::send``)
    3. Suffix LIKE on ``qualified_name`` (``ZMODEM_DRIVER::send``
       matches ``zbox::ZMODEM_DRIVER::send``)

    For classes, structs, and enums the index stores references at member-level
    granularity (e.g. ``ZUART::get``, not ``ZUART`` itself).  When the resolved
    symbol is an aggregate type, the query uses USR prefix matching
    (``to_usr LIKE usr || '@%'``) so all member references are included.

    *ref_kind* can be a single value, a list (→ ``IN`` clause), or None
    (no filter).
    """
    # ── Resolve name → USR and kind ──────────────────────────────────────
    # Three-tier match: exact name, exact qualified_name, suffix LIKE
    # Escape % and _ for the LIKE pattern
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    symbol = conn.execute(
        """SELECT usr, kind FROM symbols
           WHERE config_hash = ?
             AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\')
           ORDER BY is_definition DESC, qualified_name = ? DESC
           LIMIT 1""",
        (config_hash, name, name, suffix_pattern, name),
    ).fetchone()

    if not symbol:
        return []

    is_aggregate = symbol["kind"] in ("class", "struct", "enum")
    kind_filter, kind_params = _build_kind_filter(ref_kind)

    _SELECT = """SELECT r.from_file, r.from_line, r.ref_kind,
                        r.from_usr,
                        caller.name           AS caller_name,
                        caller.qualified_name AS caller_qname,
                        caller.kind           AS caller_kind,
                        caller.file_path      AS caller_file,
                        caller.is_project     AS caller_is_project
                 FROM refs r
                 LEFT JOIN symbols caller
                   ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                 WHERE r.config_hash = ?"""

    if is_aggregate:
        usr_prefix = symbol["usr"] + "@%"
        params: list = [config_hash, usr_prefix] + kind_params + [limit]
        return conn.execute(
            f"""{_SELECT}
                  AND r.to_usr LIKE ? {kind_filter}
                GROUP BY caller.qualified_name, r.from_file, r.from_line
                ORDER BY r.from_file, r.from_line
                LIMIT ?""",
            params,
        ).fetchall()

    # ── Exact USR match for functions, methods, variables, etc. ──────────
    # Same three-tier name resolution: exact name, exact qualified_name, suffix LIKE
    params = [config_hash, config_hash, name, name, suffix_pattern] + kind_params + [limit]
    return conn.execute(
        f"""{_SELECT}
              AND r.to_usr IN (
                  SELECT usr FROM symbols
                  WHERE config_hash = ?
                    AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\')
              ) {kind_filter}
            ORDER BY r.from_file, r.from_line
            LIMIT ?""",
        params,
    ).fetchall()


def count_refs(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total reference count for a build config (0 when refs not indexed)."""
    return conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (config_hash,)).fetchone()[0]
