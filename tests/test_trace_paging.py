"""trace_data_flow pages its source functions.

The tool traced at most 15 source functions and said nothing about the
rest.  A common type in a signature, such as a struct that many drivers
take, matches far more than 15 definitions.  The order stopped at
``caller_count``, and most functions have one or two callers, thus the
order tied on almost every row.  Here all nine sources tie at zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import insert_refs_batch, insert_symbols_batch
from tests._paging import CH, assert_whole_and_once, make_project, notice, symbol_row, walk_pages, with_project

SOURCES = 9


def _fill(conn, file_ids: list[int]) -> None:
    fid = file_ids[0]
    rows = [symbol_row(fid, "src/f0.c", "uart_send", "uart_send", "c:@F@uart_send", 1,
                       signature="void uart_send(const uint8_t *buf)")]
    rows += [
        symbol_row(fid, "src/f0.c", f"read{i}", f"read{i}", f"c:@F@read{i}", 10 + 3 * i,
                   signature=f"void read{i}(SensorData *out)")
        for i in range(SOURCES)
    ]
    rows.append(symbol_row(fid, "src/f0.c", "main", "main", "c:@F@main", 200))
    insert_symbols_batch(conn, rows)
    # The tool needs the reference index; this call reaches no source.
    insert_refs_batch(conn, [(CH, "c:@F@uart_send", "src/f0.c", 201, "c:@F@main", "call", None)])


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill)


def _call(project: Path, offset: int, **kw) -> list[dict]:
    from fw_context_mcp.mcp.handlers.callgraph import trace_data_flow

    return trace_data_flow("SensorData", "uart_send", project_root=str(project),
                           limit=4, offset=offset, **kw)


def test_the_sources_walk_whole_and_once(project):
    seen, total = walk_pages(
        lambda offset: _call(project, offset),
        lambda r: r["source_qualified_name"],
        is_answer=lambda r: "source_name" in r,
    )
    assert total == SOURCES
    assert_whole_and_once(seen, total)


def test_the_hint_repeats_the_target_and_a_depth_that_is_not_the_default(project):
    assert notice(_call(project, 0))["hint"] == (
        with_project("trace_data_flow('SensorData', 'uart_send', offset=4) reads the next page.", project)
    )
    assert notice(_call(project, 0, max_depth=3))["hint"] == (
        with_project("trace_data_flow('SensorData', 'uart_send', max_depth=3, offset=4) reads the next page.", project)
    )


def test_the_hint_default_is_the_default_of_the_tool():
    """The hint leaves out the default depth; the two defaults must agree."""
    import inspect

    from fw_context_mcp.mcp.handlers.callgraph import _DEFAULT_TRACE_DEPTH, trace_data_flow

    assert inspect.signature(trace_data_flow).parameters["max_depth"].default == _DEFAULT_TRACE_DEPTH


def test_the_summary_is_about_the_page(project):
    summary = next(r for r in _call(project, 0) if "_summary" in r)
    assert summary["_summary"].startswith("0/4 source functions on this page")


def test_a_page_after_the_end_names_the_total(project):
    assert _call(project, 40) == [
        {"info": f"No source function at offset 40; the answer holds {SOURCES}."}
    ]
