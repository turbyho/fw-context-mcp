"""find_wrapper_callers pages the wrapper classes, and never splits one.

The tool read at most 50 call sites into the driver and grouped them.  A
driver with more call sites gave a partial grouping with no notice: a
class could lose methods, and ``method_count`` counted only the part that
was read.  The page is now a slice of the wrapper classes, and each class
comes with every call it makes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import insert_refs_batch, insert_symbols_batch
from tests._paging import CH, assert_whole_and_once, make_project, symbol_row, walk_pages

WRAPPERS = 5
# The first wrapper has more methods with calls than the old cut of 50 sites.
BIG_METHODS = 60


def _fill(conn, file_ids: list[int]) -> None:
    fid = file_ids[0]
    path = "src/f0.c"
    symbols = [
        symbol_row(fid, path, "UART_DRIVER", "UART_DRIVER", "c:@S@UART_DRIVER", 1, kind="class"),
        symbol_row(fid, path, "write", "UART_DRIVER::write", "c:@S@UART_DRIVER@F@write", 2,
                   kind="method", parent_usr="c:@S@UART_DRIVER"),
    ]
    refs = []
    line = 100
    for w in range(WRAPPERS):
        cls = f"Wrap{w}"
        symbols.append(symbol_row(fid, path, cls, cls, f"c:@S@{cls}", 10 + w, kind="class"))
        for m in range(BIG_METHODS if w == 0 else 2):
            usr = f"c:@S@{cls}@F@m{m:02d}"
            symbols.append(symbol_row(fid, path, f"m{m:02d}", f"{cls}::m{m:02d}", usr, line,
                                      kind="method", parent_usr=f"c:@S@{cls}"))
            refs.append((CH, "c:@S@UART_DRIVER@F@write", path, line + 1, usr, "call", None))
            line += 3
    insert_symbols_batch(conn, symbols)
    insert_refs_batch(conn, refs)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill)


def _call(project: Path, offset: int, limit: int = 2) -> list[dict]:
    from fw_context_mcp.mcp.handlers.callgraph import find_wrapper_callers

    return find_wrapper_callers("UART_DRIVER", project_root=str(project), limit=limit, offset=offset)


def test_the_classes_walk_whole_and_once(project):
    seen, total = walk_pages(lambda offset: _call(project, offset), lambda r: r["wrapper_class"])
    assert total == WRAPPERS
    assert_whole_and_once(seen, total)


def test_a_class_with_many_calls_is_whole(project):
    """60 methods call the driver: more than the 50 call sites the tool used to read."""
    rows = _call(project, 0, limit=1)
    wrapper = next(r for r in rows if r.get("wrapper_class") == "Wrap0")
    assert wrapper["method_count"] == BIG_METHODS
    assert len(wrapper["methods"]) == BIG_METHODS


def test_the_hint_names_the_next_call(project):
    assert _call(project, 0)[0]["hint"] == (
        "find_wrapper_callers('UART_DRIVER', offset=2) reads the next page."
    )


def test_a_page_after_the_end_names_the_total(project):
    assert _call(project, 30) == [
        {"info": f"No wrapper class at offset 30; the answer holds {WRAPPERS}."}
    ]
