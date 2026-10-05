"""Shared checks for a tool answer that pages.

The older paging tests each keep their own copy of these helpers.  The
tests of the tools that page later use this module, thus one walk holds
every tool to the same contract.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable

# The keys of the page notice; ``hint`` is there only when ``more`` is true.
NOTICE_KEYS = {"total", "offset", "shown", "more"}
# Rows that say where the answer came from, and are not part of it.
SIDE_KEYS = {"warning", "info", "error", "_did_you_mean"}


def notice(rows: list[dict]) -> dict | None:
    """The page notice of *rows*, found by its keys and not by its position."""
    return next((r for r in rows if NOTICE_KEYS <= set(r)), None)


def answers(rows: list[dict]) -> list[dict]:
    """The rows of *rows* that are part of the answer."""
    return [r for r in rows if not NOTICE_KEYS <= set(r) and not SIDE_KEYS & set(r)]


def walk_pages(
    call: Callable[[int], list[dict]],
    key: Callable[[dict], Hashable],
    *,
    cap: int = 1000,
) -> tuple[list[Hashable], int]:
    """Read every page of one answer and hold each notice to its word.

    *call* gets the offset and returns one page.  *key* names a row.  The
    walk asserts that each page has a notice, that the notice gives the
    offset of the page and the number of rows on it, and that ``total``
    does not change between pages.  It returns the keys in walk order and
    the total.  *cap* stops a walk that would never end.
    """
    seen: list[Hashable] = []
    total: int | None = None
    offset = 0
    while offset < cap:
        rows = call(offset)
        body = answers(rows)
        page = notice(rows)
        if not body:
            assert page is None or page["shown"] == 0
            break
        assert page is not None, f"page at offset {offset} has no notice"
        assert page["offset"] == offset
        assert page["shown"] == len(body)
        if total is None:
            total = page["total"]
        assert page["total"] == total, "total changed between pages"
        assert ("hint" in page) == page["more"]
        seen += [key(r) for r in body]
        offset += len(body)
        if not page["more"]:
            break
    return seen, total or 0


def assert_whole_and_once(seen: list[Hashable], total: int) -> None:
    """A finished walk reached every row of the answer, and each one once."""
    assert len(seen) == total, f"walked {len(seen)} of {total}"
    assert len(set(seen)) == len(seen), "a row came back twice"
