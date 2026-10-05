"""The row that tells a reader where a page sits in the whole answer.

A tool that cuts its answer at ``limit`` leaves the reader guessing.  A
result of exactly ``limit`` rows can be the whole truth or the first slice
of hundreds, and nothing in the rows says which.  The reader then either
stops too early or asks again for no reason.

Every tool that pages therefore leads with one row:

    {"total": 137, "offset": 0, "shown": 20, "more": true,
     "hint": "…pass offset=20…"}

It is always there, even when one page holds everything.  A row that
appears only sometimes teaches the reader nothing, because its absence
would then have to mean "no more" — and that is exactly the guess this row
exists to remove.

── Why an OFFSET needs a stable order ──

Two pages of one walk must not overlap or skip.  That holds only when the
query orders by something deterministic, thus every paged query ends its
ORDER BY with a column that breaks every tie: a rowid, a USR, or a file
and line.  An ORDER BY that stops at a score lets SQLite return tied rows
in any order, and the reader would then see one row twice and never see
another.
"""

from __future__ import annotations

import re


def page_notice(
    total: int,
    offset: int,
    shown: int,
    *,
    hint: str = "",
) -> dict:
    """Build the leading row of a paged answer.

    *total* counts every row that the query matches, not the page.
    *offset* is where this page starts, *shown* how many rows follow.

    *hint* names the call that reads the next page.  It is left out when
    the page is the last one, because an instruction that leads nowhere
    costs the reader a call to find that out.
    """
    more = offset + shown < total
    notice: dict[str, object] = {
        "total": total,
        "offset": offset,
        "shown": shown,
        "more": more,
    }
    if more and hint:
        notice["hint"] = hint
    return notice


def page_hint(tool: str, *args: object, next_offset: int, **kwargs: object) -> str:
    """Spell out the call that reads the next page.

    The text is the call itself, for example
    ``find_variables('g_', kind='varglobal', offset=20) reads the next page.``
    A reader that copies it gets the same answer one page further on, thus
    the call must repeat every argument that changes the answer, and not
    only the offset.

    A string argument is in single quotes, as in the hints of the older
    paged tools, and any other value is written as it is.
    """
    parts = [_hint_value(a) for a in args]
    parts += [f"{key}={_hint_value(value)}" for key, value in kwargs.items()]
    parts.append(f"offset={next_offset}")
    return f"{tool}({', '.join(parts)}) reads the next page."


def _hint_value(value: object) -> str:
    return f"'{value}'" if isinstance(value, str) else str(value)


def past_end_info(thing: str, offset: int, total: int) -> dict:
    """The answer to a page that starts after the last row.

    An empty list would read as "no such code".  The row says instead that
    the answer exists and is shorter than the offset, thus the reader goes
    back and does not conclude that nothing matched.
    """
    return {"info": f"No {thing} at offset {offset}; the answer holds {total}."}


# The text of every past-end row: the one of ``past_end_info``, and the
# older ones that the first paged tools write inline in the same words.
_PAST_END = re.compile(r"^No .+ at offset \d+; the answer holds \d+\.$")


def holds_past_end_info(result: object) -> bool:
    """Tell if *result* holds the row of a page after the last row.

    WHY: an answer that names no file reads as an empty answer to the
    staleness check, which then scans the whole index and adds "an empty
    result is not proof of absence".  A page after the end is not empty:
    the answer exists, and the row gives its size.  The check must leave
    such an answer alone.
    """
    records = result if isinstance(result, list) else [result]
    return any(
        isinstance(r, dict) and isinstance(r.get("info"), str) and _PAST_END.match(r["info"])
        for r in records
    )


def clamp_offset(offset: int | None) -> int:
    """Read the offset a caller gave.  A missing or negative one is zero."""
    if not offset or offset < 0:
        return 0
    return int(offset)
