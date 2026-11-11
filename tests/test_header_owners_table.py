"""The tables of header owners (indexer/db/_owners.py) and the hand-over rule.

No libclang: the tests write the tables directly and give ``_hand_over``
plain decisions.  tests/test_header_ownership.py runs the whole index.
"""
from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer._unit_processor import UnitDecision
from fw_context_mcp.indexer.db import (
    delete_build_data,
    files_claimed_before,
    files_without_owner,
    forget_unit,
    insert_symbols_batch,
    load_ownership,
    number_listings,
    record_claims,
    release_files,
    skip_form,
    transaction,
    upsert_file,
)
from fw_context_mcp.indexer.runner import _hand_over
from fw_context_mcp.path_key import canonical_path

A = ("src/a.c", "fa")
B = ("src/b.c", "fb")


def test_an_index_without_the_tables_records_nothing(temp_db):
    ownership = load_ownership(temp_db, "h1")
    assert ownership.recorded == frozenset()
    assert ownership.files_of(A) == frozenset()


def test_the_claims_of_a_listing_replace_its_earlier_claims(temp_db):
    with transaction(temp_db):
        record_claims(temp_db, "h1", A, {"src/a.c", "src/shared.h", "src/old.h"}, "c")
        record_claims(temp_db, "h1", A, {"src/a.c", "src/shared.h"}, "c")
    ownership = load_ownership(temp_db, "h1")
    assert ownership.recorded == {A}
    assert ownership.files_of(A) == {"src/a.c", "src/shared.h"}


def test_a_listing_that_claims_nothing_is_recorded(temp_db):
    """The marker tells "claims nothing" from "claims unknown"."""
    with transaction(temp_db):
        record_claims(temp_db, "h1", B, set(), "c")
    ownership = load_ownership(temp_db, "h1")
    assert B in ownership.recorded
    assert ownership.files_of(B) == frozenset()


def test_a_file_moves_to_the_listing_that_claims_it(temp_db):
    with transaction(temp_db):
        record_claims(temp_db, "h1", B, {"src/b.c", "src/shared.h"}, "c")
        record_claims(temp_db, "h1", A, {"src/a.c", "src/shared.h"}, "c")
    ownership = load_ownership(temp_db, "h1")
    assert ownership.owner_of("src/shared.h") == A
    assert ownership.files_of(B) == {"src/b.c"}


def test_the_files_claimed_before_a_listing(temp_db):
    with transaction(temp_db):
        record_claims(temp_db, "h1", A, {"src/a.c", "src/shared.h"}, "c")
        record_claims(temp_db, "h1", B, {"src/b.c"}, "c")
    ownership = load_ownership(temp_db, "h1")
    assert files_claimed_before(ownership, B, "c") == {"src/a.c", "src/shared.h"}
    assert files_claimed_before(ownership, B, "c", exclude=[A]) == set()
    assert files_claimed_before(ownership, A, "c") == set()


def test_a_cpp_listing_claims_before_each_c_listing(temp_db):
    """`z.cpp` sorts after `a.c` by its path, but the C++ units come first (``claim_order``)."""
    cxx = ("src/z.cpp", "fz")
    with transaction(temp_db):
        record_claims(temp_db, "h1", A, {"src/a.c"}, "c")
        record_claims(temp_db, "h1", cxx, {"src/z.cpp", "src/shared.h"}, "cpp")
    ownership = load_ownership(temp_db, "h1")
    assert ownership.languages == {A: "c", cxx: "cpp"}
    assert files_claimed_before(ownership, A, "c") == {"src/z.cpp", "src/shared.h"}
    assert files_claimed_before(ownership, cxx, "cpp") == set()
    # A `.c` file that the build compiles as C++ (`-std=gnu++17`) is a C++
    # unit: after `z.cpp` by its path, before each C unit.
    late = ("src/zz.c", "fzz")
    assert files_claimed_before(ownership, late, "cpp") == {"src/z.cpp", "src/shared.h"}
    assert files_claimed_before(ownership, late, "c") == {"src/a.c", "src/z.cpp", "src/shared.h"}


