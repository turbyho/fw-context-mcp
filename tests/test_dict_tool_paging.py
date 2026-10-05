"""The tools that answer with a dict page their long list under the key ``page``.

get_class_members, get_inheritance_chain (transitive) and get_file_map
returned every item, or a cut with no way to the rest.  A generated
struct of a vendor header holds hundreds of members, a root class of a
framework has thousands of descendants, and a device header holds
thousands of symbols of one kind.  The page notice of a dict answer is
the value of ``page`` (``all_bases_page`` / ``all_derived_page`` for the
two lists of the chain), with the same keys as in a list answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import insert_inheritance_batch, insert_symbols_batch
from tests._paging import CH, assert_whole_and_once, make_project, symbol_row

MEMBERS = 23        # with overloads: three members share the name "set"
DERIVED = 9         # direct children of Root
GRANDCHILDREN = 2   # children of each direct child
FUNCTIONS = 70      # functions in src/f1.c
CONSTANTS = 40      # constants of mode_t in src/f1.c


def _fill(conn, file_ids: list[int]) -> None:
    f0, f1 = file_ids
    p0, p1 = "src/f0.c", "src/f1.c"
    rows = [symbol_row(f0, p0, "Regs", "Regs", "c:@S@Regs", 1, kind="struct")]
    rows += [
        symbol_row(f0, p0, "set", "Regs::set", f"c:@S@Regs@F@set#{i}", 2, kind="method",
                   parent_usr="c:@S@Regs", signature=f"void set(int{i})")
        for i in range(3)
    ]
    rows += [
        symbol_row(f0, p0, f"r{i:02d}", f"Regs::r{i:02d}", f"c:@S@Regs@FI@r{i:02d}", 10 + i,
                   kind="field", parent_usr="c:@S@Regs")
        for i in range(MEMBERS - 3)
    ]
    rows.append(symbol_row(f0, p0, "Root", "Root", "c:@S@Root", 100, kind="class"))
    edges = []
    for i in range(DERIVED):
        child = f"c:@S@D{i}"
        rows.append(symbol_row(f0, p0, f"D{i}", f"D{i}", child, 110 + i, kind="class"))
        edges.append((CH, child, "c:@S@Root", "public", 0))
        for j in range(GRANDCHILDREN):
            grand = f"c:@S@D{i}G{j}"
            # Every grandchild has one name: the walk ties on depth and name.
            rows.append(symbol_row(f0, p0, "Leaf", f"Leaf{i}{j}", grand, 130 + 2 * i + j,
                                   kind="class"))
            edges.append((CH, grand, child, "public", 0))
    rows += [
        symbol_row(f1, p1, f"fn{i:02d}", f"fn{i:02d}", f"c:@F@fn{i:02d}", 1 + i)
        for i in range(FUNCTIONS)
    ]
    rows.append(symbol_row(f1, p1, "mode_t", "mode_t", "c:@E@mode_t", 200, kind="enum"))
    rows += [
        symbol_row(f1, p1, f"MODE_{i:02d}", f"mode_t::MODE_{i:02d}", f"c:@E@mode_t@MODE_{i:02d}",
                   201 + i, kind="enum_constant")
        for i in range(CONSTANTS)
    ]
    insert_symbols_batch(conn, rows)
    insert_inheritance_batch(conn, edges)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill, files=2)


def _walk_dict(call, items_of, page_key: str = "page") -> tuple[list, int]:
    """Walk the pages of a dict answer; each page must hold its notice under *page_key*."""
    seen: list = []
    offset = 0
    totals: set[int] = set()
    while True:
        result = call(offset)
        page = result[page_key]
        totals.add(page["total"])
        assert page["offset"] == offset
        items = items_of(result)
        assert page["shown"] == len(items)
        assert ("hint" in page) == page["more"]
        seen += items
        offset += len(items)
        if not page["more"]:
            break
    assert len(totals) == 1, totals
    return seen, totals.pop()


def test_the_members_walk_whole_and_once(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_class_members

    seen, total = _walk_dict(
        lambda offset: get_class_members("Regs", project_root=str(project), limit=5, offset=offset),
        lambda r: [m["qualified_name"] + m["signature"] for ms in r["members"].values() for m in ms],
    )
    assert total == MEMBERS
    assert_whole_and_once(seen, total)


def test_member_count_is_every_member(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_class_members

    result = get_class_members("Regs", project_root=str(project), limit=5)
    assert result["member_count"] == MEMBERS
    assert result["page"]["hint"] == "get_class_members('Regs', offset=5) reads the next page."


def test_a_members_page_after_the_end_names_the_total(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_class_members

    result = get_class_members("Regs", project_root=str(project), offset=99)
    assert result == {"info": f"No member at offset 99; the answer holds {MEMBERS}."}


def test_the_descendants_walk_whole_and_once(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_inheritance_chain

    seen, total = _walk_dict(
        lambda offset: get_inheritance_chain("Root", project_root=str(project), transitive=True,
                                             limit=4, offset=offset),
        lambda r: [d["usr"] for d in r["all_derived"]],
        page_key="all_derived_page",
    )
    assert total == DERIVED * (1 + GRANDCHILDREN)
    assert_whole_and_once(seen, total)


def test_the_descendants_come_by_depth(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_inheritance_chain

    result = get_inheritance_chain("Root", project_root=str(project), transitive=True, limit=500)
    depths = [d["depth"] for d in result["all_derived"]]
    assert depths == sorted(depths)
    assert result["all_bases_page"] == {"total": 0, "offset": 0, "shown": 0, "more": False}
    assert result["all_derived_page"]["more"] is False


def test_the_chain_hint_keeps_transitive(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_inheritance_chain

    result = get_inheritance_chain("Root", project_root=str(project), transitive=True, limit=4)
    assert result["all_derived_page"]["hint"] == (
        "get_inheritance_chain('Root', transitive=True, offset=4) reads the next page."
    )


def test_a_file_map_group_that_is_cut_names_the_call_for_the_rest(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    result = get_file_map("src/f1.c", project_root=str(project))
    group = result["symbols"]["function"]
    assert group["count"] == FUNCTIONS
    assert len(group["items"]) == 30
    assert group["hint"] == "get_file_map('src/f1.c', kind='function', offset=30) reads the next page."


def test_the_items_of_one_kind_walk_whole_and_once(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    seen, total = _walk_dict(
        lambda offset: get_file_map("src/f1.c", project_root=str(project), kind="function",
                                    max_per_kind=30, offset=offset),
        lambda r: [i["qualified_name"] for i in r["items"]],
    )
    assert total == FUNCTIONS
    assert_whole_and_once(seen, total)


def test_a_file_map_page_after_the_end_names_the_total(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    result = get_file_map("src/f1.c", project_root=str(project), kind="function", offset=500)
    assert result == {"info": f"No function at offset 500; the answer holds {FUNCTIONS}."}


def test_a_cut_enum_constant_group_names_the_call_from_the_first_constant(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    group = get_file_map("src/f1.c", project_root=str(project))["symbols"]["enum_constant"]
    assert group["count"] == CONSTANTS
    assert group["hint"] == (
        "get_file_map('src/f1.c', kind='enum_constant', offset=0) reads the next page."
    )


def test_the_constants_of_a_file_page_flat(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    seen, total = _walk_dict(
        lambda offset: get_file_map("src/f1.c", project_root=str(project), kind="enum_constant",
                                    max_per_kind=15, offset=offset),
        lambda r: [i["qualified_name"] for i in r["items"]],
    )
    assert total == CONSTANTS
    assert_whole_and_once(seen, total)


def test_the_file_map_hint_keeps_the_signatures(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    result = get_file_map("src/f1.c", project_root=str(project), kind="function", signatures=True)
    assert result["page"]["hint"] == (
        "get_file_map('src/f1.c', kind='function', signatures=True, offset=30) reads the next page."
    )
    assert all("signature" in i for i in result["items"])


def test_a_kind_that_the_file_does_not_hold_names_the_kinds_it_holds(project):
    from fw_context_mcp.mcp.handlers.source import get_file_map

    result = get_file_map("src/f1.c", project_root=str(project), kind="functions")
    assert result == {"info": "No symbol of kind 'functions' in src/f1.c. "
                              "Kinds in this file: enum, enum_constant, function."}


def test_the_chain_hint_keeps_a_depth_that_is_not_the_default(project):
    from fw_context_mcp.mcp.handlers.inheritance import get_inheritance_chain

    result = get_inheritance_chain("Root", project_root=str(project), transitive=True,
                                   max_depth=3, limit=4)
    assert result["all_derived_page"]["hint"] == (
        "get_inheritance_chain('Root', transitive=True, max_depth=3, offset=4) reads the next page."
    )
