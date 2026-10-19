"""The owner of a shared header after an incremental run equals a full run.

The first unit that walks a header claims it and writes its rows.  A header
whose text depends on the defines of the unit gives each unit different
rows, thus the owner decides the content of the index.  An incremental run
must keep the owner of a full run: before db/_owners.py, the first changed
unit claimed every header that it included, also a header that an earlier,
unchanged unit owns.

Fixture: three units include ``shared.h``.  Only ``a.c`` compiles with
``-DUNIT_A``, and ``shared.h`` defines other entities under that define.
The units are parsed in the order of their path, thus ``a.c`` owns
``shared.h`` in a full run.

Each test changes the project, runs an incremental index, and compares it
with a rebuild from an empty database (see test_index_equivalence.py for why
``--force`` is not a reference).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import open_db

# Fixture helpers are imported for the same reason as in
# test_index_equivalence.py: they build the fake project there.
from tests.test_incremental_reindex import (
    _advance_mtime,
    _cli,
    _db_path_for_project,
    _isolate_index_db,
    _write_file,
)
from tests.test_index_equivalence import (
    _content_snapshot,
    _index,
    _macro_snapshot,
    _rebuild_from_scratch,
    _snapshot,
)

_SHARED_H = """\
#ifndef SHARED_H
#define SHARED_H

#ifdef UNIT_A
#define SHARED_KIND 1
struct shared_cfg { int a_only; };
static inline int shared_a(void) { return 1; }
#else
#define SHARED_KIND 2
struct shared_cfg { int other; };
static inline int shared_other(void) { return 2; }
#endif

#endif
"""

_EXTRA_H = """\
#ifndef EXTRA_H
#define EXTRA_H

#ifdef UNIT_A
static inline int extra_a(void) { return 3; }
#else
static inline int extra_other(void) { return 4; }
#endif

