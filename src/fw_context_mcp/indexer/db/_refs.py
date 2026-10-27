"""Reference tracking — the rows of the ``refs`` table.

Every read, write, call, or member access by source code is recorded as a
row in ``refs``.  Queries resolve a symbol name → USR → all reference rows,
joining back through ``symbols`` for caller metadata.

The indirect calls are not here: the slot facts (``indexer/_slots.py``)
record them, and ``_indirect.py`` reads them.

── Reference resolution strategy ──

Name → USR resolution uses a four-tier match:

1. Exact ``name`` match — ``send`` matches bare name.
2. Exact ``qualified_name`` match — ``app::ModemDriver::send``.
3. Suffix LIKE on ``qualified_name`` — ``ModemDriver::send`` matches
   ``app::ModemDriver::send``.
4. Plain name fallback — the segment after the last ``::``.

For aggregate types (class, struct, enum), references are stored at
member granularity (``MyClass::method``, not just ``MyClass``).  The
query takes the USR RANGE of the members to include all of them when the
resolved symbol is an aggregate type — see ``_member_usr_range`` for why
a range and not a LIKE prefix.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from ._chunking import chunked

log = logging.getLogger(__name__)

__all__ = [
    "count_refs",
    "delete_refs_for_files",
    "insert_refs_batch",
    "link_ref_producers",
    "release_ref_producer",
]


def insert_refs_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert reference rows for the cross-reference / call graph.

    Each row: (config_hash, to_usr, from_file, from_line, from_usr,
               ref_kind, slot_index, from_col, origins).  The last two came
    later, thus the first seven keep the order of an older row.  slot_index is an
    index counted from zero — a slot of a vector table from the assembly
    reader, or an element of a positional array initializer from the C
    indexer — the IRQ number of a runtime vector, or None for a reference
    that has no position.  origins is a set of ``models.RefOrigin`` bits.

    One row is one reference site of one caller: ``idx_refs_site`` is
    unique on the target, the file, the line, the column, the kind, the
    slot and the caller (``ifnull`` of the two nullable columns, because a
    unique index reads two NULLs as different values).  A row for a site
    that is already there does not add a second row: its origins join the
    origins of the row.  WHY: one site can be found by more than one
    analysis (the AST walk, a scan of the tokens, the assembly reader), and
    each of them is a fact about the edge, while a second row would count
    one call twice.  WHY the caller is in the key: a macro that defines
    several functions on one line gives each of them a call at one place.
    """
    cur = conn.executemany(
        """INSERT INTO refs
               (config_hash, to_usr, from_file, from_line, from_usr, ref_kind, slot_index, from_col, origins)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, to_usr, from_file, from_line, from_col, ref_kind,
                       ifnull(slot_index, -1), ifnull(from_usr, ''))
           DO UPDATE SET origins = refs.origins | excluded.origins""",
        rows,
    )
    return cur.rowcount




# The site of a row, as the key of ``idx_refs_site`` compares it.  The
# parameters are those of a row of ``insert_refs_batch``, see _site_params.
_SITE_MATCH = (
    "config_hash = ? AND to_usr = ? AND from_file = ? AND from_line = ? AND from_col = ? "
    "AND ref_kind = ? AND ifnull(slot_index, -1) = ifnull(?, -1) AND ifnull(from_usr, '') = ifnull(?, '')"
)


def _site_params(row: tuple) -> tuple:
    """Give the parameters of ``_SITE_MATCH`` for a row of ``insert_refs_batch``."""
    config_hash, to_usr, from_file, from_line, from_usr, ref_kind, slot_index, from_col, _origins = row
    return (config_hash, to_usr, from_file, from_line, from_col, ref_kind, slot_index, from_usr)


def link_ref_producers(conn: sqlite3.Connection, rows: Sequence[tuple], unit: tuple[str, str]) -> None:
    """Record *unit* as a producer of each row of *rows* that has the INSTANTIATION bit.

    *rows* are rows of ``insert_refs_batch`` that are already stored; *unit*
    is ``(normalized path, flags_hash)`` of the listing that parsed them.
    See the table comment of ``ref_producers`` for why.
    """
    from ..models import RefOrigin

    links = [
        (unit[0], unit[1], *_site_params(row))
        for row in rows
        if row[8] & RefOrigin.INSTANTIATION
    ]
    if links:
        conn.executemany(
            "INSERT OR IGNORE INTO ref_producers (ref_id, config_hash, unit_path, unit_flags_hash) "
            f"SELECT id, config_hash, ?, ? FROM refs WHERE {_SITE_MATCH}",  # noqa: S608 — a constant
            links,
        )


