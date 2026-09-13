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


def clamp_offset(offset: int | None) -> int:
    """Read the offset a caller gave.  A missing or negative one is zero."""
    if not offset or offset < 0:
        return 0
    return int(offset)
