"""get_symbol_context and get_source say when a list of the answer is cut.

The lists of these one-body tools are cut: callers at 50, callees at 100,
indirect call sites at 200, enum constants at 200.  The cut was silent,
thus a hot function with 60 callers read as one with 50.  Each list now
has a ``<list>_total``, and a ``<list>_hint`` names the tool that pages
it when the list is cut.

``callees`` gave one row per CALL, and the row has no line, thus two
calls of one callee gave two rows that nobody could tell apart.  It now
gives one row per callee and kind of reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import insert_refs_batch, insert_symbols_batch
from tests._paging import CH, make_project, symbol_row, with_project

CALLERS = 60
CALLEES = 120
CONSTANTS = 250


def _fill(conn, file_ids: list[int]) -> None:
    fid = file_ids[0]
    path = "src/f0.c"
    symbols = [symbol_row(fid, path, "hub", "hub", "c:@F@hub", 1)]
    refs = []
    for i in range(CALLERS):
        usr = f"c:@F@caller{i:03d}"
        symbols.append(symbol_row(fid, path, f"caller{i:03d}", f"caller{i:03d}", usr, 10 + i))
        refs.append((CH, "c:@F@hub", path, 10 + i, usr, "call", None))
    for i in range(CALLEES):
        usr = f"c:@F@callee{i:03d}"
        symbols.append(symbol_row(fid, path, f"callee{i:03d}", f"callee{i:03d}", usr, 100 + i))
        refs.append((CH, usr, path, 2, "c:@F@hub", "call", None))
    # A second call of one callee: the same row, and it must not count twice.
    refs.append((CH, "c:@F@callee000", path, 3, "c:@F@hub", "call", None))
    symbols.append(symbol_row(fid, path, "reg_id_t", "reg_id_t", "c:@E@reg_id_t", 300, kind="enum"))
    symbols += [
        symbol_row(fid, path, f"REG_{i:03d}", f"reg_id_t::REG_{i:03d}", f"c:@E@reg_id_t@REG_{i:03d}",
                   301 + i, kind="enum_constant")
        for i in range(CONSTANTS)
    ]
    # Two enums whose constants the old patterns took for constants of
    # reg_id_t.  Their lines come before REG_000, thus a wrong match would
    # lead the list.
    # - regXid_t differs where reg_id_t has an underscore: LIKE without
    #   ESCAPE read that underscore as any character.
    # - xreg_id_t holds the whole name: the pattern %reg_id_t::% matched it,
    #   as it matched rtc_gpio_mode_t for gpio_mode_t on a real index.
    for enum, const, line in (("regXid_t", "OTHER", 290), ("xreg_id_t", "FOREIGN", 295)):
        symbols.append(symbol_row(fid, path, enum, enum, f"c:@E@{enum}", line, kind="enum"))
        symbols.append(symbol_row(fid, path, const, f"{enum}::{const}", f"c:@E@{enum}@{const}",
                                  line + 1, kind="enum_constant"))
    insert_symbols_batch(conn, symbols)
    insert_refs_batch(conn, refs)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill)


def _context(project: Path, name: str) -> dict:
    from fw_context_mcp.mcp.handlers.source import get_symbol_context

    return get_symbol_context(name, project_root=str(project))


def test_the_callers_say_that_they_are_cut(project):
    result = _context(project, "hub")
    assert len(result["callers"]) == 50
    assert result["callers_total"] == CALLERS
    assert result["callers_hint"] == with_project("find_callers('hub') pages the callers.", project)


def test_each_callee_comes_once(project):
    result = _context(project, "hub")
    names = [c["name"] for c in result["callees"]]
    assert len(names) == len(set(names)) == 100
    assert result["callees_total"] == CALLEES
    assert result["callees_hint"] == (
        with_project("find_callees_recursive('hub', max_depth=1) pages the callees.", project)
    )


def test_a_list_that_is_whole_has_no_hint(project):
    result = _context(project, "caller000")
    assert result["callers_total"] == 0
    assert result["callees_total"] == 1
    assert "callers_hint" not in result
    assert "callees_hint" not in result
    assert result["indirect_call_sites_total"] == 0


@pytest.mark.parametrize("tool", ["get_source", "get_symbol_context"])
def test_the_constants_say_that_they_are_cut(project, tool):
    from fw_context_mcp.mcp.handlers import source

    result = getattr(source, tool)("reg_id_t", project_root=str(project))
    assert len(result["constants"]) == 200
    assert result["constants_total"] == CONSTANTS
    assert result["constants_hint"] == with_project("lookup_symbol('reg_id_t::') pages every constant.", project)
    names = {c["name"] for c in result["constants"]}
    assert "OTHER" not in names, "LIKE read _ as any character"
    assert "FOREIGN" not in names, "a constant of xreg_id_t is no constant of reg_id_t"


def test_the_constants_keep_the_order_of_the_source(project):
    from fw_context_mcp.mcp.handlers.source import get_source

    names = [c["name"] for c in get_source("reg_id_t", project_root=str(project))["constants"]]
    assert names == [f"REG_{i:03d}" for i in range(200)]
