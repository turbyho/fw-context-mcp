"""Name → symbol resolution, shared by the call graph and the reference queries.

A caller gives a NAME and the database holds USRs.  Between the two sits a
choice, because one name can mean several symbols: two classes can each
hold a method called ``probe``, and a C++ inline function can carry one USR
per translation unit.

This module holds that choice in ONE place.  It lived in three:
``_resolve_target_usr`` in ``_callgraph.py``, a second copy inside
``find_call_path``, and a third inside the reference lookup of
``_refs.py``.  All three wrote the same four conditions as one flat
``OR`` with ``LIMIT 1``,
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
    "CANDIDATES_SHOWN",
    "Candidate",
    "ambiguity_notice",
    "candidate_labels",
    "candidate_rows",
    "count_candidates",
    "resolve_candidates",
    "resolve_usr",
    "resolve_usrs",
]

#: How many same-name symbols one graph query WALKS.
#:
#: This is a cost bound, not a display bound.  Each target costs a
#: recursive CTE or a reference query, thus the two must not share one
#: number: measured on one firmware index, ``size`` matches 132 symbols and
#: ``operator=`` matches 204, and walking all of them answers a question
#: that nobody asked.
MAX_TRAVERSED_TARGETS = 50

#: Kept for the readers that still spell it the older way.
MAX_AMBIGUOUS_TARGETS = MAX_TRAVERSED_TARGETS

#: How many candidates a tool OFFERS the caller to choose from.
#:
#: A candidate row costs about 250 bytes, thus this is a display bound and
#: it is far cheaper than a walk.  It stays small because the list sits
#: beside the body that the caller asked for, and the caller reads the
#: whole answer.  The count of ALL matches travels with the list, and
#: ``lookup_symbol`` pages through the rest, so a small list hides nothing:
#: it only spares the common case.
CANDIDATES_SHOWN = 20


#: The fields of a Candidate, in order.
_CANDIDATE_FIELDS = (
    "usr", "qualified_name", "kind", "file_path", "owner", "line", "signature",
)


class Candidate(tuple):
    """One symbol that a name can mean.

    Fields: ``usr``, ``qualified_name``, ``kind``, ``file_path``, ``owner``,
    ``line``, ``signature``.

    A named tuple subclass rather than a dataclass, because the callers
    unpack it and pass the parts to SQL.

    ``kind`` is here for ``refs_for_symbol``: a class, struct or enum keeps
    its references at member granularity, and that query needs the kind to
    know it must match a USR prefix.

    ``owner`` is the class, struct or union that declares the symbol, and
    it is empty for a free function.  It is the field that tells two
    same-name methods apart at a glance, without a reader having to cut a
    qualified name at the last ``::``.

    ``file_path``, ``line`` and ``signature`` complete the description, so
    that a caller can choose between the candidates without asking again.
    """

    __slots__ = ()

    def __new__(
        cls,
        usr: str,
        qualified_name: str,
        kind: str,
        file_path: str = "",
        owner: str = "",
        line: int = 0,
        signature: str = "",
    ):
        return super().__new__(
            cls, (usr, qualified_name, kind, file_path, owner, line, signature)
        )

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

    @property
    def owner(self) -> str:
        return self[4]

    @property
    def line(self) -> int:
        return self[5]

    @property
    def signature(self) -> str:
        return self[6]


def _parameters(signature: str) -> str:
    """Cut the parameter list out of a signature, brackets included.

    ``bool set(uint8_t, const char *)`` gives ``(uint8_t, const char *)``.
    A signature with no parentheses gives ``""``.
    """
    start = signature.find("(")
    end = signature.rfind(")")
    if start == -1 or end <= start:
        return ""
    return signature[start: end + 1]


def candidate_labels(candidates: list[Candidate]) -> list[str]:
    """Name each candidate so that a reader can tell them apart.

    A qualified name alone is enough almost always, and it is then what the
    caller can type back.  Two measured cases where it is not:

    * Overloads.  ``CoilData::set`` has five, thus one project answered
      with two rows that read ``CoilData.cpp:183 → CoilData::set`` and
      differed only in a USR the reader never sees.  The parameter list is
      what separates overloads, and it is what a reader recognises.
    * A C function at file scope, whose qualified name IS its bare name.
      One Zephyr project gave a notice reading ``clock_stop, clock_stop,
      clock_stop``.  Those live in different files, thus the file
      separates them.

    The parameter list comes first because two overloads usually share a
    file, which would leave the file label as ambiguous as the bare name.
    A candidate that neither tells apart keeps the plain name: a label that
    adds nothing is worse than a short one.
    """
    seen: dict[str, int] = {}
    for candidate in candidates:
        seen[candidate.qualified_name] = seen.get(candidate.qualified_name, 0) + 1

    labels: list[str] = []
    for c in candidates:
        if seen[c.qualified_name] <= 1:
            labels.append(c.qualified_name)
            continue
        params = _parameters(c.signature)
        if params:
            labels.append(f"{c.qualified_name}{params}")
        elif c.file_path:
            labels.append(f"{c.qualified_name} ({c.file_path})")
        else:
            labels.append(c.qualified_name)
    return labels


def candidate_rows(candidates: list[Candidate], root=None) -> list[dict]:
    """Describe each candidate as a row that a caller can choose from.

    WHY a list of rows and not a sentence: a tool that answers with ONE body
    used to name the alternatives in prose, and a reader then had to parse
    that text and ask again.  These rows carry what the choice needs —
    ``class``, ``file``, ``line``, ``signature`` — thus the reader can pick
    the right symbol, or read all of them, without another round trip.

    ``class`` is empty for a free function.  ``qualified_name`` is the
    string to give back to the tool to get that one symbol.
    """
    rows: list[dict] = []
    for c in candidates:
        file_path = c.file_path
        if root is not None and file_path:
            from fw_context_mcp.utils import abs_path

            file_path = abs_path(root, file_path)
        rows.append({
            "qualified_name": c.qualified_name,
            "class": c.owner,
            "kind": c.kind,
            "file": file_path,
            "line": c.line,
            "signature": c.signature,
        })
    return rows


#: The rank expression and the WHERE clause.  ``count_candidates`` and
#: ``resolve_candidates`` both read them, thus the count can never describe
#: another set than the list does.  They were one text and two copies of
#: it until this was written down, which is the drift these constants
#: exist to stop.
#:
#: The parameter order is part of the contract: three for the rank
#: (qualified name, bare name, suffix pattern), then five for the WHERE
#: (config hash, bare name, bare name, suffix pattern, plain tail).
#: :func:`_match_params` builds them.
_RANK_SQL = """CASE WHEN s.qualified_name = ? THEN 0
                    WHEN s.name = ? THEN 1
                    WHEN s.qualified_name LIKE ? ESCAPE '\\' THEN 2
                    ELSE 3 END"""
_WHERE_SQL = """s.config_hash = ?
                AND (s.name = ? OR s.qualified_name = ?
                     OR s.qualified_name LIKE ? ESCAPE '\\' OR s.name = ?)"""

#: The order of the candidates, which ranks more than a list.
#:
#: It decides which symbols a graph query WALKS when a name matches more
#: than ``MAX_TRAVERSED_TARGETS``, it decides the interleave that
#: ``find_refs_with_candidates`` cuts with an ``offset``, and it decides
#: which candidates a tool that returns one body OFFERS.  Two pages of one
#: walk read this order twice, thus it must give the same answer twice.
#:
#: It therefore ends at ``s.usr``, which is unique within one build.  Every
#: column before it ties over exactly the population that this module
#: exists for: two same-name methods of two classes share the definition
#: flag and the project flag, and a pair that nothing references shares
#: both counts as well.  SQLite may return tied rows in any order, thus a
#: walk over such an order shows one symbol twice and hides another.
_CANDIDATE_ORDER = """ORDER BY match_rank, s.is_definition DESC, s.is_project DESC,
                               ref_count DESC, out_count DESC, s.usr"""


def _match_params(name: str) -> tuple:
    """Build the parameters that the rank and the WHERE clause both need."""
    esc_name = _escape_like(name)
    suffix_pattern = f"%::{esc_name}"
    plain_name = name.rsplit("::", 1)[-1] if "::" in name else name
    # NULL never equals a column value, thus a bare name reaches no rank 0.
    qualified_probe = name if "::" in name else None
    return qualified_probe, name, suffix_pattern, plain_name


def count_candidates(conn: sqlite3.Connection, config_hash: str, name: str) -> int:
    """Count EVERY symbol that *name* matches at its best rank.

    The list that a tool shows is cut to keep an answer readable, thus the
    caller must learn how much it is not seeing.  Without the true count a
    short list reads as the whole truth: measured on one firmware index,
    ``size`` matches 132 symbols and a list of 20 says nothing about the
    other 112.
    """
    qualified_probe, plain, suffix_pattern, plain_name = _match_params(name)
    try:
        row = conn.execute(
            f"""WITH ranked AS (
                    SELECT s.usr AS usr, {_RANK_SQL} AS match_rank
                    FROM symbols s
                    WHERE {_WHERE_SQL}
                )
                SELECT COUNT(DISTINCT usr) FROM ranked
                WHERE match_rank = (SELECT MIN(match_rank) FROM ranked)""",
            (
                qualified_probe, plain, suffix_pattern,
                config_hash, plain, plain, suffix_pattern, plain_name,
            ),
        ).fetchone()
    except sqlite3.Error:
        log.exception("count_candidates failed for '%s'", name)
        return 0
    return row[0] if row else 0


def resolve_candidates(
    conn: sqlite3.Connection, config_hash: str, name: str, limit: int | None = None
) -> list[Candidate]:
    """Find each symbol that *name* can mean.  The best match comes first.

    Returns the candidates that share the BEST match rank, at most *limit*
    of them (``MAX_TRAVERSED_TARGETS`` by default).  One element means the
    name was not ambiguous.  More than one means the caller must either
    walk all of them or say which one it took — it must not pick one in
    silence.

    *limit* is a parameter because two callers need different numbers: a
    graph query pays a recursive CTE per target and wants few, while a
    tool that only LISTS the choice pays about 250 bytes per row and can
    afford more.  :func:`count_candidates` gives the true total either way.

    Inside one rank the order is: a definition before a declaration, then
    the variant with the most references, and the USR last.  Candidates of
    equal specificity carry the same answer for the caller, but they must
    still come back in the SAME order every time — see
    ``_CANDIDATE_ORDER`` for what reads this order.

    Rank 0 needs a name that HOLDS ``::``.  A bare name carries no
    disambiguator, thus it means every symbol that bears it.  Without this
    guard a bare name that is also the whole qualified name of a free
    function sat alone at rank 0 and hid every method of that name:
    measured on one firmware index, ``read`` gave one free function while
    the body tools answered with ``IteratorReader::read``, and the two
    tools then described different symbols.
    """
    cap = MAX_TRAVERSED_TARGETS if limit is None else limit
    qualified_probe, plain, suffix_pattern, plain_name = _match_params(name)
    try:
        rows = conn.execute(
            f"""SELECT s.usr,
                      COALESCE(s.qualified_name, s.name) AS qualified_name,
                      s.kind AS kind,
                      s.file_path AS file_path,
                      s.line AS line,
                      s.signature AS signature,
                      CASE WHEN p.kind IN ('class', 'struct', 'union')
                           THEN p.name ELSE '' END AS owner,
                      {_RANK_SQL} AS match_rank,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.to_usr = s.usr AND r.config_hash = s.config_hash) AS ref_count,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.from_usr = s.usr AND r.config_hash = s.config_hash) AS out_count
               FROM symbols s
               LEFT JOIN symbols p
                 ON p.usr = s.parent_usr AND p.config_hash = s.config_hash
               WHERE {_WHERE_SQL}
               {_CANDIDATE_ORDER}""",
            (
                qualified_probe, plain, suffix_pattern,
                config_hash, plain, plain, suffix_pattern, plain_name,
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
            row["file_path"] or "", row["owner"] or "",
            row["line"] or 0, row["signature"] or "",
        ))
        if len(out) >= cap:
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


def ambiguity_notice(
    name: str,
    qualified_names: list[str],
    relation: str,
    total: int | None = None,
) -> dict[str, str]:
    """Build the row that reports a name with more than one symbol.

    *relation* names the rows that come after it, for example ``callers``.

    *total* is how many symbols the name matches ALTOGETHER, from
    :func:`count_candidates`.  It matters because the list of names that
    reaches this function is cut at ``MAX_TRAVERSED_TARGETS``, and the
    exact number is known: without it a full list could only be reported
    as "more than 50", while ``get_source`` answered for the same name
    with an exact ``candidates_total``.  A reader who saw both had no way
    to tell that the two described one ambiguity.  Measured on one
    firmware index, ``size`` matches 132 symbols.

    ``None`` falls back to the length of the list, for a caller that
    cannot count — the notice is then honest about being a lower bound.

    WHY the names go in the message text, and not in a second key: these
    tools answer with a list of rows and have nowhere to put a key that
    belongs to the whole answer.  A leading row that holds ``warning``
    alone is the shape the project already uses for such a fact —
    ``with_stale_annotation`` in ``mcp/shared/stale.py`` prepends the same
    one — thus the count and the names travel inside that text.
    """
    walked = len(qualified_names)
    matched = walked if total is None else total
    # The walk is what a row can come from, thus the cap is about the walk
    # and not about the count.
    capped = walked >= MAX_AMBIGUOUS_TARGETS or matched > walked
    count = str(matched) if total is not None else (
        f"more than {MAX_AMBIGUOUS_TARGETS}" if capped else str(walked)
    )
    message = (
        f"The name '{name}' matches {count} symbols. The {relation} of them "
        f"are in the rows below: {name_list(qualified_names)}. Each row has "
        f"'target_qualified_name', which tells the symbol that the row "
        f"belongs to. To get one symbol only, give its full qualified name."
    )
    if capped:
        message += (
            f" The query used the first {min(walked, MAX_AMBIGUOUS_TARGETS)} "
            f"symbols only, thus a symbol of this name can be missing from "
            f"the rows."
        )
    return {"warning": message}
