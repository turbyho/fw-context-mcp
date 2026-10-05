"""get_template_instances pages the instances of a template.

The tool cut the instances at ``limit``, and ``instance_count`` counted
the rows it returned, thus a full page read as all the instances there
are.  An implicit instantiation often has the file and line of the
template itself, thus the instances also tie on ``file_path, line``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import insert_symbols_batch
from tests._paging import assert_whole_and_once, make_project, notice, symbol_row, walk_pages

INSTANCES = 11
TEMPLATE_USR = "c:@ST>1#T@Callback"


def _fill(conn, file_ids: list[int]) -> None:
    fid = file_ids[0]
    rows = [symbol_row(fid, "src/f0.c", "Callback", "mbed::Callback", TEMPLATE_USR, 10,
                       kind="class", is_template=1)]
    # Every instance on the line of the template: they all tie on file and line.
    rows += [
        symbol_row(fid, "src/f0.c", "Callback", f"mbed::Callback<void(int{i})>",
                   f"c:@S@Callback>#{i}", 10, kind="class", template_usr=TEMPLATE_USR)
        for i in range(INSTANCES)
    ]
    insert_symbols_batch(conn, rows)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill)


def _instances(rows: list[dict]) -> list[dict]:
    return [i for r in rows if "instances" in r for i in r["instances"]]


def _call(project: Path, offset: int, limit: int = 4) -> list[dict]:
    from fw_context_mcp.mcp.handlers.inheritance import get_template_instances

    return get_template_instances("Callback", project_root=str(project), limit=limit, offset=offset)


def test_the_instances_walk_whole_and_once(project):
    def page(offset: int) -> list[dict]:
        rows = _call(project, offset)
        # The instances are nested in one wrapper dict; lift them for the walk.
        return [r for r in rows if "instances" not in r] + _instances(rows)

    seen, total = walk_pages(page, lambda r: r["qualified_name"])

    assert total == INSTANCES
    assert_whole_and_once(seen, total)


def test_instance_count_is_every_instance_and_not_the_page(project):
    rows = _call(project, 0)
    wrapper = next(r for r in rows if "instances" in r)
    assert len(wrapper["instances"]) == 4
    assert wrapper["instance_count"] == INSTANCES


def test_the_hint_names_the_next_call(project):
    assert notice(_call(project, 0))["hint"] == (
        "get_template_instances('Callback', offset=4) reads the next page."
    )


def test_a_page_after_the_end_names_the_total(project):
    assert _call(project, 50) == [
        {"info": f"No instance at offset 50; the answer holds {INSTANCES}."}
    ]
