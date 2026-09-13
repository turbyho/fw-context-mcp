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
    "candidate_labels",
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
    """One symbol that a name can mean: ``(usr, qualified_name, kind, file_path)``.

    A named tuple subclass rather than a dataclass, because the callers
    unpack it and pass the parts to SQL.  ``kind`` is here for ``find_refs``:
    a class, struct or enum keeps its references at member granularity, and
    that query needs the kind to know it must match a USR prefix.

    ``file_path`` is here because a qualified name does not always tell two
    candidates apart.  A C function at file scope carries a qualified name
    equal to its bare name, thus three static functions of one name in
    three files all read as that one name.
    """

    __slots__ = ()

    def __new__(cls, usr: str, qualified_name: str, kind: str, file_path: str = ""):
        return super().__new__(cls, (usr, qualified_name, kind, file_path))

    @property
    def usr(self) -> str:
        return self[0]

    @property
    def qualified_name(self) -> str:
        return self[1]

    @property
    def kind(self) -> str:
        return self[2]

    @property
    def file_path(self) -> str:
        return self[3]


def candidate_labels(candidates: list[Candidate]) -> list[str]:
    """Name each candidate so that a reader can tell them apart.

    A qualified name alone is enough almost always, and it is then what the
    caller can type back.  It is NOT enough for a C function at file scope:
    measured on one Zephyr project, a name matched three symbols and the
    notice read ``clock_stop, clock_stop, clock_stop``.  A repeated name
    therefore takes its file.
    """
    seen: dict[str, int] = {}
    for candidate in candidates:
        seen[candidate.qualified_name] = seen.get(candidate.qualified_name, 0) + 1
    return [
        f"{c.qualified_name} ({c.file_path})"
        if seen[c.qualified_name] > 1 and c.file_path
        else c.qualified_name
        for c in candidates
    ]


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

    Rank 0 needs a name that HOLDS ``::``.  A bare name carries no
    disambiguator, thus it means every symbol that bears it.  Without this
    guard a bare name that is also the whole qualified name of a free
    function sat alone at rank 0 and hid every method of that name:
    measured on one firmware index, ``read`` gave one free function while
    the body tools answered with ``IteratorReader::read``, and the two
    tools then described different symbols.
    """
    esc_name = _escape_like(name)
    suffix_pattern = f"%::{esc_name}"
    plain_name = name.rsplit("::", 1)[-1] if "::" in name else name
    # NULL never equals a column value, thus a bare name reaches no rank 0.
    qualified_probe = name if "::" in name else None
    try:
        rows = conn.execute(
            """SELECT s.usr,
                      COALESCE(s.qualified_name, s.name) AS qualified_name,
                      s.kind AS kind,
                      s.file_path AS file_path,
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
                qualified_probe, name, suffix_pattern,
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
        out.append(Candidate(
            usr, row["qualified_name"] or usr, row["kind"] or "",
            row["file_path"] or "",
        ))
        if len(out) >= MAX_AMBIGUOUS_TARGETS:
            break
    return out


def resolve_usrs(
    conn: sqlite3.Connection, config_hash: str, name: str
) -> tuple[list[str], list[str]]:
    """Give ``(usrs, labels)`` for *name* as two parallel lists.

    A view on :func:`resolve_candidates` for the call-graph queries, which
    build SQL parameter lists and never need the kind.

    The label is the qualified name, and it is the qualified name alone
    almost always.  It takes the file when two candidates share a qualified
    name, because a reader cannot otherwise tell them apart — see
    :func:`candidate_labels`.
    """
    candidates = resolve_candidates(conn, config_hash, name)
    return [c.usr for c in candidates], candidate_labels(candidates)


def resolve_usr(conn: sqlite3.Connection, config_hash: str, name: str) -> str | None:
    """Give the single best USR for *name*, or ``None`` when there is none.

    It reports the best match and tells nothing about the other candidates.
    Thus a caller that must not guess, because it WRITES an edge and does
    not read one, must use :func:`resolve_candidates` instead and must have
    its own policy for an ambiguous answer.
    """
    candidates = resolve_candidates(conn, config_hash, name)
    return candidates[0].usr if candidates else None


#: How many names a notice writes out before it counts the rest.
#: Measured on one firmware index, a bare ``size`` matched 50 symbols, and
#: a notice that spelled all of them out cost more to read than the rows it
#: described.  A reader needs enough names to recognise the ambiguity, not
#: the whole list.
_NAMES_SHOWN_IN_NOTICE = 8


def name_list(qualified_names: list[str]) -> str:
    """Write out the first few names, then count the rest."""
    shown = qualified_names[:_NAMES_SHOWN_IN_NOTICE]
    rest = len(qualified_names) - len(shown)
    text = ", ".join(shown)
    if rest > 0:
        text += f", and {rest} more"
    return text


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
    capped = len(qualified_names) >= MAX_AMBIGUOUS_TARGETS
    count = f"more than {MAX_AMBIGUOUS_TARGETS}" if capped else str(len(qualified_names))
    message = (
        f"The name '{name}' matches {count} symbols. The {relation} of them "
        f"are in the rows below: {name_list(qualified_names)}. Each row has "
        f"'target_qualified_name', which tells the symbol that the row "
        f"belongs to. To get one symbol only, give its full qualified name."
    )
    if capped:
        message += (
            f" The query used the first {MAX_AMBIGUOUS_TARGETS} symbols only,"
            f" thus a symbol of this name can be missing from the rows."
        )
    return {"warning": message}
