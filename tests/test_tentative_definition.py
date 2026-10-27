"""A C tentative definition is a symbol: `static cb_t g_cb;` at file scope.

C11 6.9.2 makes a file-scope object with no initializer and no ``extern``
a definition at the end of the unit, and libclang 22 reports it as no
definition.  The walk dropped each such variable, thus a function pointer
that firmware fills at run time was no symbol, and no tool could name it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang

_C = """\
typedef void (*cb_t)(int);
static cb_t g_cb;
cb_t g_public;
static cb_t g_init = 0;
extern cb_t g_extern;
static int g_twice;
static int g_twice;
cb_t g_later;
cb_t g_later = 0;
void f(void) { static cb_t local_static; (void)local_static; }
"""

_CPP = """\
typedef void (*cb_t)(int);
struct S { static cb_t member; };
cb_t S::member;
extern cb_t g_extern;
namespace ns { cb_t g_ns; }
"""


def _variables(tmp_path: Path, name: str, code: str) -> dict[str, tuple[str, bool, int]]:
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / name
    src.write_text(code, encoding="utf-8")
    cpp = name.endswith(".cpp")
    unit = CompilationUnit(
        file=src, directory=tmp_path, language="c++" if cpp else "c",
        clang_args=["-std=c++17"] if cpp else ["-std=gnu11"],
    )
    symbols = extract_all(unit).symbols
    return {
        s.qualified_name: (s.kind, s.is_definition, s.line, s.is_tentative)
        for s in symbols
        if s.kind in ("varglobal", "varlocal", "variable")
    }


def test_a_file_scope_variable_with_no_initializer_is_a_definition(tmp_path: Path):
    found = _variables(tmp_path, "t.c", _C)
    assert found["g_cb"] == ("varglobal", True, 2, True)
    assert found["g_public"] == ("varglobal", True, 3, True)
    assert found["g_init"] == ("varglobal", True, 4, False)
    assert found["g_twice"][:2] == ("varglobal", True)


def test_an_extern_declaration_is_no_definition(tmp_path: Path):
    assert "g_extern" not in _variables(tmp_path, "t.c", _C)


def test_a_definition_with_an_initializer_wins_over_the_tentative_one(tmp_path: Path):
    assert _variables(tmp_path, "t.c", _C)["g_later"] == ("varglobal", True, 9, False)


@pytest.mark.parametrize("tentative_first", [True, False])
def test_a_definition_with_an_initializer_in_another_unit_wins(tmp_path: Path, tentative_first: bool):
    """`hdr.h: int g_x;` read by a.c, and `b.c: int g_x = 5;`: the row is the definition of b.c.

    With ``-fcommon`` the linker merges the common symbol into the
    definition; the order of the units must not decide.
    """
    from fw_context_mcp.indexer.db import (
        insert_symbols_batch,
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )

    conn = open_db(tmp_path / "index.db")
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(tmp_path))
            upsert_build_config(conn, "ch", "pid", "cc.json")
            hdr = upsert_file(conn, "ch", "hdr.h", "c")
            unit_b = upsert_file(conn, "ch", "b.c", "c")

        def row(fid: int, path: str, line: int, tentative: int) -> tuple:
            return (
                "ch", fid, path, "g x", "c:@g_x", "g_x", "g_x", "varglobal", line, 1, line, 1,
                "int g_x", "", None, 0, 0, "", 0, "", 1, 0.0, "", 0, "g_x", tentative,
            )

        rows = [row(hdr, "hdr.h", 1, 1), row(unit_b, "b.c", 9, 0)]
        for one in rows if tentative_first else reversed(rows):
            with transaction(conn):
                insert_symbols_batch(conn, [one])
        stored = conn.execute("SELECT file_path, line, is_tentative FROM symbols WHERE usr = 'c:@g_x'").fetchone()
        assert tuple(stored) == ("b.c", 9, 0)
    finally:
        conn.close()


def test_cpp_keeps_its_rules(tmp_path: Path):
    """In C++ a namespace-scope declaration is a definition already, and a class member declaration is not."""
    found = _variables(tmp_path, "t.cpp", _CPP)
    assert found["ns::g_ns"][:2] == ("varglobal", True)
    assert found["S::member"] == ("varglobal", True, 3, False)
    assert "g_extern" not in found
