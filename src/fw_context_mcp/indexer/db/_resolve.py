"""Name → symbol resolution, shared by the call graph and the reference queries.

A caller gives a NAME and the database holds USRs.  Between the two sits a
choice, because one name can mean several symbols: two classes can each
hold a method called ``probe``, and a C++ inline function can carry one USR
per translation unit.

This module holds that choice in ONE place.  It lived in three:
``_resolve_target_usr`` in ``_callgraph.py``, a second copy inside
``find_call_path``, and a third inside ``find_refs`` in ``_refs.py``.  All
three wrote the same four conditions as one flat ``OR`` with ``LIMIT 1``,
and each then disagreed with the others about what one name meant.
Measured on one firmware index, a single tool reported the body of one
symbol together with the call sites of another, because it asked two of
those copies.

── The rank ──

The four conditions do not carry equal weight:

0. Exact ``qualified_name`` — ``ClassB::probe`` names ONE symbol.
1. Exact ``name`` — the bare name as the caller wrote it.
2. Suffix of ``qualified_name`` — ``ClassB::probe`` inside a namespace.
3. Plain name — the tail after the last ``::``.

Rank 3 is the widest and must never dilute rank 0.  As peers of one ``OR``
they did exactly that: the tail of a qualified name matched every class
with such a method, and an unrelated reference count then picked the
winner.  Ordering by rank first makes the exact answer unbeatable, because
a lower rank is never reached once a higher one matched.
"""

from __future__ import annotations

import logging
import sqlite3

from fw_context_mcp.utils import escape_like as _escape_like

log = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "ambiguity_notice",
    "resolve_candidates",
    "resolve_usr",
    "resolve_usrs",
]

#: The maximum number of same-name targets that one query walks.
#: Measured on one firmware index: 90% of the names give a single USR and
#: never touch this limit, but ``operator=`` gives 204.  To walk all of
#: those gives an answer to a question that nobody asked.  Thus the query
#: cuts the set, and it tells the caller that it did.
MAX_AMBIGUOUS_TARGETS = 50


class Candidate(tuple):
    """One symbol that a name can mean: ``(usr, qualified_name, kind)``.

    A named tuple subclass rather than a dataclass, because the callers
    unpack it and pass the parts to SQL.  ``kind`` is here for ``find_refs``:
    a class, struct or enum keeps its references at member granularity, and
    that query needs the kind to know it must match a USR prefix.
    """

    __slots__ = ()

    def __new__(cls, usr: str, qualified_name: str, kind: str):
        return super().__new__(cls, (usr, qualified_name, kind))

    @property
    def usr(self) -> str:
        return self[0]

    @property
    def qualified_name(self) -> str:
        return self[1]

    @property
    def kind(self) -> str:
        return self[2]


def resolve_candidates(
    conn: sqlite3.Connection, config_hash: str, name: str
) -> list[Candidate]:
    """Find each symbol that *name* can mean.  The best match comes first.

    Returns the candidates that share the BEST match rank.  One element
    means the name was not ambiguous.  More than one means the caller must
    either walk all of them or say which one it took — it must not pick one
    in silence.

    Inside one rank the order is: a definition before a declaration, then
    the variant with the most references.  Candidates of equal specificity
    are interchangeable for the purpose of the caller.
    """
    esc_name = _escape_like(name)
    suffix_pattern = f"%::{esc_name}"
    plain_name = name.rsplit("::", 1)[-1] if "::" in name else name
    try:
        rows = conn.execute(
            """SELECT s.usr,
                      COALESCE(s.qualified_name, s.name) AS qualified_name,
                      s.kind AS kind,
                      CASE WHEN s.qualified_name = ? THEN 0
                           WHEN s.name = ? THEN 1
                           WHEN s.qualified_name LIKE ? ESCAPE '\\' THEN 2
                           ELSE 3 END AS match_rank,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.to_usr = s.usr AND r.config_hash = s.config_hash) AS ref_count,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.from_usr = s.usr AND r.config_hash = s.config_hash) AS out_count
               FROM symbols s
               WHERE s.config_hash = ?
                 AND (s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ? ESCAPE '\\'
                      OR s.name = ?)
               ORDER BY match_rank, s.is_definition DESC, ref_count DESC, out_count DESC""",
            (
                name, name, suffix_pattern,
                config_hash, name, name, suffix_pattern, plain_name,
            ),
        ).fetchall()
    except sqlite3.Error:
        log.exception("resolve_candidates failed for '%s'", name)
        return []
    if not rows:
        return []

    # Keep only the best rank.  The rows already arrive ordered by it, thus
    # the first row names the rank and the walk stops at the first row past
    # it.
    best_rank = rows[0]["match_rank"]
    out: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        if row["match_rank"] != best_rank:
            break
        usr = row["usr"]
        # A declaration and its definition share one USR, thus the same
        # symbol arrives twice.  That is one candidate, not two.
        if usr in seen:
            continue
        seen.add(usr)
        out.append(Candidate(usr, row["qualified_name"] or usr, row["kind"] or ""))
        if len(out) >= MAX_AMBIGUOUS_TARGETS:
            break
    return out


def resolve_usrs(
    conn: sqlite3.Connection, config_hash: str, name: str
) -> tuple[list[str], list[str]]:
    """Give ``(usrs, qualified_names)`` for *name* as two parallel lists.

    A view on :func:`resolve_candidates` for the call-graph queries, which
    build SQL parameter lists and never need the kind.
    """
    candidates = resolve_candidates(conn, config_hash, name)
    return [c.usr for c in candidates], [c.qualified_name for c in candidates]


def resolve_usr(conn: sqlite3.Connection, config_hash: str, name: str) -> str | None:
    """Give the single best USR for *name*, or ``None`` when there is none.

    It reports the best match and tells nothing about the other candidates.
    Thus a caller that must not guess, because it WRITES an edge and does
    not read one, must use :func:`resolve_candidates` instead and must have
    its own policy for an ambiguous answer.
    """
    candidates = resolve_candidates(conn, config_hash, name)
    return candidates[0].usr if candidates else None


def ambiguity_notice(name: str, qualified_names: list[str], relation: str) -> dict[str, str]:
    """Build the row that reports a name with more than one symbol.

    *relation* names the rows that come after it, for example ``callers``.

    WHY the names go in the message text, and not in a second key:
    ``handlers/_base.py`` lifts a row whose key set is exactly
    ``{"warning"}`` out of the results of each build variant, removes the
    duplicates, and puts one copy in front.  A row with a second key does
    not take that path.  It stays in the middle of the rows, where a
    reader that looks at the first row does not see it.
    """
    targets = ", ".join(qualified_names)
    message = (
        f"The name '{name}' matches {len(qualified_names)} symbols. The "
        f"{relation} of all of them are in the rows below: {targets}. Each "
        f"row has 'target_qualified_name', which tells the symbol that the "
        f"row belongs to. To get one symbol only, give its full qualified "
        f"name."
    )
    if len(qualified_names) >= MAX_AMBIGUOUS_TARGETS:
        message += (
            f" More than {MAX_AMBIGUOUS_TARGETS} symbols have this name. "
            f"The query used the first {MAX_AMBIGUOUS_TARGETS} only."
        )
    return {"warning": message}
