"""A `static` function of a header has one USR in the index, also when C and C++ units call it.

clang gives such a function one USR in a C unit (`c:hdr.h@F@twice`) and an
other in a C++ unit (`c:hdr.h@F@twice#I#`), and the index holds the symbol
of the unit that walks the header first.  Each unit records the definition
of each such function that it names (``internal_decls``), and the step
``internal_usrs`` gives each reference and each slot fact the USR of the
symbol with the same name at the same place (``indexer/_internal_usrs.py``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests._toml import toml_string

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

HEADER = """\
#pragma once
#ifdef __cplusplus
extern "C" {
#endif
typedef void (*cb_t)(void);
extern cb_t g_cb;
static inline int twice(int x) { return x * 2; }
static inline void post(cb_t f) { g_cb = f; }
#ifdef __cplusplus
}
#endif
static inline int bare(int x) { return x + 1; }
#define TWO(n) static inline int n##_a(void) { return 1; } static inline int n##_b(void) { return 2; }
TWO(pair)
#ifdef __cplusplus
#define OV static inline int ov(int x) { return x; } static inline int ov(double x) { return (int) x; }
#else
#define OV static inline int ov(int x) { return x; }
#endif
OV
"""
C_UNIT = """\
#include "hdr.h"
cb_t g_cb;
static void job_c(void) {}
int use_c(void) { post(job_c); return twice(1) + bare(2) + pair_a() + pair_b() + ov(1); }
void tick(void) { g_cb(); }
"""
TEMPLATE = """\
#pragma once
#include "hdr.h"
template <class T> int g(T x) { return twice(x); }
"""
CPP_UNIT = """\
#include "hdr.h"
#include "tpl.hpp"
static void job_cpp() {}
int use_cpp() { post(job_cpp); return twice(3) + bare(4) + pair_a() + pair_b() + ov(1) + ov(2.0) + g(5); }
"""
# The two orders of the paths of the units.  The C++ unit claims the header
# in each of them (``db.claim_order``), thus the symbols have the USRs of
# C++, and the calls of the C unit are renamed.
ORDERS = {
    "c-first": {"a.c": C_UNIT, "b.cpp": CPP_UNIT},
    "cpp-first": {"a.cpp": CPP_UNIT, "b.c": C_UNIT},
}


def _write(root: Path, units: dict[str, str], headers: dict[str, str] | None = None) -> None:
    (root / ".fw-context").mkdir(parents=True, exist_ok=True)
    db_dir = root.parent / "db"
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-internal"\n\n[build]\n\n[index]\ndb_dir = {toml_string(db_dir)}\n', encoding="utf-8",
    )
    for file, text in {"hdr.h": HEADER, "tpl.hpp": TEMPLATE, **(headers or {}), **units}.items():
        (root / file).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": file,
         "arguments": ["c++", "-std=c++17", "-c", file] if file.endswith(".cpp") else ["cc", "-std=gnu11", "-c", file]}
        for file in units
    ]), encoding="utf-8")


def _run(root: Path) -> sqlite3.Connection:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    db_path = root.parent / "db" / "proj-internal" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id="proj-internal", project_name="proj-internal", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module", params=sorted(ORDERS))
def index(request, tmp_path_factory) -> tuple[str, sqlite3.Connection]:
    root = tmp_path_factory.mktemp(request.param) / "proj"
    root.mkdir()
    _write(root, ORDERS[request.param])
    conn = _run(root)
    yield request.param, conn
    conn.close()


def _callers(conn: sqlite3.Connection, name: str) -> list[str]:
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE name = ? AND kind = 'function'", (name,))]
    return sorted(
        row[0] for row in conn.execute(
            "SELECT c.name FROM refs r JOIN symbols c ON c.usr = r.from_usr"
            " WHERE r.to_usr = ? AND r.ref_kind = 'call'", (usr,),
        )
    )


def test_the_cpp_unit_gives_the_symbol_in_each_order_of_the_paths(index):
    """The C++ units claim first: the order of the paths does not decide the language of the symbol."""
    _order, conn = index
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE name = 'twice'")]
    assert "#" in usr


@pytest.mark.parametrize("name", ["bare", "post"])
def test_a_call_from_each_language_reaches_the_one_symbol(index, name):
    """`extern "C"` or not, a C call and a C++ call of a header function reach its symbol."""
    _order, conn = index
    assert _callers(conn, name) == ["use_c", "use_cpp"]


def test_a_call_in_the_body_of_a_template_instance_reaches_the_symbol(index):
    """`g<T>` calls `twice(x)` with a dependent argument: only the instance names the function."""
    _order, conn = index
    assert _callers(conn, "twice") == ["g", "use_c", "use_cpp"]


def test_two_functions_of_one_macro_keep_their_own_callers(index):
    """`TWO(pair)` defines two functions at one place: the name tells them apart."""
    _order, conn = index
    assert _callers(conn, "pair_a") == ["use_c", "use_cpp"]
    assert _callers(conn, "pair_b") == ["use_c", "use_cpp"]


def test_no_reference_names_a_usr_of_a_static_function_that_is_no_symbol(index):
    """Except `ov`: its place does not tell the overloads apart (the next test)."""
    _order, conn = index
    dangling = conn.execute(
        "SELECT DISTINCT r.to_usr FROM refs r WHERE r.to_usr LIKE 'c:hdr.h@F@%' AND r.to_usr NOT LIKE 'c:hdr.h@F@ov%'"
        " AND NOT EXISTS (SELECT 1 FROM symbols s WHERE s.usr = r.to_usr)"
    ).fetchall()
    assert [row[0] for row in dangling] == []


def test_two_overloads_at_the_place_of_one_function_of_c_get_no_match(index):
    """`OV` defines `ov(int)` and `ov(double)` in C++, and `ov(int)` in C, at one place.

    The place cannot tell which C++ overload is the function of C, thus no
    call of an other language moves: a call of `ov(double)` must never
    reach `ov(int)`.
    """
    _order, conn = index
    symbols = {row[0]: row[1] for row in conn.execute("SELECT usr, signature FROM symbols WHERE name = 'ov'")}
    callers = {
        usr: sorted(row[0] for row in conn.execute(
            "SELECT c.name FROM refs r JOIN symbols c ON c.usr = r.from_usr WHERE r.to_usr = ? AND r.ref_kind = 'call'",
            (usr,),
        ))
        for usr in symbols
    }
    assert sorted(callers.values()) == [["use_cpp"], ["use_cpp"]]


def test_a_function_that_a_unit_of_the_other_language_stores_reaches_the_call(index):
    """`post(f)` stores `f` in `g_cb`: the walk of the body of `post` is in one language only."""
    _order, conn = index
    assert sorted(
        row[0] for row in conn.execute(
            "SELECT t.name FROM refs r JOIN symbols t ON t.usr = r.to_usr JOIN symbols c ON c.usr = r.from_usr"
            " WHERE c.name = 'tick' AND r.ref_kind = 'call' AND t.kind = 'function'"
        )
    ) == ["job_c", "job_cpp"]


# ── the other runs of the index ──────────────────────────────────────────────

PROTO = "static inline int pf(int x);\n"
PROTO_HEADER = "static inline int pf(int x) { return x - 1; }\n"


@pytest.mark.parametrize("cxx", ["c.cpp", "0.cpp"])
def test_a_prototype_in_an_other_header_gives_one_more_usr_of_the_same_function(tmp_path, cxx):
    """The USR names the file of the first declaration: `c:proto.h@F@pf` in a.c, `c:hdr2.h@F@pf` in b.c.

    The C++ unit owns `hdr2.h` and `a.c` owns `proto.h`, thus `a.c` stores a
    declaration row with its USR.  The call of `a.c` must reach the
    definition, and the function has one row, for each order of the paths.
    """
    root = tmp_path / "proj"
    root.mkdir()
    units = {
        "a.c": '#include "proto.h"\n#include "hdr2.h"\nint use_a(void) { return pf(1); }\n',
        "b.c": '#include "hdr2.h"\nint use_b(void) { return pf(2); }\n',
        cxx: '#include "hdr2.h"\nint use_c() { return pf(3); }\n',
    }
    _write(root, units, {"proto.h": PROTO, "hdr2.h": PROTO_HEADER})
    conn = _run(root)
    assert _callers(conn, "pf") == ["use_a", "use_b", "use_c"]
    assert [tuple(row) for row in conn.execute("SELECT file_path, is_definition FROM symbols WHERE name = 'pf'")] == [
        ("hdr2.h", 1),
    ]
    conn.close()


def test_an_unchanged_unit_keeps_its_call_when_an_other_unit_owns_the_header(tmp_path):
    """c.cpp owns hdr.h first, thus the symbol is of C++; then only C units include it.

    The call of the unchanged b.c holds the USR of C++ after the first run
    (the rename); the second run gives the symbol the USR of C, and the
    call must follow it, as a full run would give.
    """
    root = tmp_path / "proj"
    root.mkdir()
    units = {
        "a.c": '#include "hdr.h"\ncb_t g_cb;\nint use_a(void) { return twice(1); }\n',
        "b.c": '#include "hdr.h"\nint use_b(void) { return twice(2); }\n',
        "c.cpp": '#include "hdr.h"\nint use_c() { return twice(3); }\n',
    }
    _write(root, units)
    conn = _run(root)
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE name = 'twice'")]
    assert "#" in usr, "the symbol is of C++"
    assert _callers(conn, "twice") == ["use_a", "use_b", "use_c"]
    conn.close()
    (root / "c.cpp").write_text("int use_c() { return 3; }\n", encoding="utf-8")
    conn = _run(root)
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE name = 'twice'")]
    assert "#" not in usr, "the symbol is of C now"
    assert _callers(conn, "twice") == ["use_a", "use_b"]
    conn.close()


def test_the_resolution_step_of_reindex_file_renames_an_alias(tmp_path):
    """``reindex_file`` parses one unit again, and that parse writes the USR of its language.

    The test puts the alias back into the rows by hand and calls the step
    of ``reindex_file`` that resolves the slots: it tests that the step
    renames, not a parse of ``reindex_file``.
    """
    from fw_context_mcp.mcp.handlers.maintenance import _resolve_slot_edges_locked

    root = tmp_path / "proj"
    root.mkdir()
    _write(root, ORDERS["c-first"])
    conn = _run(root)
    [config_hash] = [row[0] for row in conn.execute("SELECT DISTINCT config_hash FROM symbols")]
    # The C++ unit gives the symbol; the C unit recorded the USR of C.
    [usr] = [row[0] for row in conn.execute("SELECT usr FROM symbols WHERE name = 'twice'")]
    [alias] = [row[0] for row in conn.execute(
        "SELECT usr FROM internal_decls WHERE name = 'twice' AND usr NOT LIKE '%#%'")]
    with conn:
        conn.execute("UPDATE refs SET to_usr = ? WHERE to_usr = ?", (alias, usr))
    result: dict = {}
    _resolve_slot_edges_locked(conn, config_hash, root.parent / "db" / "proj-internal", root, result)
    assert result == {}
    assert _callers(conn, "twice") == ["g", "use_c", "use_cpp"]
    conn.close()


def test_a_function_that_only_a_template_instance_names_gets_its_caller(tmp_path):
    """No written code of a C++ unit names `twice`: only the instance of `g<int>` records its USR."""
    root = tmp_path / "proj"
    root.mkdir()
    units = {
        "a.c": '#include "hdr.h"\ncb_t g_cb;\nint use_a(void) { return twice(1); }\n',
        "d.cpp": '#include "tpl.hpp"\nint use_d() { return g(5); }\n',
    }
    _write(root, units)
    conn = _run(root)
    assert _callers(conn, "twice") == ["g", "use_a"]
    conn.close()