#endif
"""


def _unit_source(name: str, includes: tuple[str, ...]) -> str:
    lines = [f'#include "{header}"' for header in includes]
    lines += ["", f"int {name}_entry(void) {{ return SHARED_KIND; }}", ""]
    return "\n".join(lines)


def _entry(src: Path, name: str, defines: tuple[str, ...]) -> dict:
    return {
        "directory": str(src),
        "file": f"{name}.c",
        "arguments": ["gcc", "-std=c11", *defines, "-c", f"{name}.c", "-o", f"build/{name}.o"],
    }


_UNITS: dict[str, tuple[str, ...]] = {
    "a": ("-DUNIT_A",),
    "b": (),
    "c": (),
}


def _write_cc(proj: Path, names: list[str]) -> None:
    """Write compile_commands.json with the units in the order of *names*."""
    src = proj / "src"
    cc = [_entry(src, name, _UNITS[name]) for name in names]
    (proj / "compile_commands.json").write_text(json.dumps(cc, indent=2), encoding="utf-8")


@pytest.fixture
def owned_project(tmp_path: Path):
    """Create and index the project of the module docstring."""
    proj = tmp_path / "owners"
    src = proj / "src"
    src.mkdir(parents=True)
    _write_file(src / "shared.h", _SHARED_H)
    _write_file(src / "extra.h", _EXTRA_H)
    _write_file(src / "a.c", _unit_source("a", ("shared.h", "extra.h")))
    _write_file(src / "b.c", _unit_source("b", ("shared.h",)))
    _write_file(src / "c.c", _unit_source("c", ("shared.h",)))
    # compile_commands.json in reverse order: the owner must follow the path
    # of the unit, not the order of this file.
    _write_cc(proj, ["c", "b", "a"])
    _isolate_index_db(proj, tmp_path)
    init = _cli(["init", "--project", str(proj)], cwd=proj, timeout=60)
    assert init.returncode == 0, init.stderr
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    return proj


def _summary(result) -> str:
    """Return the "N updated, M unchanged, K skipped" line of an index run."""
    assert result.returncode == 0, result.stderr
    for line in result.stderr.splitlines():
        if " updated, " in line and " unchanged, " in line:
            return line
    raise AssertionError("no summary line\n" + result.stderr[-3000:])


def _names(db_path: Path) -> set[str]:
    return {row[1] for row in _snapshot(db_path)}


def _edit(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    _advance_mtime(path, 10.0)


def _assert_equals_rebuild(proj: Path) -> None:
    """Compare the index after an incremental run with a rebuild."""
    db_path = _db_path_for_project(proj)
    incr = (_snapshot(db_path), _macro_snapshot(db_path), _content_snapshot(db_path))
    fresh_db = _rebuild_from_scratch(proj)
    full = (_snapshot(fresh_db), _macro_snapshot(fresh_db), _content_snapshot(fresh_db))
    for label, incr_rows, full_rows in zip(("symbols", "macros"), incr[:2], full[:2], strict=True):
        assert not (full_rows - incr_rows), f"incremental MISSED {label}: {sorted(full_rows - incr_rows)[:8]}"
        assert not (incr_rows - full_rows), f"incremental kept STALE {label}: {sorted(incr_rows - full_rows)[:8]}"
    differing = sorted(p for p in incr[2].keys() | full[2].keys() if incr[2].get(p) != full[2].get(p))
    assert not differing, f"stored content differs for {differing}"


def test_the_first_unit_by_path_owns_the_shared_header(owned_project: Path):
    """``a.c`` is last in compile_commands.json and first by path: it owns the header."""
    names = _names(_db_path_for_project(owned_project))
    assert "shared_a" in names
    assert "shared_other" not in names


def test_a_change_of_a_later_unit_keeps_the_owner(owned_project: Path):
    """``b.c`` changes: the incremental run must not rewrite ``shared.h`` for ``b.c``."""
    proj = owned_project
    _edit(proj / "src" / "b.c", _unit_source("b", ("shared.h",)) + "int b_more(void) { return 5; }\n")
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    names = _names(_db_path_for_project(proj))
    assert "b_more" in names, "the edit did not reach the index"
    assert "shared_a" in names
    assert "shared_other" not in names
    _assert_equals_rebuild(proj)


def test_a_change_of_the_owner_keeps_the_owner(owned_project: Path):
    proj = owned_project
    _edit(proj / "src" / "a.c", _unit_source("a", ("shared.h", "extra.h")) + "int a_more(void) { return 6; }\n")
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    _assert_equals_rebuild(proj)


def test_a_later_unit_that_adds_an_include_does_not_take_the_header(owned_project: Path):
    """``c.c`` now includes ``extra.h``, which ``a.c`` owns."""
    proj = owned_project
    _edit(proj / "src" / "c.c", _unit_source("c", ("shared.h", "extra.h")))
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    names = _names(_db_path_for_project(proj))
    assert "extra_a" in names
    assert "extra_other" not in names
    _assert_equals_rebuild(proj)


def test_the_next_includer_takes_a_header_that_its_owner_drops(owned_project: Path):
    """``a.c`` no longer includes ``shared.h``: ``b.c`` owns it, also when ``b.c`` did not change."""
    proj = owned_project
    _edit(proj / "src" / "a.c", _unit_source("a", ("extra.h",)).replace("SHARED_KIND", "0"))
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    names = _names(_db_path_for_project(proj))
    assert "shared_other" in names
    assert "shared_a" not in names
    _assert_equals_rebuild(proj)


def test_a_header_that_no_unit_includes_now_loses_its_rows(owned_project: Path):
    """Only ``a.c`` includes ``extra.h``; when it drops the include, the rows go."""
    proj = owned_project
    _edit(proj / "src" / "a.c", _unit_source("a", ("shared.h",)))
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    names = _names(_db_path_for_project(proj))
    assert "extra_a" not in names
    _assert_equals_rebuild(proj)


def test_the_next_includer_takes_the_headers_of_a_unit_that_left(owned_project: Path):
    """``a.c`` leaves compile_commands.json: ``b.c`` owns ``shared.h``."""
    proj = owned_project
    _write_cc(proj, ["c", "b"])
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    names = _names(_db_path_for_project(proj))
    assert "shared_other" in names
    assert "shared_a" not in names
    _assert_equals_rebuild(proj)


def test_the_owner_keeps_the_header_when_its_flags_change(owned_project: Path, monkeypatch):
    """``a.c`` gets one more include path: a new listing of the same first file.

    An include path is in the flags hash of the listing and not in
    config_hash (a define is in both, and a new config_hash indexes the
    whole build).  The new listing must take the files of the old one, and
    the run must parse no other unit for them.
    """
    proj = owned_project
    (proj / "src" / "extra").mkdir()
    monkeypatch.setitem(_UNITS, "a", ("-DUNIT_A", "-Iextra"))
    _write_cc(proj, ["c", "b", "a"])
    result = _index(proj)
    assert "1 updated, 2 unchanged" in _summary(result)
    names = _names(_db_path_for_project(proj))
    assert "shared_a" in names
    assert "shared_other" not in names
    _assert_equals_rebuild(proj)


def test_a_file_listed_two_times_with_the_same_flags_keeps_its_headers(owned_project: Path):
    """Zephyr lists ``misc/empty_file.c`` so: the same flags, another ``-o``.

    Before number_listings, the two listings had one key, and the claims of
    the second (nothing new) replaced the claims of the first: ``shared.h``
    had no owner, and the next change of ``b.c`` rewrote it for ``b.c``.
    """
    proj = owned_project
    src = proj / "src"
    second_a = _entry(src, "a", _UNITS["a"])
    second_a["arguments"][-1] = "build/a_again.o"
    cc = [_entry(src, name, _UNITS[name]) for name in ("c", "b", "a")] + [second_a]
    (proj / "compile_commands.json").write_text(json.dumps(cc, indent=2), encoding="utf-8")
    first = _index(proj)
    assert first.returncode == 0, first.stderr
    _edit(src / "b.c", _unit_source("b", ("shared.h",)) + "int b_more(void) { return 5; }\n")
    result = _index(proj)
    assert "1 updated" in _summary(result)
    names = _names(_db_path_for_project(proj))
    assert "shared_a" in names
    assert "shared_other" not in names
    _assert_equals_rebuild(proj)


def test_a_file_that_lost_its_owner_in_a_stopped_run_goes_to_its_first_includer(owned_project: Path):
    """A run stopped after ``a.c`` gave ``shared.h`` away and before the heir claimed it.

    The rows of ``shared.h`` stay and no listing owns it.  The next run must
    give it to its first includer, ``a.c``, and not to ``b.c``, the unit
    that changed.
    """
    proj = owned_project
    conn = open_db(_db_path_for_project(proj))
    try:
        conn.execute("DELETE FROM header_owners WHERE path = 'src/shared.h'")
        conn.commit()
    finally:
        conn.close()
    _edit(proj / "src" / "b.c", _unit_source("b", ("shared.h",)) + "int b_more(void) { return 5; }\n")
    result = _index(proj)
    assert "2 updated, 1 unchanged" in _summary(result), "b.c, and a.c for shared.h"
    names = _names(_db_path_for_project(proj))
    assert "shared_a" in names
    assert "shared_other" not in names
    _assert_equals_rebuild(proj)


_GATED_H = """\
#pragma once
#if defined(UNIT_A)
static inline int gated_a(void) { return 7; }
#elif defined(UNIT_C)
static inline int gated_c(void) { return 8; }
#endif
"""


def test_a_lost_header_goes_past_an_includer_without_a_cursor(owned_project: Path, monkeypatch):
    """``b.c`` includes ``gated.h`` and has no cursor in it; ``c.c`` must take it.

    ``#pragma once`` and an inactive ``#if`` leave ``b.c`` without a cursor
    in the file, thus ``b.c`` does not claim it in a full run, and the next
    includer does.
    """
    proj = owned_project
    src = proj / "src"
    _write_file(src / "gated.h", _GATED_H)
    _write_file(src / "a.c", _unit_source("a", ("shared.h", "extra.h", "gated.h")))
    _write_file(src / "b.c", _unit_source("b", ("shared.h", "gated.h")))
    _write_file(src / "c.c", _unit_source("c", ("shared.h", "gated.h")))
    monkeypatch.setitem(_UNITS, "c", ("-DUNIT_C",))
    _write_cc(proj, ["c", "b", "a"])
    assert _index(proj).returncode == 0
    assert "gated_a" in _names(_db_path_for_project(proj))

    _edit(src / "a.c", _unit_source("a", ("shared.h", "extra.h")))
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    names = _names(_db_path_for_project(proj))
    assert "gated_c" in names
    assert "gated_a" not in names
    _assert_equals_rebuild(proj)


def test_a_new_order_of_compile_commands_parses_nothing(owned_project: Path):
    """The order of compile_commands.json is not the order of the run."""
    proj = owned_project
    db_path = _db_path_for_project(proj)
    before = _snapshot(db_path)
    _write_cc(proj, ["a", "c", "b"])
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    assert "0 updated, 3 unchanged" in _summary(result)
    assert _snapshot(db_path) == before
    # A full run with the new order gives the same owners.
    _assert_equals_rebuild(proj)


def test_an_index_run_after_a_change_of_a_source_that_another_unit_includes(owned_project: Path):
    """A unity build: ``a.c`` includes ``b.c``; ``b.c`` changes.

    On the base of this change, the incremental run gave ``b.c`` no symbol
    at all, and a rebuild gave each one.
    """
    proj = owned_project
    src = proj / "src"
    _edit(src / "a.c", _unit_source("a", ("shared.h", "extra.h", "b.c")))
    assert _index(proj).returncode == 0
    _edit(src / "b.c", _unit_source("b", ("shared.h",)) + "int b_more(void) { return 5; }\n")
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    assert "b_more" in _names(_db_path_for_project(proj))
    _assert_equals_rebuild(proj)


def test_a_listing_whose_parse_fails_keeps_its_rows_in_the_first_run_after_the_upgrade(owned_project: Path):
    """No claims recorded, and ``b.c`` cannot be read: its rows and its own header stay.

    The run gives each file with rows and no owner to its first includer.
    For ``b.c`` and ``only_b.h`` that is ``b.c``; a failed parse must not
    give them on, or the files lose their rows (no later listing includes
    them).
    """
    proj = owned_project
    src = proj / "src"
    _write_file(src / "only_b.h", "#pragma once\nint only_b_decl(void);\n")
    _edit(src / "b.c", _unit_source("b", ("shared.h", "only_b.h")))
    assert _index(proj).returncode == 0
    db_path = _db_path_for_project(proj)
    before = _names(db_path)
    assert {"b_entry", "only_b_decl"} <= before
    conn = open_db(db_path)
    try:
        conn.execute("DROP TABLE header_owners")
        conn.execute("DROP TABLE owner_units")
        conn.commit()
    finally:
        conn.close()
    (src / "b.c").chmod(0)
    try:
        result = _index(proj)
    finally:
        (src / "b.c").chmod(0o644)
    assert result.returncode == 0, result.stderr
    assert {"b_entry", "only_b_decl"} <= _names(db_path)


def test_an_index_without_recorded_claims_parses_each_unit_one_time(owned_project: Path):
    """An index from an older version has no claims: each unit is parsed, then none."""
    proj = owned_project
    db_path = _db_path_for_project(proj)
    conn = open_db(db_path)
    try:
        conn.execute("DROP TABLE header_owners")
        conn.execute("DROP TABLE owner_units")
        conn.commit()
    finally:
        conn.close()
    first = _index(proj)
    assert first.returncode == 0, first.stderr
    assert "3 updated, 0 unchanged" in _summary(first)
    second = _index(proj)
    assert second.returncode == 0, second.stderr
    assert "0 updated, 3 unchanged" in _summary(second)