def release_ref_producer(conn: sqlite3.Connection, config_hash: str, unit: tuple[str, str]) -> None:
    """Remove *unit* as a producer: before its new parse, or when it left the build.

    A row that no unit produces any more loses the INSTANTIATION bit, and a
    row with no bit left goes.  Only the rows of *unit* are read, thus the
    cost follows the edges of that unit and not the size of the build.
    """
    from ..models import RefOrigin

    ref_ids = [
        row[0] for row in conn.execute(
            "SELECT ref_id FROM ref_producers WHERE config_hash = ? AND unit_path = ? AND unit_flags_hash = ?",
            (config_hash, unit[0], unit[1]),
        )
    ]
    if not ref_ids:
        return
    conn.execute(
        "DELETE FROM ref_producers WHERE config_hash = ? AND unit_path = ? AND unit_flags_hash = ?",
        (config_hash, unit[0], unit[1]),
    )
    bit = int(RefOrigin.INSTANTIATION)
    orphan = "NOT EXISTS (SELECT 1 FROM ref_producers p WHERE p.ref_id = refs.id)"
    for chunk in chunked(ref_ids):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM refs WHERE id IN ({placeholders}) AND origins = ? AND {orphan}",  # noqa: S608
            (*chunk, bit),
        )
        conn.execute(
            f"UPDATE refs SET origins = origins & ~? WHERE id IN ({placeholders}) AND {orphan}",  # noqa: S608
            (bit, *chunk),
        )


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
    *,
    keep_instance_edges: bool = False,
) -> None:
    """Delete all refs originating in any of *from_files* under *config_hash*.

    WHY a plural form is needed: a translation unit owns more than its own
    file.  A call inside an inline function in a header is stored with the
    HEADER's path in ``from_file``, so a delete keyed on the TU path alone
    leaves those rows behind.  A call removed from a header then keeps its
    refs row — ``find_callers`` reports a caller that no longer exists — and
    a call that only moved gains a SECOND row, because ``idx_refs_site``
    includes ``from_line`` and ``from_col``.

    *from_files* must be normalised exactly the way ``insert_refs_batch``
    normalises the paths it writes.  A path built by a different normaliser
    would not match, and the stale row would survive the delete.

    *keep_instance_edges*: the owner of the files is parsed again.  A row
    that a unit still produces as the edge of a template instance stays,
    with the INSTANTIATION bit only: the other bits came from the parse of
    the owner, which writes them again.  WHY: the producing unit is not
    parsed again, thus the edge would be lost (see ``ref_producers``).  A
    file that left the build loses each row.
    """
    if not keep_instance_edges:
        _delete_by_from_file(conn, "refs", config_hash, from_files)
        return
    from ..models import RefOrigin

    paths = list(dict.fromkeys(from_files))
    produced = "EXISTS (SELECT 1 FROM ref_producers p WHERE p.ref_id = refs.id)"
    for chunk in chunked(paths):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE refs SET origins = ? WHERE config_hash = ? AND from_file IN ({placeholders}) "  # noqa: S608
            f"AND {produced}",
            (int(RefOrigin.INSTANTIATION), config_hash, *chunk),
        )
        conn.execute(
            f"DELETE FROM refs WHERE config_hash = ? AND from_file IN ({placeholders}) "  # noqa: S608
            f"AND NOT {produced}",
            (config_hash, *chunk),
        )


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


#: The kinds whose references sit at MEMBER granularity.  A reference to
#: ``MyClass`` is recorded against ``MyClass::field``, not against the class,
#: thus these kinds reach their references through the USR of each member.
_MEMBER_GRANULAR_KINDS = ("class", "struct", "enum")


