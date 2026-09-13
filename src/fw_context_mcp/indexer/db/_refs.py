"""Reference tracking — refs, indirect call sites, function pointer assignments.

This module handles three layers of cross-reference resolution:

── Layer 1: Direct references (refs table) ──

Every read, write, call, or member access by source code is recorded as a
row in ``refs``.  Queries resolve a symbol name → USR → all reference rows,
joining back through ``symbols`` for caller metadata.

── Layer 2: Indirect call sites (indirect_call_sites table) ──

When code invokes a function through a pointer (``driver.onData(buf, len)``),
the callee is a FIELD_DECL or VAR_DECL, not a FUNCTION_DECL.  The
``indirect_call_sites`` table records WHERE the call happens and WHICH
field/variable was invoked — but not WHICH function is actually called.
That resolution happens in Layer 3.

── Layer 3: Function pointer assignments (fp_assignments table) ──

The ``fp_assignments`` table records every ``field = &function`` assignment.
The linking query in ``find_indirect_targets()`` joins:
  ``fp_assignments.lhs_usr = indirect_call_sites.target_usr``
to answer: "which functions can be called through this function pointer?"

── Reference resolution strategy ──

Name → USR resolution uses a four-tier match:

1. Exact ``name`` match — ``send`` matches bare name.
2. Exact ``qualified_name`` match — ``the Mbed project::ZMODEM_DRIVER::send``.
3. Suffix LIKE on ``qualified_name`` — ``ZMODEM_DRIVER::send`` matches
   ``the Mbed project::ZMODEM_DRIVER::send``.
4. Plain name fallback — the segment after the last ``::``.

For aggregate types (class, struct, enum), references are stored at
member granularity (``MyClass::method``, not just ``MyClass``).  The
query uses USR prefix matching (``to_usr LIKE usr || '@%'``) to include
all member references when the resolved symbol is an aggregate type.

── Phase 3b / 3c fallbacks ──

When the exact USR join in ``find_indirect_targets()`` produces no call
site, two fallback strategies are tried:

**Phase 3b — class hierarchy fallback**: traces the receiver field's
type through the inheritance chain and searches for an ``indirect_call_sites``
row in any method of that class.  This handles C++ template callback
wrappers like ``mbed::Callback<void()>`` where the callback is stored in
an internal field of the receiver class.

**Phase 3c — fn_ptr_type fallback**: matches by function pointer type
string when the exact USR join fails.  This handles cases where the same
function pointer type is assigned through one variable but invoked through
a differently-named variable (e.g., ``.handler = cb`` → ``m_usbevt_handler(...)``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from ._chunking import chunked

log = logging.getLogger(__name__)

__all__ = [
    "count_fp_assignments",
    "count_indirect_call_sites",
    "count_refs",
    "delete_fp_assignments_for_files",
    "delete_indirect_call_sites_for_files",
    "delete_refs_for_files",
    "find_indirect_call_sites",
    "find_indirect_targets",
    "find_refs",
    "insert_fp_assignments_batch",
    "insert_indirect_call_sites_batch",
    "insert_refs_batch",
]


def insert_refs_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert reference rows for the cross-reference / call graph.

    Each row: (config_hash, to_usr, from_file, from_line, from_usr,
               ref_kind, slot_index).  slot_index is an index counted from
    zero — a slot of a vector table from the assembly reader, or an element
    of a positional array initializer from the C indexer — and None for a
    reference that has no position.

    Why INSERT OR IGNORE: the same TU can be indexed multiple times
    (reindex after header change).  OR IGNORE skips duplicates silently
    instead of raising IntegrityError.  Duplicate detection relies on
    the ``idx_refs_unique`` UNIQUE index, which is created during data
    migration AFTER deduplication for performance.
    """
    cur = conn.executemany(
        """INSERT OR IGNORE INTO refs (config_hash, to_usr, from_file, from_line, from_usr, ref_kind, slot_index)
           VALUES (?,?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount




def _delete_by_from_file(
    conn: sqlite3.Connection,
    table: str,
    config_hash: str,
    from_files: Sequence[str],
) -> None:
    """Delete rows of *table* whose ``from_file`` is one of *from_files*.

    Shared by the three reference tables: they all key their rows on
    ``(config_hash, from_file)`` and all carry an index on that pair, so the
    ``IN`` list resolves to index seeks instead of a table scan.

    *table* is never caller-supplied data — the three public wrappers below
    each pass a literal name.  Only ``?`` placeholders are interpolated.
    """
    paths = list(dict.fromkeys(from_files))  # dedupe, keep a stable order
    if not paths:
        return
    for chunk in chunked(paths):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM {table} WHERE config_hash=? AND from_file IN ({placeholders})",
            (config_hash, *chunk),
        )


def delete_refs_for_files(
    conn: sqlite3.Connection,
    config_hash: str,
    from_files: Sequence[str],
) -> None:
    """Delete all refs originating in any of *from_files* under *config_hash*.

    WHY a plural form is needed: a translation unit owns more than its own
    file.  A call inside an inline function in a header is stored with the
    HEADER's path in ``from_file``, so a delete keyed on the TU path alone
    leaves those rows behind.  A call removed from a header then keeps its
    refs row — ``find_callers`` reports a caller that no longer exists — and
    a call that only moved gains a SECOND row, because ``idx_refs_unique``
    includes ``from_line`` and ``insert_refs_batch`` uses ``INSERT OR
    IGNORE``.

    *from_files* must be normalised exactly the way ``insert_refs_batch``
    normalises the paths it writes.  A path built by a different normaliser
    would not match, and the stale row would survive the delete.
    """
    _delete_by_from_file(conn, "refs", config_hash, from_files)


# ---------------------------------------------------------------------------
# Indirect call sites (Phase 2) — function pointer invocation tracking
# ---------------------------------------------------------------------------


def insert_indirect_call_sites_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert indirect call site rows for batch indexing.

    Each row records one function pointer invocation.  Unlike refs, the
    target is a FIELD_DECL/VAR_DECL (the function pointer variable), not
    a FUNCTION_DECL (the actual function called).

    Why no UNIQUE constraint: a single field can be called many times
    from the same line (e.g., inside a loop).  Each invocation is a
    separate call-site row.  Duplicate prevention is not needed here
    because re-indexing cleans the file's entries first via
    ``delete_indirect_call_sites_for_files()``.
    """
    cur = conn.executemany(
        """INSERT INTO indirect_call_sites
           (config_hash, from_file, from_line, from_usr, expr_text, target_usr, target_name, fn_ptr_type)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount


def delete_indirect_call_sites_for_files(
    conn: sqlite3.Connection,
    config_hash: str,
    from_files: Sequence[str],
) -> None:
    """Delete indirect call sites originating in any of *from_files*.

    Same header-ownership problem as :func:`delete_refs_for_files`, with a
    worse failure mode: ``indirect_call_sites`` has NO unique constraint (one
    field can legitimately be invoked many times from a single line), so a
    function pointer call inside an inline header function gains a duplicate
    row on EVERY reindex, without bound.
    """
    _delete_by_from_file(conn, "indirect_call_sites", config_hash, from_files)


def find_indirect_call_sites(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find indirect call sites for a function pointer field or variable.

    Three-tier name resolution:

    1. Match via ``symbols`` table — covers FIELD_DECL and VAR_DECL symbols
       that have a USR in the index.
    2. Fallback to ``target_name`` direct match — covers PARM_DECL symbols
       (function pointer parameters) that are NOT in the symbols table.
       Parameters are excluded from symbols because they have no definition
       USR, but they still appear in indirect_call_sites via the expr_text
       captured during indexing.

    Why this split: a single ``indirect_call_sites.target_usr IN (SELECT...)``
    query would miss PARM_DECL targets.  The ``target_name`` fallback
    ensures parameter-based function pointer calls are found.
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
    """Return total indirect call site count for a build config.

    Returns 0 when the Phase 2 analysis has not been indexed.  Used by
    ``get_active_build()`` to report index coverage.
    """
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


def delete_fp_assignments_for_files(
    conn: sqlite3.Connection,
    config_hash: str,
    from_files: Sequence[str],
) -> None:
    """Delete fp_assignments rows originating in any of *from_files*.

    Same header-ownership problem as :func:`delete_refs_for_files`: a
    ``handler = &fn`` assignment inside an inline function in a header is
    stored under the HEADER's path, not the translation unit's.
    """
    _delete_by_from_file(conn, "fp_assignments", config_hash, from_files)



def find_indirect_targets(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Find functions assigned to a function pointer field/variable/parameter.

    The core join: ``fp_assignments`` LEFT JOIN ``indirect_call_sites``
    ON ``target_usr = lhs_usr``.  When both sides resolve, the result
    shows which function (rhs_usr) is called where (call_file:call_line).

    **When the exact USR join fails** (call_file IS NULL), three fallback
    strategies are applied:

    ── Strategy 1: lhs_usr IS NOT NULL → type-based class hierarchy ──
    Extract the receiver field's type from its signature, look up the
    type's class hierarchy, and search for a ``handler`` method in the
    class or its bases.  This handles C++ template callback wrappers
    like ``mbed::Callback<void()>`` where the callback is stored in an
    internal field (``TimeoutBase::_function``) of the class, not the
    field that was attached to (``TimeoutBase::_timeout``).

    ── Strategy 1b: lhs_usr IS NULL → resolve field by name in parent class ──
    When the callee is template-obscured and has no USR, find the
    enclosing function's parent class, enumerate its fields, and try
    each field's type as a candidate for the class hierarchy fallback.

    ── Strategy 2 (Phase 3c): fn_ptr_type matching ──
    When both type-hierarchy strategies fail, match by function pointer
    type string.  This handles cases where the same fn_ptr_type is
    assigned through one struct field but invoked through a differently
    named variable (e.g., ``config.handler = cb`` stored via one field
    but ``m_usbevt_handler(...)`` called through another).  The match
    is logged as ``_note`` because it is less certain than USR resolution.

    Why multi-tier fallback: each strategy covers a different C/C++
    callback pattern.  No single strategy works for all patterns because
    the indexer captures different levels of detail depending on whether
    libclang can resolve the template parameters at parse time.
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

    Returns ``(sql_fragment, params)``:

    - ``None`` → ``""`` (no filter) — returns all reference kinds.
    - ``"call"`` → ``"AND r.ref_kind = ?"`` — single-value filter.
    - ``["call", "indirect"]`` → ``"AND r.ref_kind IN (?, ?)"`` — multi-value.
    - ``[]`` (empty list) → ``"AND 1=0"`` — intentionally returns no rows
      (caller asked for zero allowed kinds).

    Why ``1=0`` instead of returning ``""`` for an empty list: returning
    all rows when the caller explicitly wants zero types would be a silent
    correctness bug.  ``1=0`` is a constant false condition that SQLite's
    query planner recognizes and short-circuits.
    """
    if ref_kind is None:
        return "", []
    if isinstance(ref_kind, list):
        if len(ref_kind) == 0:
            return "AND 1=0", []
        placeholders = ", ".join("?" * len(ref_kind))
        return f"AND r.ref_kind IN ({placeholders})", list(ref_kind)
    return "AND r.ref_kind = ?", [ref_kind]


def count_refs_for_symbol(
    conn: sqlite3.Connection,
    config_hash: str,
    usr: str,
    kind: str,
    ref_kind: str | list[str] | None = None,
) -> int:
    """Count every reference of one symbol, ignoring any page bound.

    A paged answer must say how much it does not show, thus the count runs
    on the same conditions as the page and only the LIMIT differs.
    """
    kind_filter, kind_params = _build_kind_filter(ref_kind)
    if kind in ("class", "struct", "enum"):
        # The page groups its rows, thus the count must group them too.
        row = conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT 1 FROM refs r
                    LEFT JOIN symbols caller
                      ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                    WHERE r.config_hash = ? AND r.to_usr LIKE ? {kind_filter}
                    GROUP BY caller.qualified_name, r.from_file, r.from_line)""",
            [config_hash, usr + "@%", *kind_params],
        ).fetchone()
        return row[0] if row else 0
    row = conn.execute(
        f"""SELECT COUNT(*) FROM refs r
            WHERE r.config_hash = ? AND r.to_usr = ? {kind_filter}""",
        [config_hash, usr, *kind_params],
    ).fetchone()
    return row[0] if row else 0


def refs_for_symbol(
    conn: sqlite3.Connection,
    config_hash: str,
    usr: str,
    kind: str,
    ref_kind: str | list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Return the reference rows of ONE symbol, named by its USR.

    This half of ``find_refs`` does no name resolution.  A caller that has
    already chosen a symbol must use it, so that the choice is made once.
    ``get_symbol_context`` used to call ``find_refs`` with the NAME after
    ``_lookup_definition`` had already picked a symbol from that same name,
    and the two resolvers ranked differently: measured on one firmware
    index, one answer held the body of one symbol and the call sites of
    another.

    **Aggregate type handling**: for a class, struct or enum the references
    sit at member granularity (``MyClass::method``), thus the query matches
    a USR prefix (``to_usr LIKE usr || '@%'``) to reach every member.
    GROUP BY deduplicates when several members are referenced on one line,
    as in ``obj.a = obj.b``.

    ``(from_file, from_line)`` orders the rows and no two references of one
    symbol share both, thus *offset* walks the answer without repeating a
    row or stepping over one.
    """
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

    if kind in ("class", "struct", "enum"):
        params: list = [config_hash, usr + "@%"] + kind_params + [limit, offset]
        return conn.execute(
            f"""{_SELECT}
                  AND r.to_usr LIKE ? {kind_filter}
                GROUP BY caller.qualified_name, r.from_file, r.from_line
                ORDER BY r.from_file, r.from_line
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()

    params = [config_hash, usr] + kind_params + [limit, offset]
    return conn.execute(
        f"""{_SELECT}
              AND r.to_usr = ? {kind_filter}
            ORDER BY r.from_file, r.from_line
            LIMIT ? OFFSET ?""",
        params,
    ).fetchall()


def find_refs_with_candidates(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    ref_kind: str | list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list, list, int]:
    """Give ``(rows, candidates, total)`` for one page of the references.

    Name resolution goes through :func:`~._resolve.resolve_candidates`,
    which ranks a match on its specificity: an exact ``qualified_name``
    first, then the bare ``name``, then a suffix of ``qualified_name``,
    then the tail after the last ``::``.  Users typically start typing at
    the class level and not at the namespace level, thus the suffix tier
    keeps a partial query useful.

    When the name matches SEVERAL symbols of the best rank — two classes
    with a method of one name — the rows of all of them come back, and
    each row carries ``target_usr`` and ``target_qualified_name`` to say
    which symbol it belongs to.  The earlier version picked one of them
    with ``LIMIT 1`` and reported nothing, so a caller could not tell that
    it was reading half the answer.

    *candidates* holds EVERY symbol that the name matched, and not only the
    ones that have a reference.  A caller that reports the ambiguity needs
    that number: measured on one firmware index, ``getMember`` matched 18
    symbols and 5 of them had callers, and a notice built from the rows
    alone told the reader that the name matched 5.

    *total* counts the references of every matched symbol, thus a paged
    answer can say how much it does not show.

    *limit* bounds the page.  The rows are taken one per symbol in turn,
    thus every symbol appears before any symbol gets a second row, and a
    symbol with many references cannot crowd out a symbol with few.

    WHY *offset* skips the MERGED list and not each symbol's own: giving
    the offset to every symbol query would walk each symbol past its own
    rows, and the interleave would then step over the rows between.  Page 2
    of a three-symbol answer would jump from row 4 to row 13 and never show
    the eight between.  Each symbol therefore fetches ``offset + limit``
    rows, the merge runs, and the slice comes last — which is what a plain
    SQL OFFSET does anyway.
    """
    from ._resolve import candidate_labels, resolve_candidates

    candidates = resolve_candidates(conn, config_hash, name)
    if not candidates:
        return [], [], 0

    if len(candidates) == 1:
        only = candidates[0]
        rows = refs_for_symbol(
            conn, config_hash, only.usr, only.kind,
            ref_kind=ref_kind, limit=limit, offset=offset,
        )
        total = count_refs_for_symbol(
            conn, config_hash, only.usr, only.kind, ref_kind=ref_kind
        )
        return rows, candidates, total

    # sqlite3.Row takes no new key, thus each row is wrapped rather than
    # copied into a dict.  A dict here would change what every caller of
    # this function receives, and only the ambiguous answer needs the tag.
    reach = offset + limit
    # The label separates two candidates that share a qualified name, which
    # an overload always does.  Without it two rows of one call site read
    # alike and a reader cannot tell which symbol each belongs to.
    labels = candidate_labels(candidates)
    per_symbol: list[list] = []
    total = 0
    for candidate, label in zip(candidates, labels, strict=True):
        rows = refs_for_symbol(
            conn, config_hash, candidate.usr, candidate.kind,
            ref_kind=ref_kind, limit=reach,
        )
        per_symbol.append([_TaggedRef(row, candidate, label) for row in rows])
        total += count_refs_for_symbol(
            conn, config_hash, candidate.usr, candidate.kind, ref_kind=ref_kind
        )

    merged: list = []
    for depth in range(max((len(rows) for rows in per_symbol), default=0)):
        for rows in per_symbol:
            if depth < len(rows):
                merged.append(rows[depth])
                if len(merged) >= reach:
                    return merged[offset:reach], candidates, total
    return merged[offset:reach], candidates, total


def find_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    ref_kind: str | list[str] | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find all references to a symbol by name or partial qualified name.

    A view on :func:`find_refs_with_candidates` for a caller that needs the
    rows only.  See that function for the resolution rules and for what an
    ambiguous name does to the answer.
    """
    rows, _candidates, _total = find_refs_with_candidates(
        conn, config_hash, name, ref_kind=ref_kind, limit=limit
    )
    return rows


class _TaggedRef:
    """A reference row that also knows which same-name symbol it belongs to.

    ``sqlite3.Row`` cannot take a new key, and turning every row into a
    dict here would change what each caller of ``find_refs`` receives.
    This wrapper forwards the mapping access and adds the two target keys.
    """

    __slots__ = ("_row", "_candidate", "_label")

    def __init__(self, row: sqlite3.Row, candidate, label: str = "") -> None:
        self._row = row
        self._candidate = candidate
        # The label tells two same-name symbols apart, which the qualified
        # name alone cannot do for an overload: measured on one project,
        # ``CoilData::set`` has five overloads, and two rows of one call
        # site then read alike and differed only in a USR.
        self._label = label or candidate.qualified_name

    def __getitem__(self, key):
        if key == "target_usr":
            return self._candidate.usr
        if key == "target_qualified_name":
            return self._label
        return self._row[key]

    def keys(self) -> list[str]:
        return [*self._row.keys(), "target_usr", "target_qualified_name"]


def count_refs(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total reference count for a build config.

    Returns 0 when refs have not been indexed.  Used by
    ``get_active_build()`` to display index statistics and by the
    indexer to decide whether reference-based tools (find_callers,
    find_hotspots) are available.
    """
    return conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (config_hash,)).fetchone()[0]