def test_a_listing_with_no_language_is_not_recorded(temp_db):
    """An older table has no language: the run cannot place its claims, thus it parses the unit again."""
    temp_db.execute(
        "CREATE TABLE owner_units (config_hash TEXT NOT NULL, owner_path TEXT NOT NULL, "
        "owner_flags_hash TEXT NOT NULL, PRIMARY KEY (config_hash, owner_path, owner_flags_hash))"
    )
    temp_db.execute(
        "CREATE TABLE header_owners (config_hash TEXT NOT NULL, path TEXT NOT NULL, owner_path TEXT NOT NULL, "
        "owner_flags_hash TEXT NOT NULL, PRIMARY KEY (config_hash, path))"
    )
    temp_db.execute("INSERT INTO owner_units VALUES ('h1', 'src/a.c', 'fa')")
    temp_db.execute("INSERT INTO header_owners VALUES ('h1', 'src/a.c', 'src/a.c', 'fa')")
    assert load_ownership(temp_db, "h1").recorded == frozenset()
    with transaction(temp_db):
        record_claims(temp_db, "h1", B, {"src/b.c"}, "c")
    ownership = load_ownership(temp_db, "h1")
    assert ownership.recorded == {B}
    assert ownership.owner_of("src/a.c") is None


def test_forget_and_release(temp_db):
    with transaction(temp_db):
        record_claims(temp_db, "h1", A, {"src/a.c", "src/shared.h"}, "c")
        record_claims(temp_db, "h1", B, {"src/b.c"}, "c")
        forget_unit(temp_db, "h1", B)
        release_files(temp_db, "h1", {"src/shared.h"})
    ownership = load_ownership(temp_db, "h1")
    assert ownership.recorded == {A}
    assert ownership.files_of(A) == {"src/a.c"}


def test_the_claims_belong_to_one_build(temp_db):
    with transaction(temp_db):
        record_claims(temp_db, "h1", A, {"src/a.c"}, "c")
        record_claims(temp_db, "h2", A, {"src/a.c"}, "c")
        delete_build_data(temp_db, "h1")
    assert load_ownership(temp_db, "h1").recorded == frozenset()
    assert load_ownership(temp_db, "h2").recorded == {A}


def _symbol_row(file_id: int, path: str, usr: str) -> tuple:
    """One row in the column order of insert_symbols_batch."""
    name = usr.rsplit("@", 1)[-1].rsplit(":", 1)[-1]
    return (
        "hash-deadbeef", file_id, path, name, usr, name, name, "function",
        1, 1, 1, 1, "", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0,
    )


def test_files_without_owner(populated_db):
    """A file with symbol rows and no owner; asm rows and an owned file do not count."""
    conn = populated_db
    with transaction(conn):
        for path in ("src/owned.h", "src/lost.h", "src/start.S"):
            upsert_file(conn, "hash-deadbeef", path, "c", mtime=0.0)
        ids = {
            row[0]: row[1]
            for row in conn.execute("SELECT path, id FROM files WHERE config_hash = 'hash-deadbeef'")
        }
        insert_symbols_batch(conn, [
            _symbol_row(ids["src/owned.h"], "src/owned.h", "c:@F@owned"),
            _symbol_row(ids["src/lost.h"], "src/lost.h", "c:@F@lost"),
            _symbol_row(ids["src/start.S"], "src/start.S", "asm:start"),
        ])
        record_claims(conn, "hash-deadbeef", A, {"src/owned.h"}, "c")
    assert files_without_owner(conn, "hash-deadbeef") == {"src/lost.h"}


def test_numbered_keys_keep_the_order_of_the_listings():
    """Ten or more listings of one key: ``#10`` must sort after ``#2``."""
    keys = number_listings([("src/a.c", "f")] * 12)
    assert keys == sorted(keys)
    assert len(set(keys)) == 12


