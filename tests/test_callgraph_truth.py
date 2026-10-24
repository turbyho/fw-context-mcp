"""The call edges of the index against a known truth.

Each case in tests/fixtures/callgraph/ is indexed as a project, with the full
pipeline: the skip set of headers and the post-processing take part, because
two of the measured false edges come from them (a header in the skip set hides
a symbol from the name lookup of the text heuristic; the cross-TU backfill runs
in post-processing). A test of extract_all alone would miss both.

A case with the key "xfail" names the phase that repairs it. The test is then
xfail(strict=True): the commit of that phase must remove the key, because a
strict xfail fails as soon as the case passes.
"""

from __future__ import annotations

import collections
import json
import shutil
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import open_db

pytest.importorskip("clang.cindex", reason="libclang not available")

CASES = Path(__file__).parent / "fixtures" / "callgraph"
Edge = tuple[str | None, str, int, str]


def _case_dirs() -> list[Path]:
    return sorted(p for p in CASES.iterdir() if (p / "case.json").is_file())


def _load(case_dir: Path) -> dict:
    return json.loads((case_dir / "case.json").read_text(encoding="utf-8"))


def _index(case_dir: Path, work: Path, units: list[str]) -> list[Edge]:
    """Index the case in *work* with *units*, and give its call, indirect and implicit_construct edges.

    These are the kinds that find_callers answers with.
    """
    from fw_context_mcp.indexer.runner import run

    case = _load(case_dir)
    project = work / "project"
    shutil.copytree(case_dir, project)
    compiler = case["compiler"]
    std = ["-std=c++17"] if compiler == "c++" else ["-std=gnu11"]
    entries = [
        {
            "directory": str(project),
            "file": unit,
            "arguments": [compiler, *std, *case.get("flags", []), "-c", unit, "-o", f"{unit}.o"],
        }
        for unit in units
    ]
    (project / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
    db_path = work / "db" / "index.db"
    db_path.parent.mkdir()
    config_hash = run(
        compile_commands=project / "compile_commands.json",
        db_path=db_path,
        project_root=project,
        project_id="truth",
        project_name=case_dir.name,
        index_refs=True,
        index_embeddings=False,
        analyze_symbols=False,
        record_git=False,
    )
    case_files = {p.name for p in case_dir.iterdir() if p.suffix in (".c", ".cpp", ".h")}
    conn = open_db(db_path)
    try:
        # A symbol of the case: one whose declaration or definition is in a
        # file of the case. A call into the C library is not part of the truth.
        own = {
            row["usr"]: row["qualified_name"]
            for row in conn.execute(
                """SELECT s.usr, s.qualified_name, f.path FROM symbols s JOIN files f ON f.id = s.file_id
                   WHERE s.config_hash = ?""",
                (config_hash,),
            )
            if Path(row["path"]).name in case_files
        }
        rows = conn.execute(
            """SELECT from_usr, to_usr, from_line, ref_kind FROM refs
               WHERE config_hash = ? AND ref_kind IN ('call', 'indirect', 'implicit_construct')""",
            (config_hash,),
        ).fetchall()
    finally:
        conn.close()
    return [
        (own.get(r["from_usr"]) if r["from_usr"] else None, own[r["to_usr"]], r["from_line"], r["ref_kind"])
        for r in rows
        if r["to_usr"] in own and (r["from_usr"] is None or r["from_usr"] in own)
    ]


def _as_edges(items: list[list]) -> list[Edge]:
    return [(caller, callee, line, kind) for caller, callee, line, kind in items]


def _check(case: dict, edges: list[Edge]) -> None:
    actual = collections.Counter(edges)
    present = collections.Counter(_as_edges(case["present"]))
    missing = present - actual
    found_absent = [e for e in _as_edges(case["absent"]) if actual[e]]
    assert not missing, f"missing edges: {sorted(missing.elements(), key=str)}; index has {sorted(edges, key=str)}"
    assert not found_absent, f"edges that must not exist: {found_absent}"
    if case.get("exact"):
        extra = actual - present
        assert not extra, f"edges beyond the truth: {sorted(extra.elements(), key=str)}"


def _params() -> list:
    params = []
    for case_dir in _case_dirs():
        case = _load(case_dir)
        marks = []
        if case.get("xfail"):
            marks.append(pytest.mark.xfail(strict=True, reason=f"repaired in {case['xfail']}"))
        params.append(pytest.param(case_dir, id=case_dir.name, marks=marks))
    return params


@pytest.mark.libclang
@pytest.mark.parametrize("case_dir", _params())
def test_the_call_edges_equal_the_truth(case_dir: Path, tmp_path: Path):
    case = _load(case_dir)
    _check(case, _index(case_dir, tmp_path, case["units"]))


@pytest.mark.libclang
@pytest.mark.xfail(strict=True, reason="repaired in phase 1")
def test_another_unit_does_not_change_the_edges_of_a_function(tmp_path: Path):
    """The edges of Driver::off come from its code, not from the other units of the build.

    The runner sorts the units by path. With parser.cpp in the build, it walks
    parser.h first, and parser.h is in the skip set when zapp.cpp walks
    driver.h. Without parser.cpp, zapp.cpp walks parser.h itself. An edge
    that changes with that is not a fact of the code of Driver::off.
    """
    case_dir = CASES / "skip_set_header"
    alone = _index(case_dir, tmp_path / "a", ["zapp.cpp"])
    with_parser = _index(case_dir, tmp_path / "b", ["parser.cpp", "zapp.cpp"])

    def of_off(edges: list[Edge]) -> collections.Counter:
        return collections.Counter(e for e in edges if e[0] == "Driver::off")

    assert of_off(alone) == of_off(with_parser)
