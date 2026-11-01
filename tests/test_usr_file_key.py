"""The USR of a symbol with no external name names its file by a key of the path (``indexer._usr``).

libclang writes the base name of the file into such a USR (`c:util.h@F@helper`),
thus two files of one base name in two directories gave one symbol for two.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

SOURCES = {
    "a/util.h": "static int counter_value;\nstatic inline int helper(void) { return counter_value + 1; }\n",
    "b/util.h": "static int counter_value;\nstatic inline int helper(void) { return counter_value + 2; }\n",
    "a/x.c": '#include "util.h"\nint use_a(void) { return helper(); }\n',
    "a/x2.c": '#include "util.h"\nint use_a2(void) { return helper(); }\n',
    "b/y.c": '#include "util.h"\nint use_b(void) { return helper(); }\n',
    "a/fatal.c": (
        "static const int log_level = 3;\nint level_a(void) { return log_level; }\n"
        "static int scale(int factor) { int product = factor * 3; return product; }\n"
        "int scaled_a(void) { return scale(1); }\n"
    ),
    "b/fatal.c": (
        "static const int log_level = 4;\nint level_b(void) { return log_level; }\n"
        "static int scale(int factor) { int product = factor * 4; return product; }\n"
        "int scaled_b(void) { return scale(2); }\n"
    ),
    "a/n.cpp": "namespace { int hidden(int v) { return v + 1; } }\nint run_na() { return hidden(1); }\n",
    "b/n.cpp": "namespace { int hidden(int v) { return v + 2; } }\nint run_nb() { return hidden(2); }\n",
    "a/s.c": "static void tick(void) {}\nstatic void (*slot)(void) = tick;\nvoid fire_a(void) { slot(); }\n",
    "b/s.c": "static void tock(void) {}\nstatic void (*slot)(void) = tock;\nvoid fire_b(void) { slot(); }\n",
    # One header through `..`: the key of its real path.
    "c/z.c": '#include "../a/util.h"\nint use_z(void) { return helper(); }\n',
    # A C++ unit reads the header of a C unit: one symbol in the two languages.
    "a/x.cpp": '#include "util.h"\nint use_cpp() { return helper(); }\n',
    # Anonymous types: an enum named by its first constant, a struct named by
    # a typedef, and two anonymous unions of one struct.
    "a/sm.c": (
        "enum { ST_IDLE, ST_RUN };\ntypedef struct { int speed; } cfg_t;\n"
        "int state_a(void) { cfg_t c = { ST_RUN }; return c.speed + ST_IDLE; }\n"
    ),
    "b/sm.c": (
        "enum { ST_IDLE, ST_STOP, ST_RUN };\ntypedef struct { long speed; } cfg_t;\n"
        "int state_b(void) { cfg_t c = { ST_RUN }; return (int)c.speed + ST_STOP; }\n"
    ),
    "a/two.h": (
        "struct two { union { int first; }; union { int second; }; };\n"
        "struct outer { struct { union { int inner; }; int beside; } field; };\n"
    ),
    "a/t1.c": '#include "two.h"\nint t1(struct two *t) { return t->first; }\n',
    "a/t2.c": '#include "two.h"\nint t2(struct two *t, struct outer *o) { return t->second + o->field.inner; }\n',
    "a/t3.cpp": '#include "two.h"\nint t3(two *t) { return t->first + t->second; }\n',
    "a/an.cpp": (
        "namespace { struct AN { union { int n1; }; union { int n2; }; }; }\n"
        "int an_use() { AN a{}; return a.n1 + a.n2; }\n"
    ),
    # A specialization whose argument is a typedef-named type: no anonymous type.
    "a/spec.cpp": (
        "typedef struct { int m; } mb_t;\ntemplate <class T> struct cv { int c; };\n"
        "template <> struct cv<mb_t> { int d; };\n"
        "namespace n { typedef struct { int q; } foo; }\ntemplate <class T> struct foo { T t; };\n"
        "template <> struct foo<n::foo> { int w; };\n"
        "int spec_use() { cv<mb_t> v{}; foo<n::foo> f{}; return v.d + f.w; }\n"
    ),
    "a/l.cpp": "static auto pick = [](int v) { return v + 1; };\nint run_a() { return pick(1); }\n",
    "b/l.cpp": "static auto pick = [](int v) { return v + 1; };\nint run_b() { return pick(2); }\n",
}


def _project(root: Path) -> None:
    (root / ".fw-context").mkdir(parents=True, exist_ok=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-usr"\n\n[build]\n\n[index]\ndb_dir = "{root.parent / "db"}"\n', encoding="utf-8",
    )
    for name, text in SOURCES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    entries = []
    for name in SOURCES:
        if name.endswith(".c"):
            entries.append({"directory": str(root), "file": name, "arguments": ["cc", "-std=gnu11", "-c", name]})
        elif name.endswith(".cpp"):
            entries.append({"directory": str(root), "file": name, "arguments": ["c++", "-std=c++17", "-c", name]})
    (root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")


def _db(root: Path) -> Path:
    return root.parent / "db" / "proj-usr" / "index.db"


def _run(root: Path) -> None:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    _db(root).parent.mkdir(parents=True, exist_ok=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=_db(root), project_root=root,
        project_id="proj-usr", project_name="proj-usr", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )


def _rows(root: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(_db(root))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def indexed(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("usrkey") / "proj"
    root.mkdir()
    _project(root)
    _run(root)
    return root


def test_two_files_of_one_base_name_give_two_symbols(indexed):
    def files_of(name: str) -> list[str]:
        return sorted(row[0] for row in _rows(
            indexed, "SELECT file_path FROM symbols WHERE name = ? AND is_definition = 1", name,
        ))

    assert files_of("helper") == ["a/util.h", "b/util.h"]
    assert files_of("counter_value") == ["a/util.h", "b/util.h"]
    assert files_of("log_level") == ["a/fatal.c", "b/fatal.c"]
    assert files_of("scale") == ["a/fatal.c", "b/fatal.c"]
    assert files_of("product") == ["a/fatal.c", "b/fatal.c"], "a local of two static functions"
    assert files_of("hidden") == ["a/n.cpp", "b/n.cpp"], "a member of an anonymous namespace"
    assert files_of("ST_IDLE") == ["a/sm.c", "b/sm.c"], "a constant of two anonymous enums"
    assert files_of("ST_RUN") == ["a/sm.c", "b/sm.c"]
    assert files_of("speed") == ["a/sm.c", "b/sm.c"], "a field of two structs that two typedefs name"


def test_each_anonymous_type_is_a_symbol_of_its_own(indexed):
    """Two anonymous unions of one struct of one header: two symbols, also when two units read it."""
    rows = _rows(
        indexed, "SELECT line FROM symbols WHERE kind = 'union' AND file_path = 'a/two.h' AND is_definition = 1",
    )
    assert sorted(row[0] for row in rows) == [1, 1, 2], "a C and a C++ unit read the header"
    parents = dict(_rows(indexed, "SELECT name, parent_usr FROM symbols WHERE name IN ('first', 'second')"))
    assert parents["first"] != parents["second"]
    namespace = _rows(indexed, "SELECT COUNT(*) FROM symbols WHERE kind = 'union' AND file_path = 'a/an.cpp'")
    assert namespace == [(2,)], "two anonymous unions of a struct of an anonymous namespace"
    enums = _rows(indexed, "SELECT file_path FROM symbols WHERE kind = 'enum' AND file_path LIKE '%sm.c'")
    assert sorted(row[0] for row in enums) == ["a/sm.c", "b/sm.c"]


def test_a_member_of_an_unnamed_type_with_a_place_stays_inside_the_range_of_its_type(indexed):
    """`struct { union { int inner; }; int beside; } field;`: libclang gives the type a place, and no mark.

    ``find_references`` of a type reads the USRs from `<type>@` to `<type>A`:
    `inner` (in the anonymous union) and `beside` must both be in it.
    """
    usrs = dict(_rows(indexed, "SELECT name, usr FROM symbols WHERE name IN ('inner', 'beside')"))
    owner = usrs["beside"].rsplit("@FI@", 1)[0]
    assert usrs["inner"].startswith(owner + "@"), (owner, usrs["inner"])


def test_a_specialization_for_a_typedef_named_type_is_no_anonymous_type(indexed):
    """`cv<mb_t>`: its USR ends with `@SA@mb_t`, the component of its argument, and it gets no mark."""
    usrs = dict(_rows(indexed, "SELECT name, usr FROM symbols WHERE name IN ('m', 'd', 'w')"))
    assert usrs["d"] == "c:@S@cv>#$@SA@mb_t@FI@d"
    assert usrs["w"] == "c:@S@foo>#$@N@n@SA@foo@FI@w", "a template of the name of the typedef"
    assert usrs["m"].startswith("c:@SA@mb_t~spec.cpp~"), "the typedef-named type gets its mark"


def test_a_reference_names_the_constant_of_its_own_file(indexed):
    calls = set(_rows(
        indexed,
        "SELECT a.name, b.name, b.file_path FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " JOIN symbols b ON b.usr = r.to_usr WHERE a.name LIKE 'state_%' AND b.name LIKE 'ST_%'",
    ))
    assert calls >= {("state_a", "ST_RUN", "a/sm.c"), ("state_b", "ST_RUN", "b/sm.c")}
    assert not {call for call in calls if call[0][-1] != call[2][0]}, "no reference crosses the files"


def test_each_caller_reaches_the_helper_of_its_own_header(indexed):
    calls = set(_rows(
        indexed,
        "SELECT a.name, b.file_path FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " JOIN symbols b ON b.usr = r.to_usr WHERE r.ref_kind = 'call' AND b.name = 'helper'",
    ))
    assert calls == {
        ("use_a", "a/util.h"), ("use_a2", "a/util.h"), ("use_b", "b/util.h"),
        ("use_z", "a/util.h"), ("use_cpp", "a/util.h"),
    }


def test_a_slot_of_each_file_reaches_the_function_of_its_own_file(indexed):
    calls = set(_rows(
        indexed,
        "SELECT a.name, b.name, b.file_path FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " JOIN symbols b ON b.usr = r.to_usr WHERE r.ref_kind = 'call' AND a.name LIKE 'fire_%'",
    ))
    # A call through a pointer variable is also a call edge to the variable.
    assert calls == {
        ("fire_a", "tick", "a/s.c"), ("fire_a", "slot", "a/s.c"),
        ("fire_b", "tock", "b/s.c"), ("fire_b", "slot", "b/s.c"),
    }


def test_a_header_that_two_units_read_stays_one_symbol(indexed):
    """`a/x.c` and `a/x2.c` read one `a/util.h`: one key, one symbol."""
    rows = _rows(indexed, "SELECT usr FROM symbols WHERE name = 'helper' AND file_path = 'a/util.h'")
    assert len(rows) == 1
    from fw_context_mcp.indexer._usr import file_key

    assert rows[0][0] == f"c:{file_key(str(indexed / 'a' / 'util.h'))}@F@helper"


def test_the_lambdas_of_two_files_of_one_base_name_are_two_symbols(indexed):
    rows = _rows(indexed, "SELECT file_path FROM symbols WHERE kind = 'lambda'")
    assert sorted(row[0] for row in rows) == ["a/l.cpp", "b/l.cpp"]


def test_two_lambdas_of_the_scope_of_one_file_have_two_call_operators(tmp_path):
    """The closure of a lambda is an anonymous class (`@Sa`): the mark gives each `operator()` a USR of its own.

    libclang gives the two `operator()` of one file scope one USR.  The index
    names a lambda by its own key (``_lambda_usr``), thus only the USR of
    ``cursor_usr`` shows the difference.
    """
    import clang.cindex as cx

    from fw_context_mcp.indexer._usr import cursor_usr

    source = tmp_path / "gl.cpp"
    source.write_text(
        "static auto l1 = [](int v) { return v + 1; };\nstatic auto l2 = [](int v) { return v + 2; };\n"
        "int use() { return l1(1) + l2(2); }\n",
        encoding="utf-8",
    )
    unit = cx.Index.create().parse(str(source), args=["-x", "c++", "-std=c++17"])
    # The walk does not visit the implicit class of a closure: a call names its `operator()`.
    calls = [node.referenced for node in unit.cursor.walk_preorder()
             if node.kind == cx.CursorKind.CALL_EXPR and node.spelling == "operator()"]
    assert len(calls) == 2 and calls[0].get_usr() == calls[1].get_usr(), "libclang gives one USR"
    assert cursor_usr(calls[0]) != cursor_usr(calls[1])


def test_the_mark_of_an_anonymous_enum_walks_no_child(tmp_path, monkeypatch):
    """The mark reads the end of the USR of the enum, not its first constant.

    ``get_children`` collects each constant, thus a walk to the first one
    for each constant costs the square of the number of constants.
    """
    import clang.cindex as cx

    from fw_context_mcp.indexer._usr import cursor_usr, file_key

    source = tmp_path / "big.cpp"
    # The attribute is the first child of the enum and no constant: a walk
    # to the first constant must skip it.  The mark must not need the walk.
    source.write_text("enum __attribute__((packed)) { BIG_0, BIG_1 };\nenum {};\n", encoding="utf-8")
    unit = cx.Index.create().parse(str(source), args=["-x", "c++", "-std=c++17"])
    enums = [node for node in unit.cursor.walk_preorder() if node.kind == cx.CursorKind.ENUM_DECL]
    constants = [node for node in unit.cursor.walk_preorder() if node.kind == cx.CursorKind.ENUM_CONSTANT_DECL]

    def no_walk(self):
        raise AssertionError("get_children")

    monkeypatch.setattr(cx.Cursor, "get_children", no_walk)
    key = file_key(str(source))
    assert [cursor_usr(node) for node in constants] == [
        f"c:@Ea@BIG_0~{key}:1:1@BIG_0", f"c:@Ea@BIG_0~{key}:1:1@BIG_1",
    ]
    assert cursor_usr(enums[1]) == f"c:@Ea~{key}:2:1", "an enum with no constant"


def test_a_header_through_a_symbolic_link_has_the_key_of_its_real_path(tmp_path):
    from fw_context_mcp.indexer._usr import file_key

    real = tmp_path / "inc"
    real.mkdir()
    (real / "util.h").write_text("static int value;\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic link on this file system")
    assert file_key(str(link / "util.h")) == file_key(str(real / "util.h"))


def test_an_external_name_keeps_the_usr_of_libclang(indexed):
    assert _rows(indexed, "SELECT usr FROM symbols WHERE name = 'use_a'") == [("c:@F@use_a",)]


class _Cursor:
    """A cursor with a USR and the file of its canonical declaration."""

    def __init__(self, usr: str, file: str) -> None:
        self.usr = usr
        self.location = type("Location", (), {"file": type("File", (), {"name": file})()})()
        self.canonical = self

    def get_usr(self) -> str:
        return self.usr


def test_the_file_of_the_canonical_declaration_names_the_key():
    from fw_context_mcp.indexer._usr import cursor_usr, file_key

    cursor = _Cursor("c:helper.c@F@helper", "/src/a/helper.c")
    assert cursor_usr(cursor) == f"c:{file_key('/src/a/helper.c')}@F@helper"


def test_a_prefix_that_is_not_the_file_of_the_canonical_declaration_stays_as_libclang_wrote_it():
    """The base name of the file of the canonical declaration is not the prefix: no guess."""
    from fw_context_mcp.indexer._usr import cursor_usr

    assert cursor_usr(_Cursor("c:missing.c@F@helper", "/src/other.c")) == "c:missing.c@F@helper"