def test_skip_form_resolves_project_paths(tmp_path: Path):
    """A relative key joins the canonical root, and an absolute key is a path of the system.

    The absolute key comes from a real path, thus it has the form of this
    system: ``/abs/b.h`` on POSIX, ``C:/.../abs/b.h`` on Windows.
    """
    root = tmp_path / "proj"
    outside = canonical_path(tmp_path / "abs" / "b.h")
    assert skip_form({"src/a.h", outside.as_posix()}, root) == {
        str(canonical_path(root / "src" / "a.h")), str(outside),
    }


def _decision(path: str, unchanged: bool, includes: set[str]) -> UnitDecision:
    return UnitDecision(
        hashes=("s", "f"), mtime=0.0, unchanged=unchanged,
        key=(path, "f"), includes=frozenset(includes | {path}),
    )


def test_hand_over_sets_the_next_unchanged_includer_to_parsed(temp_db, tmp_path: Path):
    decisions = [
        _decision("src/a.c", False, set()),
        _decision("src/b.c", True, {"src/shared.h"}),
        _decision("src/c.c", True, {"src/shared.h"}),
    ]
    promoted, assigned = _hand_over(decisions, 1, {"src/shared.h"}, temp_db, "h1", tmp_path)
    assert promoted == {"src/b.c"}, "the run marker must name the promoted listing"
    assert assigned == {1: {"src/shared.h"}}
    assert not decisions[1].unchanged
    assert decisions[2].unchanged, "only the first includer"


def test_hand_over_does_not_promote_an_includer_that_the_run_parses(temp_db, tmp_path: Path):
    decisions = [
        _decision("src/a.c", False, set()),
        _decision("src/b.c", False, {"src/shared.h"}),
    ]
    promoted, assigned = _hand_over(decisions, 1, {"src/shared.h"}, temp_db, "h1", tmp_path)
    assert promoted == set()
    assert assigned == {1: {"src/shared.h"}}


def test_hand_over_promotes_each_listing_of_the_heir_file(temp_db, tmp_path: Path):
    """The index holds one set of rows for a file: each listing is parsed."""
    heir_one = _decision("src/b.c", True, {"src/shared.h"})
    heir_two = UnitDecision(
        hashes=("s", "g"), mtime=0.0, unchanged=True, key=("src/b.c", "g"),
        includes=frozenset({"src/shared.h", "src/b.c"}),
    )
    decisions = [_decision("src/a.c", False, set()), heir_one, heir_two]
    _hand_over(decisions, 1, {"src/shared.h"}, temp_db, "h1", tmp_path)
    assert not decisions[1].unchanged
    assert not decisions[2].unchanged


def test_hand_over_leaves_a_listing_that_the_loop_handled(temp_db, tmp_path: Path):
    """A listing of the heir file before *start* was handled already."""
    handled = _decision("src/b.c", True, {"src/shared.h"})
    heir = UnitDecision(
        hashes=("s", "g"), mtime=0.0, unchanged=True, key=("src/b.c", "g"),
        includes=frozenset({"src/shared.h", "src/b.c"}),
    )
    decisions = [handled, _decision("src/c.c", False, set()), heir]
    _hand_over(decisions, 2, {"src/shared.h"}, temp_db, "h1", tmp_path)
    assert decisions[0].unchanged
    assert not decisions[2].unchanged


def test_hand_over_releases_a_file_that_no_later_listing_includes(temp_db, tmp_path: Path):
    with transaction(temp_db):
        record_claims(temp_db, "h1", A, {"src/a.c", "src/gone.h"}, "c")
    decisions = [_decision("src/a.c", False, set()), _decision("src/b.c", True, set())]
    promoted, assigned = _hand_over(decisions, 1, {"src/gone.h"}, temp_db, "h1", tmp_path)
    assert promoted == set()
    assert assigned == {}
    assert load_ownership(temp_db, "h1").owner_of("src/gone.h") is None
