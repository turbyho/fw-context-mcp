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

    A string argument is written by :func:`hint_arg`, and any other value
    is written as it is.  Give the arguments of :func:`selector_args` too:
    without them the next call answers for another project or build.
    """
    return f"{call_text(tool, *args, **kwargs, offset=next_offset)} reads the next page."


def call_text(tool: str, *args: object, **kwargs: object) -> str:
    """Write a tool call as text: ``tool('a', key=value)``.

    For a hint that names another tool, such as ``find_callers('x')
    pages the callers``.  :func:`page_hint` writes its call with it.
    """
    parts = [_hint_value(a) for a in args]
    parts += [f"{key}={_hint_value(value)}" for key, value in kwargs.items()]
    return f"{tool}({', '.join(parts)})"


def selector_args(
    project_root: str | None = None,
    variant: str | None = None,
    image: str | None = None,
) -> dict[str, str]:
    """The arguments that selected the project and the build, for a hint.

    WHY: a hint without them sent the next call to the project of the
    current directory and to the default build.  After a question about
    another project, that next page came from other source code, and
    nothing in it said so.

    *project_root* is the value that the caller gave: a path, a project
    name or a project_id.  The ``project`` parameter puts its value there
    too (``_merge_project_selector`` in ``mcp/server.py``).  The hint
    writes it as ``project``, because that parameter takes each of the
    three, and the instructions for the reader name it.  A value that the
    caller did not give is left out, thus a plain call keeps a short hint.
    """
    args: dict[str, str] = {}
    if project_root:
        args["project"] = str(project_root)
    if variant:
        args["variant"] = variant
    if image:
        args["image"] = image
    return args


def hint_arg(value: str) -> str:
    """Write a string argument of a hint as a Python string literal.

    WHY not ``f"'{value}'"``: a query or a path can hold an apostrophe,
    and ``search_code('it's', offset=20)`` is not a call that a reader can
    copy — the string stops after ``it``.  ``repr`` gives single quotes
    for a plain value, thus the hint of a C name or a plain query does not
    change, and it gives double quotes or escapes for the other values.

    A backslash is doubled, as in a Windows path ``'src\\\\net.c'``.  The
    old form ``'src\\net.c'`` read as a newline in a Python literal.  A
    JSON string escapes a backslash the same way, thus a reader that
    copies the doubled text into a JSON argument sends the right path.
    """
    return repr(value)


def _hint_value(value: object) -> str:
    return hint_arg(value) if isinstance(value, str) else str(value)


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
