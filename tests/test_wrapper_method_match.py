"""find_wrapper_callers takes the methods of the named class, not of a class with a similar name.

The tool matched the methods with LIKE.  LIKE reads ``_`` as any
character and ignores the case of ASCII letters, and the namespace tier
``%<class>::%`` did not ask for ``::`` before the name.  Thus a driver
got the methods, and so the wrappers, of another class:

* ``I2C_Base`` also got ``I2CxBase``.
* ``Timer`` also got ``TIMER``.
* ``Gap`` also got ``ble::impl::PalGap``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import insert_refs_batch, insert_symbols_batch
from tests._paging import CH, make_project, symbol_row

# (class to ask for, its qualified name, a class that LIKE also matched)
CASES = [
    ("I2C_Base", "I2C_Base", "I2CxBase"),
    ("Timer", "Timer", "TIMER"),
    ("Gap", "ble::Gap", "ble::impl::PalGap"),
]


def _usr(qualified_name: str) -> str:
    return "c:@S@" + qualified_name.replace("::", "@S@")


def _fill(conn, file_ids: list[int]) -> None:
    fid = file_ids[0]
    path = "src/f0.c"
    symbols = []
    refs = []
    line = 10
    for _, right, decoy in CASES:
        for owner in (right, decoy):
            short = owner.rsplit("::", 1)[-1]
            wrapper = "Wrap" + owner.replace("::", "")
            symbols += [
                symbol_row(fid, path, short, owner, _usr(owner), line, kind="class"),
                symbol_row(fid, path, "op", f"{owner}::op", _usr(owner) + "@F@op", line + 1,
                           kind="method", parent_usr=_usr(owner)),
                symbol_row(fid, path, wrapper, wrapper, _usr(wrapper), line + 2, kind="class"),
                symbol_row(fid, path, "go", f"{wrapper}::go", _usr(wrapper) + "@F@go", line + 3,
                           kind="method", parent_usr=_usr(wrapper)),
            ]
            refs.append((CH, _usr(owner) + "@F@op", path, line + 4, _usr(wrapper) + "@F@go",
                         "call", None))
            line += 10
    insert_symbols_batch(conn, symbols)
    insert_refs_batch(conn, refs)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path, _fill)


@pytest.mark.parametrize(("asked", "right", "decoy"), CASES)
def test_only_the_named_class_gives_its_wrappers(project, asked, right, decoy):
    from fw_context_mcp.mcp.handlers.callgraph import find_wrapper_callers

    rows = find_wrapper_callers(asked, project_root=str(project))
    wrappers = [r["wrapper_class"] for r in rows if "wrapper_class" in r]
    assert wrappers == ["Wrap" + right.replace("::", "")], rows
