"""Two definitions of one C++ type in two files are one type (the ODR), and two of a C type are two (``indexer/_odr.py``).

``cursor_usr`` writes the place of the definition of each named type into
its USR: two drivers that each define `struct config` in C are two types.  A
project that has a copy of a header of its SDK gave two C++ classes: the
units that include the copy of the SDK called a method with no definition,
and the unit of the copy in the project defined it.  The run merges the
places of a C++ type that C++ units read, and parses again each unit whose
rows change.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from fw_context_mcp.file_lock import try_lock

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

PARSER = (
    "namespace drv {\n"
    "class Parser {\n"
    "public:\n"
    "    struct oob { int len; void (*cb)(); };\n"
    "    int send(int v);\n"
    "    void add(void (*cb)());\n"
    "    void poll();\n"
    "    oob slot;\n"
    "};\n"
    "}\n"
)
USER = '#include "{header}"\nstatic void handler() {{}}\nint use(drv::Parser &p) {{ p.add(handler); return p.send(1); }}\n'
IMPL = (
    '#include "{header}"\n'
    "int drv::Parser::send(int v) {{ return v; }}\n"
    "void drv::Parser::add(void (*cb)()) {{ slot.cb = cb; }}\n"
    "void drv::Parser::poll() {{ slot.cb(); }}\n"
)


def _write(root: Path, files: dict[str, str], units: list[str]) -> None:
    for file, text in files.items():
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": file,
         "arguments": (["cc", "-std=gnu11"] if file.endswith(".c") else ["c++", "-std=c++17"]) + ["-c", file]}
        for file in units
    ]), encoding="utf-8")


def _run(root: Path, db: str = "db") -> Path:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    db_path = root.parent / db / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id="proj-odr", project_name="proj-odr", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )
    return db_path


def _rows(db_path: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _symbols(db_path: Path, name: str) -> list[tuple]:
    return _rows(
        db_path, "SELECT usr, file_path, is_definition FROM symbols WHERE qualified_name = ? ORDER BY usr", name,
    )


def _calls(db_path: Path) -> set[tuple]:
    """``(caller, callee, is_definition of the callee)`` of each call and slot edge."""
    return set(_rows(
        db_path,
        "SELECT a.qualified_name, b.qualified_name, b.is_definition FROM refs r"
        " JOIN symbols a ON a.usr = r.from_usr JOIN symbols b ON b.usr = r.to_usr"
        " WHERE r.ref_kind IN ('call', 'indirect')",
    ))


def _state(db_path: Path) -> tuple[set, set]:
    """The symbols and the references of the index, for a comparison with a full run."""
    symbols = set(_rows(db_path, "SELECT usr, qualified_name, kind, file_path, line, is_definition FROM symbols"))
    refs = set(_rows(db_path, "SELECT from_usr, to_usr, ref_kind, from_file, from_line FROM refs"))
    return symbols, refs


def _odr(db_path: Path) -> tuple[set, set]:
    """The merged marks and the pending units; an index with no place has no table, and reads as none."""
    from fw_context_mcp.indexer.db import load_merged_types, load_pending_units

    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    conn = sqlite3.connect(db_path)
    try:
        return set(load_merged_types(conn, config_hash)), set(load_pending_units(conn, config_hash))
    finally:
        conn.close()


def _stale_units(db_path: Path, root: Path) -> set[str]:
    """The file of each entry of the manifest that the staleness check of the status gives as stale."""
    from fw_context_mcp.indexer.manifest import check_tu_staleness, tu_headers
    from fw_context_mcp.indexer.manifest import load as load_manifest

    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    manifest = load_manifest(db_path.parent, config_hash)
    return {
        entry["file"] for entry in manifest["entries"]
        if check_tu_staleness(entry, root, headers=tu_headers(manifest, entry))[0]
    }


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _copies(root: Path, impl_header: str = "proj/p.h") -> None:
    _write(root, {
        "sdk/p.h": PARSER, "proj/p.h": PARSER,
        "user.cpp": USER.format(header="sdk/p.h"), "impl.cpp": IMPL.format(header=impl_header),
    }, ["impl.cpp", "user.cpp"])


def test_a_copy_of_a_header_of_a_cpp_class_is_one_class(root):
    """The caller of the copy of the SDK reaches the method that the unit of the copy in the project defines."""
    _copies(root)
    db_path = _run(root)
    classes = _symbols(db_path, "drv::Parser")
    assert len(classes) == 1 and classes[0][0] == "c:@N@drv@S@Parser", classes
    assert ("use", "drv::Parser::send", 1) in _calls(db_path)
    assert len(_symbols(db_path, "drv::Parser::oob::cb")) == 1
    marks, pending = _odr(db_path)
    # `Parser::oob` is defined in the body of `Parser`: it follows the merge of `Parser`, with no place of its own.
    assert len(marks) == 2 and not pending, "two places of `Parser`"


def test_the_merged_index_has_the_calls_and_the_slots_of_one_header(root, tmp_path):
    """The copy gives the edges that one header gives, the slot of `oob::cb` too."""
    _copies(root)
    merged = _calls(_run(root))
    one = tmp_path / "one"
    one.mkdir()
    _write(one, {
        "sdk/p.h": PARSER, "user.cpp": USER.format(header="sdk/p.h"), "impl.cpp": IMPL.format(header="sdk/p.h"),
    }, ["impl.cpp", "user.cpp"])
    assert merged == _calls(_run(one, "db_one"))
    assert ("drv::Parser::poll", "handler", 1) in merged


def test_two_c_structs_of_one_name_in_two_files_stay_two(root):
    """C: a tag has no linkage, thus the copies of a header that only C units read are two types."""
    _write(root, {
        "sdk/q.h": "struct cfg { int x; };\n", "proj/q.h": "struct cfg { int x; };\n",
        "a.c": '#include "sdk/q.h"\nint a(struct cfg *c) { return c->x; }\n',
        "b.c": '#include "proj/q.h"\nint b(struct cfg *c) { return c->x; }\n',
    }, ["a.c", "b.c"])
    db_path = _run(root)
    assert len(_symbols(db_path, "cfg")) == 2
    assert _odr(db_path) == (set(), set())


def test_a_c_header_that_cpp_units_read_follows_the_odr(root):
    """The copies of a C header that C++ units read: C++ makes the two definitions one type."""
    _write(root, {
        "sdk/q.h": "struct cfg { int x; };\n", "proj/q.h": "struct cfg { int x; };\n",
        "a.cpp": '#include "sdk/q.h"\nint a(cfg *c) { return c->x; }\n',
        "b.cpp": '#include "proj/q.h"\nint b(cfg *c) { return c->x; }\n',
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    assert [row[0] for row in _symbols(db_path, "cfg")] == ["c:@S@cfg"]


def test_one_header_of_c_and_cpp_units_is_one_type(root):
    """One place: one USR in the two languages, and no merge."""
    _write(root, {
        "q.h": "struct cfg { int x; };\n",
        "a.c": '#include "q.h"\nint a(struct cfg *c) { return c->x; }\n',
        "b.cpp": '#include "q.h"\nint b(cfg *c) { return c->x; }\n',
    }, ["a.c", "b.cpp"])
    db_path = _run(root)
    symbols = _symbols(db_path, "cfg")
    assert len(symbols) == 1 and "~q.h~" in symbols[0][0], symbols
    assert _odr(db_path) == (set(), set())


@pytest.mark.parametrize("c_unit", ["a.c", "z.c"])
def test_a_struct_in_a_struct_follows_the_merge_of_its_struct_for_each_claim_order(root, c_unit):
    """`A::B` of two copies is one type, also when a C unit claims a copy.

    libclang gives a struct in a struct the key `c:@S@B` in C and
    `c:@S@A@S@B` in C++, thus a key of the unit that claims the copy split
    the merge by the claim order (`a.c` sorts before the C++ units, `z.c`
    after them).  A type in the body of a type follows the merge of that
    type, and the key of `A` is one text in C and in C++.
    """
    header = "#pragma once\nstruct A { struct B { int x; } b; int y; };\n"
    _write(root, {
        "sdk/p.h": header, "proj/p.h": header,
        c_unit: '#include "sdk/p.h"\nint ca(struct A *a) { return a->b.x; }\n',
        "u.cpp": '#include "sdk/p.h"\nint cu(A &a) { return a.b.x + a.y; }\n',
        "v.cpp": '#include "proj/p.h"\nint cv(A &a) { return a.b.x + a.y; }\n',
    }, [c_unit, "u.cpp", "v.cpp"])
    db_path = _run(root)
    targets = _rows(
        db_path, "SELECT DISTINCT from_file, to_usr FROM refs WHERE to_usr LIKE '%@FI@x' AND from_file LIKE '%.cpp'",
    )
    assert sorted(targets) == [("u.cpp", "c:@S@A@S@B@FI@x"), ("v.cpp", "c:@S@A@S@B@FI@x")], targets
    assert {key for key, _ in _odr(db_path)[0]} == {"c:@S@A"}, "only a type outside a type has places to merge"


def test_partial_specializations_with_one_usr_stay_two(root):
    """libclang gives two partial specializations that differ in the qualifier of a function type one USR."""
    header = (
        "template <class> struct is_fn;\n"
        "template <class R, class... A> struct is_fn<R(A...)> { static const int v = 1; };\n"
        "template <class R, class... A> struct is_fn<R(A...) const> { static const int v = 2; };\n"
    )
    _write(root, {
        "t.h": header,
        "a.cpp": '#include "t.h"\nint a() { return is_fn<int()>::v; }\n',
        "b.cpp": '#include "t.h"\nint b() { return is_fn<int() const>::v; }\n',
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    # The primary template has no definition, thus no symbol: the two rows
    # are the two partial specializations.
    usrs = {row[0] for row in _rows(db_path, "SELECT usr FROM symbols WHERE name = 'is_fn' AND is_template = 1")}
    assert len(usrs) == 2, usrs
    assert _odr(db_path) == (set(), set())


def test_a_copy_that_comes_and_goes_gives_the_index_of_a_full_run(root):
    """An incremental run merges and divides again, also the rows of a unit that it did not change."""
    _copies(root, impl_header="sdk/p.h")
    _run(root)
    assert _odr(root.parent / "db" / "index.db") == (set(), set())

    (root / "impl.cpp").write_text(IMPL.format(header="proj/p.h"), encoding="utf-8")
    incremental = _run(root)
    assert len(_odr(incremental)[0]) == 2
    assert _state(incremental) == _state(_run(root, "db_full_1"))

    (root / "impl.cpp").write_text(IMPL.format(header="sdk/p.h"), encoding="utf-8")
    incremental = _run(root)
    assert _odr(incremental) == (set(), set())
    assert _state(incremental) == _state(_run(root, "db_full_2"))


def test_the_copy_with_the_row_of_a_merged_class_leaves_and_the_others_store_it_again(root):
    """Three copies merge `Parser`, and the row of the class is the one of `x/p.h`: its unit leaves the build.

    The other two copies stay merged, thus their merge does not change, but
    their units lost the conflict of the row and must store the class.
    """
    _write(root, {
        "sdk/p.h": PARSER, "proj/p.h": PARSER, "x/p.h": PARSER,
        "a_x.cpp": '#include "x/p.h"\nint ax(drv::Parser &p) { return p.send(2); }\n',
        "impl.cpp": IMPL.format(header="proj/p.h"), "user.cpp": USER.format(header="sdk/p.h"),
    }, ["a_x.cpp", "impl.cpp", "user.cpp"])
    db_path = _run(root)
    assert len(_odr(db_path)[0]) == 3
    assert _symbols(db_path, "drv::Parser") == [("c:@N@drv@S@Parser", "x/p.h", 1)]

    _write(root, {}, ["impl.cpp", "user.cpp"])
    incremental = _run(root)
    assert [file for _, file, _ in _symbols(incremental, "drv::Parser")] in (["proj/p.h"], ["sdk/p.h"])
    assert _state(incremental) == _state(_run(root, "db_full"))


@pytest.mark.parametrize("units", [["a.c", "ua.cpp", "ub.cpp"], ["ua.cpp", "ub.cpp", "a.c"]], ids=["c-first", "c-last"])
def test_a_cpp_class_in_a_header_that_a_c_unit_reads_too_merges(root, units):
    """The class of a copy is in `#ifdef __cplusplus`, and the C unit `a.c` sorts first by its path.

    The C++ units claim first (``db.claim_order``): `sdk/k.h` has the rows of
    C++, thus `drv::K` of the two copies merges, and the call of `ua.cpp`
    reaches the method that `ub.cpp` defines.
    """
    header = (
        "#pragma once\nstruct cfg { int x; };\n"
        "#ifdef __cplusplus\nnamespace drv { class K { public: int f(); }; }\n#endif\n"
    )
    _write(root, {
        "sdk/k.h": header, "proj/k.h": header,
        "a.c": '#include "sdk/k.h"\nint a(struct cfg *c) { return c->x; }\n',
        "ua.cpp": '#include "sdk/k.h"\nint ua(drv::K &k) { return k.f(); }\n',
        "ub.cpp": '#include "proj/k.h"\nint drv::K::f() { return 1; }\n',
    }, units)
    db_path = _run(root)
    assert {key for key, _ in _odr(db_path)[0]} == {"c:@N@drv@S@K", "c:@S@cfg"}
    assert _rows(db_path, "SELECT owner_path FROM header_owners WHERE path = 'sdk/k.h'") == [("ua.cpp",)]
    assert ("ua", "drv::K::f", 1) in _calls(db_path)
    # The C unit names the merged struct too.
    assert _rows(db_path, "SELECT DISTINCT to_usr FROM refs WHERE from_file = 'a.c' AND to_usr LIKE '%@FI@x'") == [
        ("c:@S@cfg@FI@x",),
    ]


@pytest.mark.parametrize("merged", [False, True], ids=["one-file", "merged"])
def test_a_type_in_a_struct_has_one_usr_in_c_and_in_cpp(root, merged):
    """A C unit and the C++ owner of the header give one USR to each type in a struct, its fields and its constants.

    In C a type in the body of a struct has the scope of the file, and
    libclang writes no outer struct in its USR; C++ writes it.  The C++
    unit owns the header (``db.claim_order``), thus the fields that `a.c`
    reads had no symbol.  With a copy of the header in an other C++ unit the
    outer struct merges: the struct in the unnamed type of a field had two
    USRs there too.
    """
    header = (
        "#pragma once\nstruct outer {\n  struct inner { int v; } in;\n"
        "  struct { struct deep { int d; } dd; } s;\n  enum mode { M1, M2 } m;\n};\n"
    )
    files = {
        "h.h": header,
        "a.c": '#include "h.h"\nint a(struct outer *o, struct inner *i) { return o->in.v + i->v + o->s.dd.d + M2; }\n',
        "z.cpp": '#include "h.h"\nint z(outer &o) { return o.in.v + o.s.dd.d + outer::M1; }\n',
    }
    units = ["a.c", "z.cpp"]
    if merged:
        files |= {"copy/h.h": header, "y.cpp": '#include "copy/h.h"\nint y(outer &o) { return o.in.v; }\n'}
        units.append("y.cpp")
    _write(root, files, units)
    db_path = _run(root)
    assert _rows(db_path, "SELECT owner_path FROM header_owners WHERE path = 'h.h'") == [("z.cpp",)]
    assert {key for key, _ in _odr(db_path)[0]} == ({"c:@S@outer"} if merged else set())
    assert _rows(
        db_path,
        "SELECT to_usr FROM refs r WHERE from_file = 'a.c' AND to_usr LIKE 'c:@S@%'"
        " AND NOT EXISTS (SELECT 1 FROM symbols s WHERE s.usr = r.to_usr)",
    ) == []
    for name, readers in (("outer::inner::v", {"a.c", "z.cpp"}), ("outer::s::deep::d", {"a.c", "z.cpp"}),
                          ("outer::mode::M2", {"a.c"})):
        # The unnamed type of a field of a merged struct keeps the place of
        # each copy (the limits of ``_usr``), thus a name can have a symbol
        # in each copy: one of them has the references of the two languages.
        usrs = [usr for (usr,) in _rows(db_path, "SELECT usr FROM symbols WHERE qualified_name = ?", name)]
        assert any(
            {file for (file,) in _rows(db_path, "SELECT from_file FROM refs WHERE to_usr = ?", usr)} >= readers
            for usr in usrs
        ), name


def test_a_definition_that_moves_in_its_file_gives_the_merge_of_a_full_run(root):
    """The merge names a file, not a line: a moved class changes no decision, and no unit is parsed two times."""
    from fw_context_mcp.indexer import runner

    _copies(root)
    _run(root)
    before = _odr(root.parent / "db" / "index.db")
    (root / "proj" / "p.h").write_text("// The copy of the project.\n" + PARSER, encoding="utf-8")
    parsed: list[str] = []
    parse = runner._parse_unit

    def count(unit, index_refs, skip_files, hashes, decisions):
        parsed.append(unit.file.name)
        return parse(unit, index_refs, skip_files, hashes, decisions)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner, "_parse_unit", count)
    try:
        incremental = _run(root)
    finally:
        monkeypatch.undo()
    assert parsed == ["impl.cpp"], "the unit of the changed copy, one time"
    full = _run(root, "db_full")
    assert _odr(incremental) == _odr(full) == before and len(before[0]) == 2
    assert _state(incremental) == _state(full)


def test_an_explicit_specialization_in_a_copy_of_a_header_is_one_type(root):
    """The key of a specialization holds its canonical type: two copies of `H<int>` are one class."""
    header = "template <class T> struct H { int g(); };\ntemplate <> struct H<int> { int f(); };\n"
    _write(root, {
        "sdk/h.h": header, "proj/h.h": header,
        "user.cpp": '#include "sdk/h.h"\nint use(H<int> &h) { return h.f(); }\n',
        "impl.cpp": '#include "proj/h.h"\nint H<int>::f() { return 1; }\n',
    }, ["impl.cpp", "user.cpp"])
    db_path = _run(root)
    assert _calls(db_path) == {("use", "H::f", 1)}, "the call reaches the definition of the copy of the project"
    assert len({row[0] for row in _rows(db_path, "SELECT usr FROM symbols WHERE name = 'f'")}) == 1


def test_two_partial_specializations_of_one_usr_in_copies_of_a_header_stay_two(root):
    """The two copies of each specialization merge, and the two specializations stay two types."""
    header = (
        "template <class> struct is_fn;\n"
        "template <class R, class... A> struct is_fn<R(A...)> { struct In { int x; }; };\n"
        "template <class R, class... A> struct is_fn<R(A...) const> { struct In { int y; }; };\n"
    )
    use = "int {name}() {{ is_fn<int()>::In a{{1}}; is_fn<int() const>::In b{{2}}; return a.x + b.y; }}\n"
    _write(root, {
        "sdk/t.h": header, "proj/t.h": header,
        "a.cpp": '#include "sdk/t.h"\n' + use.format(name="a"), "b.cpp": '#include "proj/t.h"\n' + use.format(name="b"),
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    specializations = {row[0] for row in _rows(db_path, "SELECT usr FROM symbols WHERE name = 'is_fn'")}
    members = {row[0] for row in _rows(db_path, "SELECT usr FROM symbols WHERE name IN ('x', 'y')")}
    assert len(specializations) == 2 and len(members) == 2, (specializations, members)
    assert len(_odr(db_path)[0]) == 4, "two specializations, each in two files; each `In` follows its specialization"


def test_an_instance_of_a_merged_template_is_one_type(root):
    """`Box<int>` has the place of its pattern: the merge of the template merges the instance too."""
    header = "template <class T> struct Box { int get() { return 1; } };\n"
    use = "int {name}() {{ Box<int> box; return box.get(); }}\n"
    _write(root, {
        "sdk/b.h": header, "proj/b.h": header,
        "a.cpp": '#include "sdk/b.h"\n' + use.format(name="a"), "b.cpp": '#include "proj/b.h"\n' + use.format(name="b"),
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    callees = _rows(
        db_path, "SELECT DISTINCT r.to_usr FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " WHERE r.ref_kind = 'call' AND a.name IN ('a', 'b')",
    )
    # A call names the method of the template, and the implicit constructor
    # is a member of the instance: one USR each, from the two copies.
    assert sorted(callees) == [("c:@S@Box>#I@F@Box#",), ("c:@ST>1#T@Box@F@get#",)], callees


def test_an_instance_has_one_usr_in_each_form_of_its_instantiation(root):
    """`Box<int>` of an implicit instance, of `extern template` and of `template struct` is one type.

    libclang gives the definition of an instance the place of the declaration
    that the unit sees last: the pattern, or the line of an explicit
    instantiation.  The place of the template is the same in each unit.
    """
    use = "int {name}() {{ Box<int> b; b.v = 1; return Box<int>::count; }}\n"
    _write(root, {
        "box.h": "#pragma once\ntemplate <class T> struct Box { T v; static int count; };\n"
                 "template <class T> int Box<T>::count = 0;\n",
        "box_ext.h": '#pragma once\n#include "box.h"\nextern template struct Box<int>;\n',
        "def.cpp": '#include "box.h"\ntemplate struct Box<int>;\n' + use.format(name="d"),
        "ext.cpp": '#include "box_ext.h"\n' + use.format(name="e"),
        "plain.cpp": '#include "box.h"\n' + use.format(name="p"),
    }, ["def.cpp", "ext.cpp", "plain.cpp"])
    db_path = _run(root)
    targets = _rows(
        db_path, "SELECT DISTINCT r.to_usr FROM refs r WHERE r.to_usr LIKE 'c:@S@Box>#I%' AND r.ref_kind != 'call'",
    )
    assert len(targets) == 2, f"one field and one static member for the three units: {targets}"
    for file in ("def.cpp", "ext.cpp", "plain.cpp"):
        assert set(_rows(
            db_path, "SELECT DISTINCT to_usr FROM refs WHERE from_file = ? AND to_usr LIKE 'c:@S@Box>#I%'"
            " AND ref_kind != 'call'", file,
        )) == set(targets), file
    count = [usr for (usr,) in targets if usr.endswith("@count")]
    assert _rows(db_path, "SELECT COUNT(*) FROM symbols WHERE usr = ?", count[0]) == [(1,)]
    # The row of a definition does not depend on the template that the unit
    # sees (_usr.odr_place): an explicit instantiation stores a row of the
    # key of the instance, in its own file.  The USRs take the place of the
    # template, which has one row.
    places = _rows(
        db_path, "SELECT DISTINCT f.path FROM type_places p JOIN files f ON f.id = p.file_id"
        " WHERE p.merge_key = 'c:@ST>1#T@Box'",
    )
    assert places == [("box.h",)], "the template has one place"


def test_the_anonymous_members_of_a_merged_class_keep_their_places(root):
    """Two anonymous unions of one struct have one USR of libclang: each keeps its place (a limit of the merge)."""
    header = "struct W { union { int first; }; union { int second; }; int w; };\n"
    _write(root, {
        "sdk/w.h": header, "proj/w.h": header,
        "a.cpp": '#include "sdk/w.h"\nint a(W &w) { return w.first + w.second + w.w; }\n',
        "b.cpp": '#include "proj/w.h"\nint b(W &w) { return w.first + w.second + w.w; }\n',
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    assert [row[0] for row in _symbols(db_path, "W")] == ["c:@S@W"]
    unions = {row[0] for row in _rows(db_path, "SELECT usr FROM symbols WHERE kind = 'union' AND file_path = 'proj/w.h'")}
    assert len(unions) == 2, unions


def test_the_unnamed_type_of_a_field_of_a_merged_class_keeps_the_place_of_each_copy(root):
    """libclang writes the base name of the file and an offset: two same copies give one such USR with no file key."""
    header = "struct M { enum { M_ON, M_OFF } mode; int m; };\n"
    _write(root, {
        "sdk/m.h": header, "proj/m.h": header,
        "a.cpp": '#include "sdk/m.h"\nint a(M &x) { return x.mode == M::M_ON ? x.m : 0; }\n',
        "b.cpp": '#include "proj/m.h"\nint b(M &x) { return x.mode == M::M_OFF ? x.m : 1; }\n',
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    assert [row[0] for row in _symbols(db_path, "M")] == ["c:@S@M"]
    assert [row[0] for row in _symbols(db_path, "M::mode")] == ["c:@S@M@FI@mode"], "a member of the merged class"
    enums = _rows(db_path, "SELECT usr, file_path FROM symbols WHERE kind = 'enum'")
    assert sorted(row[1] for row in enums) == ["proj/m.h", "sdk/m.h"], enums
    assert len({row[0] for row in enums}) == 2, enums


def test_a_type_of_an_anonymous_namespace_in_copies_of_a_header_is_no_merge(root):
    """The compiler gives such a type internal linkage: each copy is a type of its own."""
    header = "namespace { struct X { int v; }; }\ninline int read(X &x) { return x.v; }\n"
    _write(root, {
        "sdk/n.h": header, "proj/n.h": header,
        "a.cpp": '#include "sdk/n.h"\nint a() { X x{1}; return read(x); }\n',
        "b.cpp": '#include "proj/n.h"\nint b() { X x{2}; return read(x); }\n',
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    assert _odr(db_path) == (set(), set())
    types = _rows(db_path, "SELECT usr FROM symbols WHERE name = 'X' AND kind = 'struct'")
    assert len(types) == 2 and all("~n.h~" in row[0] for row in types), types


def test_a_typedef_of_a_merged_class_is_one_member(root):
    """libclang writes the file of each copy before a typedef of a class: the USR of the class names it."""
    header = "namespace drv {\nclass P { public: typedef int size_type; size_type n; };\n}\n"
    _write(root, {
        "sdk/p.h": header, "proj/p.h": header,
        "a.cpp": '#include "sdk/p.h"\nint a(drv::P &p) { drv::P::size_type s = p.n; return s; }\n',
        "b.cpp": '#include "proj/p.h"\nint b(drv::P &p) { drv::P::size_type s = p.n; return s; }\n',
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    assert [row[0] for row in _symbols(db_path, "drv::P::size_type")] == ["c:@N@drv@S@P@T@size_type"]


def test_a_type_of_a_union_template_is_a_member_of_the_template(tmp_path):
    """`@UT>` is the component of a union template: its place is in the USR, and in the USR of its members."""
    from fw_context_mcp.indexer._usr import file_key
    from tests.test_usr_file_key import _usrs

    source = tmp_path / "u.cpp"
    source.write_text("template <class T> union Var { struct N { int n; }; int i; };\n", encoding="utf-8")
    key = file_key(str(source))
    assert _usrs(source, "CLASS_TEMPLATE", "Var") == [f"c:@UT>1#T@Var~{key}:1:26"]
    assert _usrs(source, "STRUCT_DECL", "N") == [f"c:@UT>1#T@Var~{key}:1:26@S@N~{key}:1:39"]


def test_two_types_of_one_name_in_two_main_files_stay_two(root):
    """Only copies of a header merge: a helper struct of two `.cpp` files breaks the ODR, and the index shows it."""
    _write(root, {
        "a.cpp": "struct Ctx { int a; };\nint a(Ctx &c) { return c.a; }\n",
        "b.cpp": "struct Ctx { float b; };\nfloat b(Ctx &c) { return c.b; }\n",
    }, ["a.cpp", "b.cpp"])
    db_path = _run(root)
    assert len(_symbols(db_path, "Ctx")) == 2
    assert _odr(db_path) == (set(), set())


def test_a_file_that_changes_during_the_run_leaves_the_units_to_the_next_run(root, monkeypatch):
    """The copy loses the class after the parse of the loop: no error, the next run gives the index of a full run."""
    from fw_context_mcp.indexer import runner

    _copies(root, impl_header="sdk/p.h")
    _run(root)
    (root / "impl.cpp").write_text(IMPL.format(header="proj/p.h"), encoding="utf-8")
    parse = runner._parse_unit

    def edit_before_the_second_parse(unit, index_refs, skip_files, hashes, decisions):
        if decisions.merged:
            (root / "proj" / "p.h").write_text(PARSER.replace("class Parser", "class Other"), encoding="utf-8")
        return parse(unit, index_refs, skip_files, hashes, decisions)

    monkeypatch.setattr(runner, "_parse_unit", edit_before_the_second_parse)
    late = _run(root)
    assert _odr(late)[1], "the units of the changed file are pending"
    # The second parse stored the hash of the changed text: only the mark of
    # the pending units tells the status that a run must come.
    assert _stale_units(late, root) == _odr(late)[1]
    monkeypatch.setattr(runner, "_parse_unit", parse)
    (root / "proj" / "p.h").write_text(PARSER, encoding="utf-8")
    repaired = _run(root)
    assert _odr(repaired)[1] == set()
    assert _state(repaired) == _state(_run(root, "db_full"))


def test_a_run_whose_loop_stores_nothing_reports_the_units_of_the_merge(root, monkeypatch):
    """A unit that only the merge stores again counts as updated: the post-processing sees the change."""
    from fw_context_mcp.indexer import runner

    _copies(root)
    db_path = _run(root)
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DELETE FROM odr_merged_types")
    conn.close()
    seen: list[int] = []
    postprocess = runner._run_postprocess

    def capture(**kwargs):
        seen.append(kwargs["updated"])
        return postprocess(**kwargs)

    monkeypatch.setattr(runner, "_run_postprocess", capture)
    _run(root)
    assert seen == [2] and len(_odr(db_path)[0]) == 2


def test_a_unit_whose_rows_hold_no_place_of_the_merge_is_not_parsed_again(root, monkeypatch):
    """`z_other.cpp` reads a copy of the header and names no type of it: the merge does not change its rows.

    `user.cpp` sorts before it and claims `sdk/p.h`, thus `z_other.cpp`
    skips the header and its rows hold no place of `Parser`.
    """
    from fw_context_mcp.indexer import runner

    _write(root, {
        "sdk/p.h": PARSER, "proj/p.h": PARSER,
        "user.cpp": USER.format(header="sdk/p.h"), "impl.cpp": IMPL.format(header="proj/p.h"),
        "z_other.cpp": '#include "sdk/p.h"\nint other() { return 0; }\n',
    }, ["impl.cpp", "user.cpp", "z_other.cpp"])
    parsed: list[str] = []
    parse = runner._parse_unit

    def record(unit, index_refs, skip_files, hashes, decisions):
        if decisions.merged:
            parsed.append(unit.file.name)
        return parse(unit, index_refs, skip_files, hashes, decisions)

    monkeypatch.setattr(runner, "_parse_unit", record)
    db_path = _run(root)
    assert sorted(parsed) == ["impl.cpp", "user.cpp"], "the second parse of the merge"
    assert ("use", "drv::Parser::send", 1) in _calls(db_path)

    # An index without the record of the places of a unit: each unit that
    # reads a file of the change is parsed again.
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DELETE FROM odr_merged_types")
        conn.execute("DELETE FROM odr_unit_places WHERE unit_path = 'z_other.cpp'")
    conn.close()
    parsed.clear()
    _run(root)
    assert sorted(parsed) == ["impl.cpp", "user.cpp", "z_other.cpp"]

    # A unit that leaves the build leaves no record of its places.
    _write(root, {}, ["impl.cpp", "user.cpp"])
    _run(root)
    assert _rows(db_path, "SELECT unit_path FROM odr_unit_places ORDER BY unit_path") == [("impl.cpp",), ("user.cpp",)]


def test_a_unit_of_a_struct_in_a_struct_is_parsed_again_when_the_struct_around_it_merges(root):
    """A body of `A` in an other file (`#include` in the body): the unit of `B` reads the merge of `A`.

    `u.cpp` claims `sdk/p.h`, and `z.c` names `B` and not `A`.  In C `B`
    has the scope of the file and the place of `sdk/body.inc`, and it
    follows the merge of `A` (`sdk/p.h`), thus `z.c` must be parsed again
    when `A` merges.  The merged `B` then has the USR that C++ gives it.
    """
    header = '#pragma once\nstruct A {\n#include "body.inc"\n int y; };\n'
    body = "struct B { int x; } b;\n"
    _write(root, {
        "sdk/p.h": header, "proj/p.h": header, "sdk/body.inc": body, "proj/body.inc": body,
        "u.cpp": '#include "sdk/p.h"\nint cu(A &a) { return a.y; }\n',
        "v.cpp": '#include "proj/p.h"\nint cv(A &a) { return a.y; }\n',
        "z.c": '#include "sdk/p.h"\nint cb(struct B *p) { return p->x; }\n',
    }, ["u.cpp", "v.cpp", "z.c"])
    db_path = _run(root)
    assert {key for key, _ in _odr(db_path)[0]} == {"c:@S@A"}
    assert _rows(db_path, "SELECT DISTINCT to_usr FROM refs WHERE from_file = 'z.c' AND to_usr LIKE '%@FI@x'") == [
        ("c:@S@A@S@B@FI@x",),
    ], "`B` follows the merge of `A`: no place, and the USR of C++"


def test_a_pending_unit_whose_second_parse_fails_again_stays_pending(root, monkeypatch):
    """The loop parses the pending unit, the merge changes again, and its second parse fails: it stays pending."""
    from fw_context_mcp.indexer import runner

    _copies(root, impl_header="sdk/p.h")
    _run(root)
    parse = runner._parse_unit
    calls: list[str] = []

    def fail_second_parse_of_user(unit, index_refs, skip_files, hashes, decisions):
        calls.append(unit.file.name)
        if unit.file.name == "user.cpp" and calls.count("user.cpp") == 2:
            return ("skipped", None, None, None)
        return parse(unit, index_refs, skip_files, hashes, decisions)

    def fail_with_marks(unit, index_refs, skip_files, hashes, decisions):
        if decisions.merged and unit.file.name == "user.cpp":
            return ("skipped", None, None, None)
        return parse(unit, index_refs, skip_files, hashes, decisions)

    (root / "impl.cpp").write_text(IMPL.format(header="proj/p.h"), encoding="utf-8")
    monkeypatch.setattr(runner, "_parse_unit", fail_with_marks)
    failed = _run(root)
    assert _odr(failed)[1] == {"user.cpp"}
    # The loop parsed `user.cpp`, thus its entry is fresh: the mark makes it stale.
    assert _stale_units(failed, root) == {"user.cpp"}

    (root / "impl.cpp").write_text(IMPL.format(header="sdk/p.h"), encoding="utf-8")
    monkeypatch.setattr(runner, "_parse_unit", fail_second_parse_of_user)
    again = _run(root)
    assert calls.count("user.cpp") == 2, calls
    assert _odr(again)[1] == {"user.cpp"}
    assert _stale_units(again, root) == {"user.cpp"}

    monkeypatch.setattr(runner, "_parse_unit", parse)
    repaired = _run(root)
    assert _odr(repaired) == (set(), set())
    assert _stale_units(repaired, root) == set()
    assert _state(repaired) == _state(_run(root, "db_full"))


def test_a_unit_that_failed_in_the_loop_is_not_parsed_by_the_merge(root, monkeypatch):
    """Its claims of the run went to a later listing: it stays pending, and the next run parses it."""
    from fw_context_mcp.indexer import runner

    _copies(root, impl_header="sdk/p.h")
    _run(root)
    (root / "impl.cpp").write_text(IMPL.format(header="proj/p.h"), encoding="utf-8")
    (root / "user.cpp").write_text(USER.format(header="sdk/p.h") + "int more() { return 1; }\n", encoding="utf-8")
    calls: list[tuple[str, bool]] = []
    parse = runner._parse_unit

    def fail_user_in_the_loop(unit, index_refs, skip_files, hashes, decisions):
        calls.append((unit.file.name, bool(decisions.merged)))
        if unit.file.name == "user.cpp" and not decisions.merged:
            return ("skipped", None, None, None)
        return parse(unit, index_refs, skip_files, hashes, decisions)

    monkeypatch.setattr(runner, "_parse_unit", fail_user_in_the_loop)
    stopped = _run(root)
    assert ("user.cpp", True) not in calls
    assert "user.cpp" in _odr(stopped)[1]
    monkeypatch.setattr(runner, "_parse_unit", parse)
    repaired = _run(root)
    assert _odr(repaired)[1] == set()
    assert _state(repaired) == _state(_run(root, "db_full"))


def _cli_project(cli_build, impl_header: str):
    """A project of the CLI, for reindex_file: `a_user.cpp` claims `sdk/p.h` before `b_impl.cpp`."""
    return cli_build({
        "sdk/p.h": PARSER, "proj/p.h": PARSER,
        "a_user.cpp": USER.format(header="sdk/p.h"), "b_impl.cpp": IMPL.format(header=impl_header),
    }, ["a_user.cpp", "b_impl.cpp"])


@pytest.fixture
def cli_build(tmp_path: Path):
    """Give a call that makes a project of the CLI with no watcher daemon (see `_cli_build`).

    `fw-context index` starts the watcher daemon at its end
    (`cli._index._ensure_watcher_after_index`), and the daemon starts an
    index run of its own when a file of the project changes.  A test that
    changes files and compares the index with a full run then has a second
    writer in the index directory: measured 2026-11-04, a file of the
    daemon came between the steps of `shutil.rmtree`.
    """
    with contextlib.ExitStack() as stack:
        yield functools.partial(_cli_build, tmp_path, stack)


def _cli_build(tmp_path: Path, stack: contextlib.ExitStack, files: dict[str, str], units: list[str]):
    """A project of the CLI with *files* and *units*, indexed once: the project, its database, and a call that indexes it.

    The test holds `watcher.lock` until *stack* closes.  The lock tells
    that a daemon runs, thus no index run starts one
    (`mcp.background._ensure_daemon_running`).
    """
    from tests.test_incremental_reindex import _cli, _db_path_for_project, _isolate_index_db

    proj = tmp_path / "proj"
    proj.mkdir()
    _write(proj, files, units)
    _isolate_index_db(proj, tmp_path)
    assert _cli(["init", "--project", str(proj)], cwd=proj, timeout=60).returncode == 0
    db_path = _db_path_for_project(proj)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(db_path.parent / "watcher.lock", os.O_CREAT | os.O_RDWR)
    stack.callback(os.close, lock)
    assert try_lock(lock), "a different process holds watcher.lock"

    def index() -> None:
        result = _cli(["index", "--no-analyze", "--no-embeddings", str(proj / "compile_commands.json")], cwd=proj)
        assert result.returncode == 0, result.stderr

    index()
    return proj, db_path, index


def _same_as_a_full_run(db_path: Path, index) -> None:
    """Index again from an empty index directory: the index must be the same as before.

    `watcher.lock` stays, because the test holds it (see `_cli_build`).
    """
    incremental = _state(db_path)
    for entry in db_path.parent.iterdir():
        if entry.name == "watcher.lock":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    index()
    assert incremental == _state(db_path)


def _reindex(proj: Path, file: str, text: str) -> None:
    from fw_context_mcp.mcp.handlers.maintenance import reindex_file
    from tests.test_incremental_reindex import _advance_mtime

    (proj / file).write_text(text, encoding="utf-8")
    _advance_mtime(proj / file, 10.0)
    result = reindex_file(file, str(proj), with_analysis=False)
    assert "error" not in result, result


def test_reindex_file_that_changes_the_places_sends_the_unit_to_the_next_index_run(cli_build):
    """reindex_file sees one unit, not the build: the next index run decides the merge.

    `b_impl.cpp` claims no file that it held before, thus only the places
    send it to the next run.
    """
    from fw_context_mcp.indexer.manifest import load as load_manifest

    proj, db_path, index = _cli_project(cli_build, impl_header="sdk/p.h")
    _reindex(proj, "b_impl.cpp", IMPL.format(header="proj/p.h"))
    assert _odr(db_path) == (set(), set())
    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    entries = load_manifest(db_path.parent, config_hash)["entries"]
    assert [entry.get("needs_reparse") for entry in entries if entry["file"] == "b_impl.cpp"] == [True]

    index()
    assert len(_odr(db_path)[0]) == 2
    _same_as_a_full_run(db_path, index)


def test_reindex_file_that_reads_an_other_copy_sends_the_unit_to_the_next_index_run(cli_build):
    """`d.cpp` now reads the copy that only the C unit `a.c` read: no place changes, the merge does.

    `c.cpp` owns `sdk/q.h`, thus `d.cpp` lost no file.  The definition of
    `proj/q.h` has the same place in the view of `d.cpp` as in the view of
    `a.c`: only the language of a reader of the file changed.
    """
    from fw_context_mcp.indexer.manifest import load as load_manifest

    proj, db_path, index = cli_build({
        "sdk/q.h": "struct cfg { int x; };\n", "proj/q.h": "struct cfg { int x; };\n",
        "a.c": '#include "proj/q.h"\nint a(struct cfg *c) { return c->x; }\n',
        "c.cpp": '#include "sdk/q.h"\nint c(cfg *c) { return c->x; }\n',
        "d.cpp": '#include "sdk/q.h"\nint d(cfg *c) { return c->x; }\n',
    }, ["a.c", "c.cpp", "d.cpp"])
    assert _odr(db_path) == (set(), set())
    places_before = _rows(db_path, "SELECT merge_key, line, col FROM type_places ORDER BY file_id")

    _reindex(proj, "d.cpp", '#include "proj/q.h"\nint d(cfg *c) { return c->x; }\n')
    assert _rows(db_path, "SELECT merge_key, line, col FROM type_places ORDER BY file_id") == places_before
    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    entries = load_manifest(db_path.parent, config_hash)["entries"]
    assert [entry.get("needs_reparse") for entry in entries if entry["file"] == "d.cpp"] == [True]
    index()
    assert len(_odr(db_path)[0]) == 2
    _same_as_a_full_run(db_path, index)


def test_reindex_file_that_changes_the_place_of_a_template_sends_the_unit_to_the_next_index_run(cli_build):
    """The template moves, or a unit looks for a template that the build places: the next run decides.

    `a_x.cpp` does not read `prim.h`, thus the store of `prim.h` does not
    change its rows: the next index run must parse it with the new place.
    """
    from fw_context_mcp.indexer.manifest import load as load_manifest

    proj, db_path, index = cli_build({
        "fwd.h": FWD, "spec.h": SPEC, "prim.h": PRIM, "v.h": "#pragma once\ntemplate <class T> struct V { int g(); };\n",
        "a_x.cpp": SPEC_USER.format(name="x"), "b_y.cpp": '#include "v.h"\n' + PRIM_USER.format(name="y"),
    }, ["a_x.cpp", "b_y.cpp"])
    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]

    def needs_reparse(file: str) -> list:
        entries = load_manifest(db_path.parent, config_hash)["entries"]
        return [entry.get("needs_reparse") for entry in entries if entry["file"] == file]

    _reindex(proj, "prim.h", PRIM.replace("#pragma once\n", "#pragma once\n// moved\n"))
    assert needs_reparse("b_y.cpp") == [True]
    index()
    assert _callers_of(db_path, _f_usr(proj, "prim.h", 4, 27)) == {"a_x.cpp", "b_y.cpp"}
    _same_as_a_full_run(db_path, index)

    # `U` has no definition in the build: each parse gives `U<int>` its own
    # place, thus no decision changes and no unit goes to the next run.
    with_u = "template <class T> struct U;\ntemplate <> struct U<int> { int w; };\n" + SPEC_USER.format(name="x")
    _reindex(proj, "a_x.cpp", with_u)
    assert needs_reparse("a_x.cpp") == [None]
    _same_as_a_full_run(db_path, index)

    # `b_y.cpp` defines `V`, and no unit looked for it: the build has a place
    # of it now.  The second store of the same text changes no row, and the
    # decisions of the build are still the old ones: it sends the unit too.
    with_v = "template <class T> struct V;\ntemplate <> struct V<int> { int w; };\n" + SPEC_USER.format(name="x")
    _reindex(proj, "a_x.cpp", with_v)
    assert needs_reparse("a_x.cpp") == [True]
    _reindex(proj, "a_x.cpp", with_v)
    assert needs_reparse("a_x.cpp") == [True]
    index()
    assert needs_reparse("a_x.cpp") == [None]
    _same_as_a_full_run(db_path, index)


def test_the_delete_of_a_build_removes_the_rows_of_each_table_of_the_places(root, monkeypatch):
    """`delete_build_data` removes the definitions, the decisions, the pending units and the rows of the units."""
    from fw_context_mcp.indexer import runner
    from fw_context_mcp.indexer.db import delete_build_data, open_db

    _write(root, {
        "fwd.h": FWD, "spec.h": SPEC, "sdk/p.h": PARSER, "proj/p.h": PARSER, "prim.h": PRIM,
        "impl.cpp": IMPL.format(header="proj/p.h"), "user.cpp": USER.format(header="sdk/p.h"),
        "x.cpp": SPEC_USER.format(name="x"), "y.cpp": PRIM_USER.format(name="y"),
    }, ["impl.cpp", "user.cpp", "x.cpp", "y.cpp"])
    parse = runner._parse_unit

    def fail_second_parse(unit, index_refs, skip_files, hashes, decisions):
        if decisions.merged and unit.file.name == "user.cpp":
            return ("skipped", None, None, None)
        return parse(unit, index_refs, skip_files, hashes, decisions)

    monkeypatch.setattr(runner, "_parse_unit", fail_second_parse)
    db_path = _run(root)
    tables = ("type_places", "odr_merged_types", "odr_template_places", "odr_pending_units", "odr_unit_places")
    assert all(_rows(db_path, f"SELECT COUNT(*) FROM {table}")[0][0] for table in tables)
    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    conn = open_db(db_path)
    try:
        with conn:
            delete_build_data(conn, config_hash)
    finally:
        conn.close()
    assert [_rows(db_path, f"SELECT COUNT(*) FROM {table}")[0][0] for table in tables] == [0] * 5


def test_a_file_with_a_definition_and_no_row_is_no_orphan(root):
    """All rows of a copy can lose to the rows of the other copy: the place of its definition stays a fact."""
    from fw_context_mcp.indexer.db import delete_orphan_files, open_db

    _copies(root)
    db_path = _run(root)
    conn = open_db(db_path)
    try:
        (file_id,) = conn.execute("SELECT id FROM files WHERE path = 'proj/p.h'").fetchone()
        (config_hash,) = conn.execute("SELECT config_hash FROM build_configs").fetchone()
        with conn:
            conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM macros WHERE file_id = ?", (file_id,))
            conn.execute("UPDATE files SET content = '' WHERE id = ?", (file_id,))
            delete_orphan_files(conn, config_hash, root)
        assert conn.execute("SELECT COUNT(*) FROM files WHERE id = ?", (file_id,)).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("in_sdk", [True, False], ids=["merges", "one-file"])
def test_reindex_file_of_a_copy_with_a_new_definition_sends_its_owner_only_for_a_new_merge(cli_build, in_sdk):
    """The owner of the copy reads the same files: only a change of the decisions sends it to the next run.

    With `Extra` in the other copy too, the new definition makes a group of
    two files.  Without it, `Extra` has one file and no decision changes: a
    `struct` added to a header is no reason for an index run, and the index
    is the one of a full run.
    """
    from fw_context_mcp.indexer.manifest import load as load_manifest

    extra = "struct Extra { int e; };\n"
    proj, db_path, index = cli_build({
        "sdk/p.h": PARSER + (extra if in_sdk else ""), "proj/p.h": PARSER,
        "a_user.cpp": USER.format(header="sdk/p.h"), "b_impl.cpp": IMPL.format(header="proj/p.h"),
    }, ["a_user.cpp", "b_impl.cpp"])
    _reindex(proj, "proj/p.h", PARSER + extra)
    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    entries = load_manifest(db_path.parent, config_hash)["entries"]
    assert [entry.get("needs_reparse") for entry in entries if entry["file"] == "b_impl.cpp"] == [
        True if in_sdk else None,
    ]
    if in_sdk:
        index()
    _same_as_a_full_run(db_path, index)


def test_reindex_file_of_a_c_build_with_a_new_struct_sends_no_unit(cli_build):
    """Two C units read two headers of `struct cfg`: no C++ unit reads them, thus no decision reads them."""
    from fw_context_mcp.indexer.manifest import load as load_manifest

    proj, db_path, _ = cli_build({
        "sdk/q.h": "struct cfg { int x; };\n", "proj/q.h": "struct cfg { int x; };\n",
        "a.c": '#include "sdk/q.h"\nint a(struct cfg *c) { return c->x; }\n',
        "b.c": '#include "proj/q.h"\nint b(struct cfg *c) { return c->x; }\n',
    }, ["a.c", "b.c"])
    _reindex(proj, "a.c", '#include "sdk/q.h"\nstruct extra { int e; };\nint a(struct cfg *c) { return c->x; }\n')
    (config_hash,) = _rows(db_path, "SELECT config_hash FROM build_configs")[0]
    entries = load_manifest(db_path.parent, config_hash)["entries"]
    assert [entry.get("needs_reparse") for entry in entries] == [None, None]


def test_reindex_file_parses_the_cpp_owner_of_a_changed_header_before_the_c_unit(cli_build):
    """`a.c` and the header change: the C++ owner `z.cpp` of the header is parsed first, as in the run.

    In the order of the paths `a.c` comes first, and it claimed the header
    with its C view: the class of the `#ifdef __cplusplus` block was gone.
    """
    from tests.test_incremental_reindex import _advance_mtime

    header = (
        "#pragma once\nstruct cfg { int x; };\n"
        "#ifdef __cplusplus\nnamespace n { struct K { int k; }; }\n#endif\n"
    )
    proj, db_path, index = cli_build({
        "h.h": header,
        "a.c": '#include "h.h"\nint a(struct cfg *c) { return c->x; }\n',
        "z.cpp": '#include "h.h"\nint z(n::K &k) { return k.k; }\n',
    }, ["a.c", "z.cpp"])
    assert _rows(db_path, "SELECT owner_path FROM header_owners WHERE path = 'h.h'") == [("z.cpp",)]
    (proj / "h.h").write_text(header.replace("int x;", "int x; int y;"), encoding="utf-8")
    _advance_mtime(proj / "h.h", 10.0)
    _reindex(proj, "a.c", '#include "h.h"\nint a(struct cfg *c) { return c->x + c->y; }\n')
    assert _rows(db_path, "SELECT owner_path FROM header_owners WHERE path = 'h.h'") == [("z.cpp",)]
    assert _symbols(db_path, "n::K") != []
    _same_as_a_full_run(db_path, index)


def test_reindex_file_of_a_merged_build_writes_merged_rows(cli_build):
    """The rows of reindex_file leave out the merged marks, as the rows of the other units do."""
    proj, db_path, _ = _cli_project(cli_build, impl_header="proj/p.h")
    assert len(_odr(db_path)[0]) == 2
    _reindex(proj, "b_impl.cpp", IMPL.format(header="proj/p.h") + "int more() { return 2; }\n")
    assert [row[0] for row in _symbols(db_path, "drv::Parser")] == ["c:@N@drv@S@Parser"]
    assert ("use", "drv::Parser::send", 1) in _calls(db_path)


def test_a_run_that_stops_after_the_decisions_leaves_the_units_pending(root, monkeypatch):
    """The run stops after it wrote the new decisions and before the second parse: the next run parses the units.

    The new merge and the pending units are one transaction: the stop
    leaves the rows of the loop, which follow the old merge, and the units
    whose rows must change.
    """
    from fw_context_mcp.indexer import runner

    class Stop(Exception):
        pass

    _copies(root, impl_header="sdk/p.h")
    _run(root)
    (root / "impl.cpp").write_text(IMPL.format(header="proj/p.h"), encoding="utf-8")
    parse = runner._parse_unit

    def stop_at_the_second_parse(unit, index_refs, skip_files, hashes, decisions):
        if decisions.merged:
            raise Stop
        return parse(unit, index_refs, skip_files, hashes, decisions)

    monkeypatch.setattr(runner, "_parse_unit", stop_at_the_second_parse)
    with pytest.raises(Stop):
        _run(root)
    db_path = root.parent / "db" / "index.db"
    merged, pending = _odr(db_path)
    assert len(merged) == 2 and pending == {"impl.cpp", "user.cpp"}
    monkeypatch.setattr(runner, "_parse_unit", parse)

    repaired = _run(root)
    assert _odr(repaired)[1] == set()
    assert _state(repaired) == _state(_run(root, "db_full"))


FWD = "#pragma once\ntemplate <class T> struct Tr;\n"
SPEC = '#pragma once\n#include "fwd.h"\ntemplate <> struct Tr<int> { int f(); int v; };\n'
PRIM = '#pragma once\n#include "fwd.h"\ntemplate <class T> struct Tr { int g(); };\n'
SPEC_IMPL = '#include "spec.h"\nint Tr<int>::f() { return 1; }\n'
SPEC_USER = '#include "spec.h"\nint {name}(Tr<int> &t) {{ return t.f() + t.v; }}\n'
PRIM_USER = '#include "prim.h"\n#include "spec.h"\nint {name}(Tr<int> &t) {{ return t.f() + t.v; }}\n'


def _f_usr(root: Path, file: str, line: int, column: int) -> str:
    """The USR of `Tr<int>::f` with the place of the template `Tr` in *file*."""
    from fw_context_mcp.indexer._usr import file_key

    return f"c:@S@Tr>#I~{file_key(str(root / file))}:{line}:{column}@F@f#"


def _callers_of(db_path: Path, usr: str) -> set[str]:
    return {row[0] for row in _rows(db_path, "SELECT from_file FROM refs WHERE to_usr = ? AND ref_kind = 'call'", usr)}


@pytest.mark.parametrize("user_of_prim", ["y.cpp", "a_y.cpp"])
def test_an_explicit_specialization_has_the_place_of_the_template_of_the_build(root, user_of_prim):
    """A unit that declares `Tr` and does not define it gives `Tr<int>` the place that the other units give.

    `x.cpp` sees `template <class T> struct Tr;` only, `y.cpp` sees the
    definition in `prim.h`.  `a_y.cpp` claims `spec.h` before `impl.cpp`:
    the claim order does not change the USR.
    """
    _write(root, {
        "fwd.h": FWD, "spec.h": SPEC, "prim.h": PRIM, "impl.cpp": SPEC_IMPL,
        "x.cpp": SPEC_USER.format(name="x"), user_of_prim: PRIM_USER.format(name="y"),
    }, ["impl.cpp", "x.cpp", user_of_prim])
    db_path = _run(root)
    usr = _f_usr(root, "prim.h", 3, 27)
    assert _rows(db_path, "SELECT usr FROM symbols WHERE usr LIKE '%@F@f#' AND is_definition = 1") == [(usr,)]
    assert _callers_of(db_path, usr) == {"x.cpp", user_of_prim}
    assert _rows(db_path, "SELECT COUNT(DISTINCT to_usr) FROM refs WHERE to_usr LIKE '%Tr>#I%@FI@v'") == [(1,)]
    assert _state(db_path) == _state(_run(root, "db_full"))


def test_the_place_of_a_template_follows_its_definition_in_the_build(root):
    """The definition of the template comes, moves and goes: each run gives the index of a full run.

    `x.cpp` and `impl.cpp` do not read `prim.h`, thus only the place of the
    template in the build sends them to the second parse.
    """
    files = {"fwd.h": FWD, "spec.h": SPEC, "prim.h": PRIM, "impl.cpp": SPEC_IMPL, "x.cpp": SPEC_USER.format(name="x")}
    _write(root, files, ["impl.cpp", "x.cpp"])
    db_path = _run(root)
    own = "c:@S@Tr>#I~{}:3:20@F@f#"
    from fw_context_mcp.indexer._usr import file_key

    assert _callers_of(db_path, own.format(file_key(str(root / "spec.h")))) == {"x.cpp"}

    _write(root, {"y.cpp": PRIM_USER.format(name="y")}, ["impl.cpp", "x.cpp", "y.cpp"])
    _run(root)
    assert _callers_of(db_path, _f_usr(root, "prim.h", 3, 27)) == {"x.cpp", "y.cpp"}
    assert _state(db_path) == _state(_run(root, "db_came"))

    _write(root, {"prim.h": PRIM.replace("#pragma once\n", "#pragma once\n// moved\n")}, ["impl.cpp", "x.cpp", "y.cpp"])
    _run(root)
    assert _callers_of(db_path, _f_usr(root, "prim.h", 4, 27)) == {"x.cpp", "y.cpp"}
    assert _state(db_path) == _state(_run(root, "db_moved"))

    # An index without the record of what `x.cpp` read: it is parsed again
    # when a place of a template changes.
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DELETE FROM odr_unit_places WHERE unit_path = 'x.cpp'")
    conn.close()
    _write(root, {"prim.h": PRIM.replace("#pragma once\n", "#pragma once\n// moved\n\n")}, ["impl.cpp", "x.cpp", "y.cpp"])
    _run(root)
    assert _callers_of(db_path, _f_usr(root, "prim.h", 5, 27)) == {"x.cpp", "y.cpp"}

    _write(root, {}, ["impl.cpp", "x.cpp"])
    _run(root)
    assert _callers_of(db_path, own.format(file_key(str(root / "spec.h")))) == {"x.cpp"}
    assert _state(db_path) == _state(_run(root, "db_went"))


def test_a_merged_template_gives_its_specializations_no_place(root):
    """Two copies of `prim.h` merge `Tr`: the unit that only declares `Tr` gives `Tr<int>` the USR of libclang."""
    _write(root, {
        "fwd.h": FWD, "spec.h": SPEC, "sdk/prim.h": PRIM.replace('"fwd.h"', '"../fwd.h"'),
        "proj/prim.h": PRIM.replace('"fwd.h"', '"../fwd.h"'), "impl.cpp": SPEC_IMPL,
        "x.cpp": SPEC_USER.format(name="x"),
        "y.cpp": PRIM_USER.format(name="y").replace('"prim.h"', '"sdk/prim.h"'),
        "z.cpp": PRIM_USER.format(name="z").replace('"prim.h"', '"proj/prim.h"'),
    }, ["impl.cpp", "x.cpp", "y.cpp", "z.cpp"])
    db_path = _run(root)
    assert _callers_of(db_path, "c:@S@Tr>#I@F@f#") == {"x.cpp", "y.cpp", "z.cpp"}
    assert _state(db_path) == _state(_run(root, "db_full"))


def test_a_template_with_two_definitions_in_main_files_has_no_place_in_the_build(root):
    """Two `.cpp` files define `Tr`: the ODR is broken, and `x.cpp` keeps the place of its specialization."""
    from fw_context_mcp.indexer._usr import file_key

    defining = '#include "fwd.h"\ntemplate <class T> struct Tr {{ int g(); }};\n#include "spec.h"\nint {name}() {{ return 0; }}\n'
    _write(root, {
        "fwd.h": FWD, "spec.h": SPEC, "a.cpp": defining.format(name="a"), "b.cpp": defining.format(name="b"),
        "x.cpp": SPEC_USER.format(name="x"),
    }, ["a.cpp", "b.cpp", "x.cpp"])
    db_path = _run(root)
    assert _rows(db_path, "SELECT template_keys FROM odr_unit_places WHERE unit_path = 'x.cpp'") == [
        ('["c:@ST>1#T@Tr"]',),
    ]
    assert _rows(db_path, "SELECT merge_key FROM odr_template_places") == []
    own = f"c:@S@Tr>#I~{file_key(str(root / 'spec.h'))}:3:20@FI@v"
    assert _rows(db_path, "SELECT DISTINCT to_usr FROM refs WHERE from_file = 'x.cpp' AND to_usr LIKE '%@FI@v'") == [
        (own,),
    ]


def test_two_structs_of_one_name_in_two_merged_structs_stay_two_in_c(root):
    """`A::B` and `C::B`: in C each `B` has the scope of the file, and the merged `B` gets the USR of C++."""
    a = "#pragma once\nstruct A { struct B { int x; } b; };\n"
    c = "#pragma once\nstruct C { struct B { int y; } b; };\n"
    _write(root, {
        "sdk/a.h": a, "proj/a.h": a, "sdk/c.h": c, "proj/c.h": c,
        "a1.c": '#include "sdk/a.h"\nint f1(struct B *p) { return p->x; }\n',
        "a2.c": '#include "sdk/c.h"\nint f2(struct B *p) { return p->y; }\n',
        "u.cpp": '#include "sdk/a.h"\n#include "sdk/c.h"\nint u(A &a, C &c) { return a.b.x + c.b.y; }\n',
        "v.cpp": '#include "proj/a.h"\n#include "proj/c.h"\nint v(A &a, C &c) { return a.b.x + c.b.y; }\n',
    }, ["a1.c", "a2.c", "u.cpp", "v.cpp"])
    db_path = _run(root)
    assert {row[0] for row in _rows(db_path, "SELECT usr FROM symbols WHERE name = 'B'")} == {
        "c:@S@A@S@B", "c:@S@C@S@B",
    }
    for field, usr in (("x", "c:@S@A@S@B@FI@x"), ("y", "c:@S@C@S@B@FI@y")):
        assert _rows(db_path, "SELECT DISTINCT to_usr FROM refs WHERE to_usr LIKE ?", f"%@FI@{field}") == [(usr,)]
    assert _state(db_path) == _state(_run(root, "db_full"))


def test_a_struct_in_a_struct_in_a_merged_struct_has_the_usr_of_cxx_in_c(root):
    """`A::B::D` in C: each type around `D` is in the USR, as C++ writes it."""
    header = "#pragma once\nstruct A { struct B { struct D { int z; } d; } b; };\n"
    _write(root, {
        "sdk/a.h": header, "proj/a.h": header,
        "a1.c": '#include "sdk/a.h"\nint f1(struct D *p) { return p->z; }\n',
        "u.cpp": '#include "sdk/a.h"\nint u(A &a) { return a.b.d.z; }\n',
        "v.cpp": '#include "proj/a.h"\nint v(A &a) { return a.b.d.z; }\n',
    }, ["a1.c", "u.cpp", "v.cpp"])
    db_path = _run(root)
    assert _rows(db_path, "SELECT DISTINCT to_usr FROM refs WHERE to_usr LIKE '%@FI@z'") == [("c:@S@A@S@B@S@D@FI@z",)]