def _member_usr_range(usr: str) -> tuple[str, str]:
    """Give the half-open range ``[lo, hi)`` that holds every member of *usr*.

    libclang writes the USR of a member as ``<owner>@<member>``, thus the
    members of one aggregate sit in one unbroken stretch of a BINARY order.
    ``hi`` is the owner followed by the character after ``@`` (``A``), which
    is the first string that no member can reach.

    WHY a range and not ``LIKE usr || '@%'``, which this replaced.  The LIKE
    was wrong twice and slow once:

    * No ESCAPE clause, and ``_`` is the single-character wildcard of LIKE.
      A USR carries one whenever the C identifier does, which is most of the
      time.  Measured on one firmware index, the members of ``my_type`` came
      back together with the members of any type spelled ``myXtype``.
    * LIKE is case-insensitive for ASCII while a USR is case-sensitive, thus
      ``MY_TYPE`` and ``my_type`` reached each other.
    * Case-insensitive LIKE cannot use ``idx_refs_to_usr``, which is BINARY.
      The query plan fell back to a scan of every reference of the build.

    A range comparison has none of those: no metacharacters, BINARY order,
    and an index seek.
    """
    return usr + "@", usr + chr(ord("@") + 1)


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
    if kind in _MEMBER_GRANULAR_KINDS:
        # The page groups its rows, thus the count must group them too.
        lo, hi = _member_usr_range(usr)
        row = conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT 1 FROM refs r
                    LEFT JOIN symbols caller
                      ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                    WHERE r.config_hash = ?
                      AND r.to_usr >= ? AND r.to_usr < ? {kind_filter}
                    GROUP BY caller.qualified_name, r.from_file, r.from_line)""",
            [config_hash, lo, hi, *kind_params],
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

    This half of ``find_refs_with_candidates`` does no name resolution.  A
    caller that has already chosen a symbol must use it, so that the choice
    is made once.  ``get_symbol_context`` once passed the NAME to the
    resolving half after ``_lookup_definition`` picked a symbol from that
    same name, and the two resolvers ranked differently: measured on one
    firmware index, one answer held the body of one symbol and the call
    sites of another.

    **Aggregate type handling**: for a class, struct or enum the references
    sit at member granularity (``MyClass::method``), thus the query takes
    the USR range of the members — see :func:`_member_usr_range` for why a
    range and not a LIKE.  GROUP BY deduplicates when several members are
    referenced on one line, as in ``obj.a = obj.b``.

    The order ends where the GROUP BY does, and both ends are TOTAL —
    ``offset`` can then neither repeat a row nor step over one.

    An aggregate groups by the CALLER as well, and one line can carry
    references from several callers — measured on one firmware index, 88
    lines did, one of them from 8 callers.  Those groups tie on the file
    and the line alone, thus the caller ends that order.

    A plain symbol orders by the column after the line, thus two calls on
    one line come in the order of the source, and ends at ``r.rowid``.  The
    file, the line and the column alone do NOT
    separate its references: ``idx_refs_site`` covers the column, the slot,
    ``from_usr`` and ``ref_kind`` as well, thus one line may hold a ``call``
    row and a ``ref`` row for one symbol.  Measured over six firmware indexes, 4792
    to 80635 lines carry such a pair — ``ARM_MPU_ClrRegion`` at
    ``cortex_m/scb.c:64`` is one.  The rowid is unique whatever the columns
    hold, which the unique index is not: SQLite reads two NULL
    ``from_usr`` values as distinct, so even that index leaves ties.
    """
    from ..models import origins_or_sql

    kind_filter, kind_params = _build_kind_filter(ref_kind)

    _SELECT = """SELECT r.from_file, r.from_line, r.ref_kind,
                        r.from_usr, {col} AS from_col, {origins} AS origins,
                        caller.name           AS caller_name,
                        caller.qualified_name AS caller_qname,
                        caller.kind           AS caller_kind,
                        caller.file_path      AS caller_file,
                        caller.is_project     AS caller_is_project
                 FROM refs r
                 LEFT JOIN symbols caller
                   ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                 WHERE r.config_hash = ?"""

    if kind in _MEMBER_GRANULAR_KINDS:
        lo, hi = _member_usr_range(usr)
        params: list = [config_hash, lo, hi] + kind_params + [limit, offset]
        # One row for each caller and line: the first column of the group,
        # and the origins of each row of it.
        select = _SELECT.format(col="MIN(r.from_col)", origins=origins_or_sql("r.origins"))
        return conn.execute(
            f"""{select}
                  AND r.to_usr >= ? AND r.to_usr < ? {kind_filter}
                GROUP BY caller.qualified_name, r.from_file, r.from_line
                ORDER BY r.from_file, r.from_line, caller.qualified_name
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()

    params = [config_hash, usr] + kind_params + [limit, offset]
    select = _SELECT.format(col="r.from_col", origins="r.origins")
    return conn.execute(
        f"""{select}
              AND r.to_usr = ? {kind_filter}
            ORDER BY r.from_file, r.from_line, r.from_col, r.rowid
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
    # ``reach`` is an upper bound per symbol and not a cost: SQLite stops
    # at the end of the rows a symbol has, and measured on one firmware
    # index the widest same-name answer held 159 rows over 50 symbols.
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


class _TaggedRef:
    """A reference row that also knows which same-name symbol it belongs to.

    ``sqlite3.Row`` cannot take a new key, and turning every row into a
    dict here would change what each caller of
    ``find_refs_with_candidates`` receives.  This wrapper forwards the
    mapping access and adds the two target keys.
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
    ``get_build_info()`` to display index statistics and by the
    indexer to decide whether reference-based tools (find_callers,
    find_hotspots) are available.
    """
    return conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash=?", (config_hash,)).fetchone()[0]
