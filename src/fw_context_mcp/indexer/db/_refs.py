"""Reference tracking (refs, indirect_call_sites, fp_assignments)."""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

__all__ = [
    "count_fp_assignments",
    "count_indirect_call_sites",
    "count_refs",
    "delete_fp_assignments_for_file",
    "delete_indirect_call_sites_for_file",
    "delete_refs_for_file",
    "find_indirect_call_sites",
    "find_indirect_targets",
    "find_refs",
    "insert_fp_assignments_batch",
    "insert_indirect_call_sites_batch",
    "insert_refs_batch",
]


def insert_refs_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert reference rows for the cross-reference / call graph.

    Each row: (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
    Returns number of rows inserted.
    """
    cur = conn.executemany(
        """INSERT OR IGNORE INTO refs (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
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
) -> list[dict[str, object]]:
    """Find functions assigned to a function pointer field/variable/parameter.

    Joins ``fp_assignments`` with ``indirect_call_sites`` on
    ``lhs_usr = target_usr`` to link assignment sites to call sites.
    When ``lhs_usr`` is empty (template-obscured callee), ``call_file``
    will be ``NULL`` — the callback is stored for async invocation through
    a different USR (e.g. mbed::Timeout internal fields).
    Three-tier name resolution: exact name, exact qualified, suffix LIKE.

    **Type-based fallback (Phase 3b):** When the exact USR join fails
    (call_file IS NULL, typical for C++ template callback wrappers like
    ``mbed::Callback<void()>``), traces the receiver field's *type* to
    its class hierarchy and searches for ``indirect_call_sites`` in any
    method of that class.  This correctly links ``_timeout.attach(cb)``
    → ``TimeoutBase::handler()`` for mbed-os Timer/Ticker callbacks,
    and analogous patterns in other C++ frameworks.  C patterns
    (Zephyr ``k_work_init``, FreeRTOS ``xTimerStart``) are unaffected
    — their function pointers are stored directly (no templates) so the
    exact USR join already works.
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    rows = conn.execute(
        """SELECT fpa.rhs_usr, fpa.rhs_name, fpa.fn_ptr_type, fpa.method,
                  fpa.lhs_usr, fpa.lhs_name,
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
                 OR
                 -- Fallback: target function name (covers callback wrapper
                 -- assignments where neither lhs_usr nor lhs_name match the
                 -- searched field name)
                 fpa.rhs_name = ?
                 OR fpa.rhs_name LIKE ?
             )
           ORDER BY fpa.from_file, fpa.from_line
           LIMIT ?""",
         (config_hash, config_hash, name, name, suffix_pattern, "\\", name, name,
          f"%{esc_name}%", limit),
    ).fetchall()

    # ── Type-based fallback (Phase 3b): when call_file IS NULL, trace the
    #     receiver field type → class → methods → call sites.
    #     This handles C++ template callback wrappers (mbed::Callback<T>,
    #     std::function<T>) where the callback is stored in an internal
    #     field of the receiver class, not the field it was attached to.
    #     Also handles lhs_usr IS NULL (template-obscured callee) — resolves
    #     the field by name within the enclosing function's parent class.
    _fixed_rows: list[dict] = []
    for r in rows:
        if r["call_file"] is not None:
            _fixed_rows.append(dict(r))
            continue
        lhs_usr = r["lhs_usr"]
        lhs_name = r["lhs_name"] or ""
        assign_from_usr = r["assign_from_usr"]

        type_name: str | None = None
        _field_names: list[tuple[str, str]] = []  # (type_name, field_name)
        if lhs_usr:
            type_sym = conn.execute(
                """SELECT signature FROM symbols
                   WHERE config_hash = ? AND usr = ?""",
                (config_hash, lhs_usr),
            ).fetchone()
            if type_sym:
                sig = type_sym["signature"] or ""
                type_name = sig.split(" ")[0].strip() if " " in sig else ""
        elif lhs_name and assign_from_usr:
            parent_row = conn.execute(
                """SELECT parent_usr FROM symbols
                   WHERE config_hash = ? AND usr = ?""",
                (config_hash, assign_from_usr),
            ).fetchone()
            if parent_row:
                parent_usr_val = parent_row["parent_usr"]
                if parent_usr_val:
                    field_rows = conn.execute(
                        """SELECT signature, name FROM symbols
                           WHERE config_hash = ? AND parent_usr = ?
                             AND kind = 'field'
                             AND signature != ''
                           LIMIT 20""",
                        (config_hash, parent_usr_val),
                    ).fetchall()
                    for fr in field_rows:
                        sig = fr["signature"] or ""
                        candidate = sig.split(" ")[0].strip() if " " in sig else ""
                        if candidate:
                            _field_names.append((candidate, fr["name"] or ""))

        if not type_name and not _field_names:
            r_dict = dict(r)
            _fixed_rows.append(r_dict)
            continue

        # Try single type_name first, then candidate field types
        candidates = [(type_name, type_name)] if type_name else _field_names

        resolved = False
        for candidate_type, _candidate_field in candidates:
            esc_ct = candidate_type.replace("%", "\\%").replace("_", "\\_")
            class_row = conn.execute(
                """SELECT usr FROM symbols
                   WHERE config_hash = ? AND kind IN ('class', 'struct')
                     AND (qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\'
                          OR name = ?)
                   ORDER BY is_definition DESC LIMIT 1""",
                (config_hash, candidate_type, f"%::{esc_ct}", candidate_type),
            ).fetchone()
            if not class_row:
                continue
            handler_found: tuple[str, int] | None = None
            _seen2: set[str] = set()
            _queue2: list[tuple[str, int]] = [(class_row["usr"], 0)]
            _MAX_CLASS_DEPTH = 20
            try:
                while _queue2 and handler_found is None:
                    cur, depth = _queue2.pop(0)
                    if cur in _seen2:
                        continue
                    if depth >= _MAX_CLASS_DEPTH:
                        continue
                    _seen2.add(cur)
                    h = conn.execute(
                        """SELECT file_path, line FROM symbols
                           WHERE config_hash = ? AND parent_usr = ?
                             AND kind IN ('method', 'function')
                             AND is_definition = 1
                             AND (name = 'handler' OR name LIKE '%handler%')
                           LIMIT 1""",
                        (config_hash, cur),
                    ).fetchone()
                    if h:
                        handler_found = (h["file_path"], h["line"])
                        break
                    bases = conn.execute(
                        """SELECT base_usr FROM inheritance
                           WHERE config_hash = ? AND derived_usr = ?""",
                        (config_hash, cur),
                    ).fetchall()
                    for b in bases:
                        if b["base_usr"] and b["base_usr"] not in _seen2:
                            _queue2.append((b["base_usr"], depth + 1))
            except sqlite3.Error:
                log.debug("find_indirect_targets: BFS traversal failed", exc_info=True)
                break
            if handler_found:
                r_dict = dict(r)
                r_dict["call_file"] = handler_found[0] or None
                r_dict["call_line"] = handler_found[1] or None
                r_dict["call_expr_text"] = "<inferred via class hierarchy>"
                _fixed_rows.append(r_dict)
                resolved = True
                break

        if not resolved:
            r_dict = dict(r)
            # ── Phase 3c: fn_ptr_type fallback ──────────────────────────
            # When the exact USR join fails AND the type-hierarchy fallback
            # fails, try matching by function pointer type string.
            # This handles cases where the same fn_ptr_type is assigned
            # through a struct field but invoked through a differently-named
            # variable (e.g., config.handler = cb → m_usbevt_handler(…)).
            fpa_fn_ptr_type = r_dict.get("fn_ptr_type", "") or ""
            if fpa_fn_ptr_type.strip():
                ics_row = conn.execute(
                    """SELECT from_file, from_line, expr_text
                       FROM indirect_call_sites
                       WHERE config_hash = ?
                         AND fn_ptr_type = ?
                       LIMIT 1""",
                    (config_hash, fpa_fn_ptr_type.strip()),
                ).fetchone()
                if ics_row:
                    r_dict["call_file"] = ics_row["from_file"] or None
                    r_dict["call_line"] = ics_row["from_line"] or None
                    r_dict["call_expr_text"] = (
                        ics_row["expr_text"] or "<inferred via fn_ptr_type>"
                    )
                    r_dict["_note"] = (
                         "Call site resolved via fn_ptr_type matching "
                         "(different LHS/target USR, same function pointer type)."
                     )
            _fixed_rows.append(r_dict)

    return _fixed_rows

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
    # Four-tier match: exact name, exact qualified_name, suffix LIKE, plain name
    # Escape % and _ for the LIKE pattern
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    plain_name = name.rsplit("::", 1)[-1] if "::" in name else name
    symbol = conn.execute(
        """SELECT usr, kind FROM symbols
           WHERE config_hash = ?
             AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\'
                  OR name = ?)
           ORDER BY is_definition DESC, qualified_name = ? DESC
           LIMIT 1""",
        (config_hash, name, name, suffix_pattern, plain_name, name),
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
    # Same four-tier name resolution: exact name, exact qualified_name, suffix LIKE, plain name
    params = [config_hash, config_hash, name, name, suffix_pattern, plain_name] + kind_params + [limit]
    return conn.execute(
        f"""{_SELECT}
              AND r.to_usr IN (
                  SELECT usr FROM symbols
                  WHERE config_hash = ?
                    AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\'
                         OR name = ?)
              ) {kind_filter}
            ORDER BY r.from_file, r.from_line
            LIMIT ?""",
        params,
    ).fetchall()


def count_refs(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total reference count for a build config (0 when refs not indexed)."""
    return conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (config_hash,)).fetchone()[0]
