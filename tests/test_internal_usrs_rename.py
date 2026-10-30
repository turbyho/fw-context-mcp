"""The rename of the USRs of `static` functions on rows written by hand (``indexer/_internal_usrs.py``).

The libclang tests (``test_internal_usrs.py``) give the rows of real units.
These rows give the cases that real units give rarely: a site with a row
to the alias and a row to the symbol, a key ``R|``, the values of
``slot_arguments``, an alias at two places, and two overloads at one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CH = "hash-internal"
SYMBOL = "c:hdr.h@F@f"
CPP = "c:hdr.h@F@f#I#"
OTHER_FILE = "c:other.h@F@f"


@pytest.fixture
def conn(tmp_path: Path):
    from fw_context_mcp.indexer.db import open_db, transaction, upsert_build_config, upsert_file, upsert_project

    conn = open_db(tmp_path / "index.db")
    # Plain tuples: the assertions compare and sort the rows, and the code
    # under test reads each row by position.
    conn.row_factory = None
    with transaction(conn):
        upsert_project(conn, "proj-internal", "test", str(tmp_path))
        upsert_build_config(conn, CH, "proj-internal", str(tmp_path / "compile_commands.json"))
        fid = upsert_file(conn, CH, "hdr.h", "c")
        for usr, name, line in ((SYMBOL, "f", 3), ("c:util.h@F@u", "u", 7), ("c:o.h@F@ov", "ov", 9)):
            conn.execute(
                "INSERT INTO symbols (config_hash, file_id, file_path, usr, name, qualified_name, kind, line, col,"
                " is_definition) VALUES (?,?,?,?,?,?,'function',?,5,1)",
                (CH, fid, "hdr.h", usr, name, name, line),
            )
    yield conn
    conn.close()


def _decl(conn, usr: str, name: str, line: int, unit: str, decl_file: str = "hdr.h") -> None:
    conn.execute(
        "INSERT INTO internal_decls (config_hash, usr, name, decl_file, line, col, from_file) VALUES (?,?,?,?,?,5,?)",
        (CH, usr, name, decl_file, line, unit),
    )


def _unify(conn) -> dict[str, int]:
    from fw_context_mcp.indexer._internal_usrs import unify_internal_functions
    from fw_context_mcp.indexer.db import transaction

    with transaction(conn):
        return unify_internal_functions(conn, CH)


def test_a_site_with_a_row_to_the_alias_and_to_the_symbol_keeps_one_row(conn):
    from fw_context_mcp.indexer.db import insert_refs_batch

    _decl(conn, CPP, "f", 3, "b.cpp")
    insert_refs_batch(conn, [
        (CH, CPP, "b.cpp", 10, "c:@F@use", "call", None, 3, 1),
        (CH, SYMBOL, "b.cpp", 10, "c:@F@use", "call", None, 3, 4),
    ])
    [alias_id] = [row[0] for row in conn.execute("SELECT id FROM refs WHERE to_usr = ?", (CPP,))]
    conn.execute("INSERT INTO ref_producers VALUES (?, ?, 'b.cpp', 'flags')", (alias_id, CH))

    assert _unify(conn)["refs"] == 1
    rows = conn.execute("SELECT id, to_usr, origins FROM refs").fetchall()
    assert [(row[1], row[2]) for row in rows] == [(SYMBOL, 5)], "the origins of the two rows join"
    assert conn.execute("SELECT ref_id, unit_path FROM ref_producers").fetchall() == [(rows[0][0], "b.cpp")]


def test_each_slot_value_of_the_alias_gets_the_symbol(conn):
    _decl(conn, CPP, "f", 3, "b.cpp")
    conn.executemany(
        "INSERT INTO slot_flows (config_hash, file, line, col, source, target) VALUES (?, 'b.cpp', 1, 1, ?, ?)",
        [(CH, f"P|{CPP}|0", "V|c:@g"), (CH, f"R|{CPP}", f"P|{CPP}|12"), (CH, f"P|{CPP}x|0", "V|c:@h")],
    )
    conn.executemany(
        "INSERT INTO slot_arguments (config_hash, file, line, col, ordinal, arg_index, value_kind, value)"
        " VALUES (?, 'b.cpp', 2, 1, 0, ?, ?, ?)",
        [(CH, 0, "function", CPP), (CH, 1, "slot", f"P|{CPP}|1"), (CH, 2, "slot", f"R|{CPP}")],
    )
    conn.execute(
        "INSERT INTO slot_sites (config_hash, file, line, col, ordinal, caller, kind, slot, slot_type, callee)"
        " VALUES (?, 'b.cpp', 3, 1, 0, 'c:@F@use', 'pointer', ?, 'void (*)(void)', ?)",
        (CH, f"P|{CPP}|0", CPP),
    )

    _unify(conn)

    assert sorted(conn.execute("SELECT source, target FROM slot_flows").fetchall()) == sorted([
        (f"P|{SYMBOL}|0", "V|c:@g"), (f"R|{SYMBOL}", f"P|{SYMBOL}|12"), (f"P|{CPP}x|0", "V|c:@h"),
    ]), "a key of an other function with the alias as its start stays"
    assert conn.execute("SELECT value FROM slot_arguments ORDER BY arg_index").fetchall() == [
        (SYMBOL,), (f"P|{SYMBOL}|1",), (f"R|{SYMBOL}",),
    ]
    assert conn.execute("SELECT slot, callee FROM slot_sites").fetchall() == [(f"P|{SYMBOL}|0", SYMBOL)]


def test_a_usr_of_c_and_a_usr_of_cpp_of_other_files_are_one_function(conn):
    """A prototype in an other header gives one more USR: the file of the first declaration."""
    _decl(conn, CPP, "f", 3, "c.cpp")
    _decl(conn, OTHER_FILE, "f", 3, "b.c")
    from fw_context_mcp.indexer._internal_usrs import _aliases

    assert _aliases(conn, CH) == {CPP: SYMBOL, OTHER_FILE: SYMBOL}


def test_an_alias_recorded_at_two_places_gets_no_symbol(conn):
    """`dirA/util.h` and `dirB/util.h`: the USR holds the base name of the file only."""
    from fw_context_mcp.indexer._internal_usrs import _aliases

    _decl(conn, "c:util.h@F@u#v#", "u", 7, "a.cpp")
    _decl(conn, "c:util.h@F@u#v#", "u", 7, "b.cpp", decl_file="dirB/hdr.h")
    assert _aliases(conn, CH) == {}


def test_two_signatures_at_the_place_of_one_function_of_c_get_no_symbol(conn):
    """`ov(int)` and `ov(double)` of C++ where C has `ov(int)`: the place cannot tell which one."""
    from fw_context_mcp.indexer._internal_usrs import _aliases

    _decl(conn, "c:o.h@F@ov#I#", "ov", 9, "a.cpp")
    _decl(conn, "c:o.h@F@ov#d#", "ov", 9, "a.cpp")
    assert _aliases(conn, CH) == {}


def test_the_unit_of_an_alias_records_the_usr_of_the_symbol_once(conn):
    """The rows of the unit hold the USR of the symbol now: an other owner of the header can change it."""
    _decl(conn, CPP, "f", 3, "b.cpp")
    _unify(conn)
    _unify(conn)

    assert sorted(conn.execute("SELECT usr, from_file FROM internal_decls").fetchall()) == [
        (SYMBOL, "b.cpp"), (CPP, "b.cpp"),
    ]
