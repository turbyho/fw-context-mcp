"""The USR of a symbol with no external name names its file by a key of the path (``indexer._usr``).

libclang writes the base name of the file into such a USR (`c:util.h@F@helper`),
thus two files of one base name in two directories gave one symbol for two.
"""

from __future__ import annotations

import json
import re
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
    # Named types: two drivers define `struct config`.
    "a/cfg1.c": "struct config { int level; };\nint cfg1(struct config *c) { return c->level; }\n",
    "b/cfg2.c": "struct config { long rate; int level; };\nint cfg2(struct config *c) { return c->level; }\n",
    # One struct that C and C++ read with other members (as CMSIS `__I`), a
    # typedef that two headers declare, and a typedef of an opaque struct.
    "a/reg.h": (
        "struct regs {\n#ifdef __cplusplus\n  volatile int r;\n#else\n  volatile const int r;\n#endif\n};\n"
        "typedef unsigned int word_t;\ntypedef struct tm_s tm_t;\n"
    ),
    "a/reg2.h": "typedef unsigned int word_t;\n",
    "a/r1.c": '#include "reg.h"\n#include "reg2.h"\nint r1(struct regs *p, word_t w, tm_t *t) { return p->r + (int)w + !t; }\n',
    "a/r2.cpp": '#include "reg2.h"\n#include "reg.h"\nint r2(regs *p, word_t w) { return p->r + (int)w; }\n',
    "a/tm.c": '#include "reg.h"\nstruct tm_s { int ticks; };\nint ticks_of(tm_t *t) { return t->ticks; }\n',
    "a/hid.cpp": "namespace { typedef int hid_t; }\nint hid_a() { hid_t h = 1; return h; }\n",
    "b/hid.cpp": "namespace { typedef int hid_t; }\nint hid_b() { hid_t h = 2; return h; }\n",
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
    assert re.fullmatch(r"c:@S@outer~two\.h~[0-9a-f]{8}:2:8@S@two\.h~[0-9a-f]{8}@\d+", owner), owner


def test_a_specialization_for_a_typedef_named_type_gets_the_place_of_its_template(indexed):
    """`cv<mb_t>`: its USR ends with `@SA@mb_t`, the component of its argument, which gets no mark.

    The mark after it is the place of the template `cv` (line 2), not of the
    explicit specialization (line 3): a template and its arguments are one type.
    """
    from fw_context_mcp.indexer._usr import file_key

    key = file_key(str(indexed / "a" / "spec.cpp"))
    usrs = dict(_rows(indexed, "SELECT name, usr FROM symbols WHERE name IN ('m', 'd', 'w')"))
    assert usrs["d"] == f"c:@S@cv>#$@SA@mb_t~{key}:2:27@FI@d"
    assert usrs["w"] == f"c:@S@foo>#$@N@n@SA@foo~{key}:5:27@FI@w", "a template of the name of the typedef"
    assert usrs["m"] == f"c:@SA@mb_t~{key}:1:9@FI@m", "the typedef-named type"


def test_a_specialization_with_no_definition_keeps_the_usr_of_libclang(tmp_path):
    """A specialization is no anonymous type, thus its declaration gives no mark: the unit has no definition."""
    import clang.cindex as cx

    from fw_context_mcp.indexer._usr import cursor_usr

    source = tmp_path / "decl.cpp"
    source.write_text(
        "typedef struct { int m; } mb_t;\ntemplate <class T> struct cv;\ntemplate <> struct cv<mb_t>;\n"
        "cv<mb_t> *p;\n",
        encoding="utf-8",
    )
    unit = cx.Index.create().parse(str(source), args=["-x", "c++", "-std=c++17"])
    specialization = next(node for node in unit.cursor.walk_preorder()
                          if node.kind == cx.CursorKind.STRUCT_DECL and node.location.line == 3)
    assert cursor_usr(specialization) == "c:@S@cv>#$@SA@mb_t"


def test_an_explicit_specialization_in_two_main_files_is_one_type(tmp_path):
    """`template <> struct T1<int>` of one template is one entity in C++ ([temp.expl.spec]): one USR."""
    from fw_context_mcp.indexer._usr import file_key

    (tmp_path / "t.h").write_text("template <class T> struct T1 { int a; };\n", encoding="utf-8")
    usrs = []
    for directory in ("a", "b"):
        source = tmp_path / directory / "s.cpp"
        source.parent.mkdir()
        source.write_text('#include "../t.h"\ntemplate <> struct T1<int> { int b; };\n', encoding="utf-8")
        usrs += _usrs(source, "FIELD_DECL", "b")
    key = file_key(str(tmp_path / "t.h"))
    assert usrs == [f"c:@S@T1>#I~{key}:1:27@FI@b"] * 2


def test_a_partial_specialization_in_two_main_files_has_the_place_of_each(tmp_path):
    """A partial specialization is a template of its own: its place is its definition, not the place of `T1`."""
    (tmp_path / "t.h").write_text("template <class T> struct T1 { int a; };\n", encoding="utf-8")
    usrs = []
    for directory in ("a", "b"):
        source = tmp_path / directory / "p.cpp"
        source.parent.mkdir()
        source.write_text('#include "../t.h"\ntemplate <class T> struct T1<T *> { int p; };\n', encoding="utf-8")
        usrs += _usrs(source, "FIELD_DECL", "p")
    assert len(usrs) == 2 and usrs[0] != usrs[1], usrs
    assert all(re.search(r"~p\.cpp~[0-9a-f]{8}:2:", usr) for usr in usrs), usrs


def test_a_specialization_of_a_template_with_no_definition_has_its_own_place(tmp_path):
    """`template <class T> struct D;` has no place: the explicit specialization of each file is its own type."""
    usrs = []
    for directory in ("a", "b"):
        source = tmp_path / directory / "d.cpp"
        source.parent.mkdir()
        source.write_text("template <class T> struct D;\ntemplate <> struct D<int> { int c; };\n", encoding="utf-8")
        usrs += _usrs(source, "FIELD_DECL", "c")
    assert len(usrs) == 2 and usrs[0] != usrs[1], usrs
    assert all(re.fullmatch(r"c:@S@D>#I~d\.cpp~[0-9a-f]{8}:2:20@FI@c", usr) for usr in usrs), usrs


def _usrs(source: Path, kind_name: str, name: str, *extra: str) -> list[str]:
    """``cursor_usr`` of each cursor of the kind *kind_name* and the spelling *name* that a parse of *source* holds.

    The parse keeps the preprocessing record, as ``symbols.extract_all``
    does: the key of a character type of C++ reads the predefined macros.
    """
    import clang.cindex as cx

    from fw_context_mcp.indexer._usr import cursor_usr

    language = ["-x", "c++", "-std=c++17"] if source.suffix == ".cpp" else ["-x", "c", "-std=gnu11"]
    unit = cx.Index.create().parse(
        str(source), args=[*language, *extra], options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    )
    kind = getattr(cx.CursorKind, kind_name)
    return [cursor_usr(node) for node in unit.cursor.walk_preorder() if node.kind == kind and node.spelling == name]


def test_two_types_of_one_name_in_two_files_are_two_symbols(indexed):
    """Two drivers define `struct config`: two types, each with the members and the references of its file."""
    files = _rows(indexed, "SELECT file_path FROM symbols WHERE name = 'config' AND kind = 'struct'")
    assert sorted(row[0] for row in files) == ["a/cfg1.c", "b/cfg2.c"]
    reads = set(_rows(
        indexed,
        "SELECT a.name, b.file_path FROM refs r JOIN symbols a ON a.usr = r.from_usr"
        " JOIN symbols b ON b.usr = r.to_usr WHERE b.name = 'level'",
    ))
    assert reads == {("cfg1", "a/cfg1.c"), ("cfg2", "b/cfg2.c")}


def test_one_definition_is_one_type_in_c_and_in_cpp(indexed):
    """`struct regs` has other members in C and in C++ (as CMSIS `__I`): one place, one type."""
    in_c = _usrs(indexed / "a" / "r1.c", "STRUCT_DECL", "regs")
    in_cpp = _usrs(indexed / "a" / "r2.cpp", "STRUCT_DECL", "regs")
    assert len(set(in_c + in_cpp)) == 1, (in_c, in_cpp)
    assert _rows(indexed, "SELECT COUNT(*) FROM symbols WHERE name = 'regs'") == [(1,)]


def test_a_forward_declaration_with_no_definition_keeps_the_usr_of_libclang(indexed):
    """`tm_t *t` of `r1.c` sees `struct tm_s` opaque: no unit can tell its definition."""
    assert _usrs(indexed / "a" / "r1.c", "STRUCT_DECL", "tm_s") == ["c:@S@tm_s"]
    assert _usrs(indexed / "a" / "tm.c", "STRUCT_DECL", "tm_s")[-1].startswith("c:@S@tm_s~tm.c~")


def test_a_typedef_that_two_headers_declare_is_one_usr_with_no_file(indexed):
    """libclang writes the file of the first declaration: the order of the includes decided the USR."""
    usrs = [*_usrs(indexed / "a" / "r1.c", "TYPEDEF_DECL", "word_t"),
            *_usrs(indexed / "a" / "r2.cpp", "TYPEDEF_DECL", "word_t")]
    assert len(usrs) == 4 and len(set(usrs)) == 1 and usrs[0].startswith("c:@T@word_t~"), usrs


def test_a_typedef_of_an_opaque_struct_has_one_usr_with_and_without_the_definition(indexed):
    without = _usrs(indexed / "a" / "r1.c", "TYPEDEF_DECL", "tm_t")
    with_definition = _usrs(indexed / "a" / "tm.c", "TYPEDEF_DECL", "tm_t")
    assert len(set(without + with_definition)) == 1, (without, with_definition)


def test_two_typedefs_of_one_name_and_two_targets_are_two_usrs(indexed):
    """`cfg_t` names an other anonymous struct in `a/sm.c` and `b/sm.c`."""
    rows = _rows(indexed, "SELECT file_path, usr FROM symbols WHERE name = 'cfg_t' AND kind = 'typedef'")
    assert sorted(row[0] for row in rows) == ["a/sm.c", "b/sm.c"]
    assert len({row[1] for row in rows}) == 2 and all(row[1].startswith("c:@T@cfg_t~") for row in rows)


def test_two_typedefs_of_the_unnamed_types_of_two_fields_at_one_offset_are_two_usrs(tmp_path):
    """The key of the target names the unnamed type of a field by its USR with the places, not by the USR of libclang.

    libclang writes `c:@S@H@S@ft.c@11` in the two files: the place of `H`
    and the key of the file tell the two types apart.
    """
    text = "struct H { struct { int ft_level; } f; };\ntypedef __typeof__(((struct H *)0)->f) ft_t;\n"
    usrs = []
    for directory in ("a", "b"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "ft.c").write_text(text, encoding="utf-8")
        usrs += _usrs(tmp_path / directory / "ft.c", "TYPEDEF_DECL", "ft_t")
    assert len(usrs) == 2 and len(set(usrs)) == 2, usrs


def test_a_local_of_a_method_of_the_unnamed_type_of_a_field_keeps_its_own_place(tmp_path):
    """The USR of a local starts with its own place, not with the USR of the type: it gets the key of its file only."""
    from fw_context_mcp.indexer._usr import file_key

    source = tmp_path / "g.cpp"
    source.write_text("struct { int f() { int loc = 1; return loc; } } g;\n", encoding="utf-8")
    usrs = _usrs(source, "VAR_DECL", "loc")
    pattern = rf"c:{re.escape(file_key(str(source)))}@\d+@S@g\.cpp@\d+@F@f#@loc"
    assert usrs and all(re.fullmatch(pattern, usr) for usr in usrs), usrs


def test_a_typedef_in_an_anonymous_namespace_keeps_its_file(indexed):
    rows = _rows(indexed, "SELECT file_path, usr FROM symbols WHERE name = 'hid_t'")
    assert sorted(row[0] for row in rows) == ["a/hid.cpp", "b/hid.cpp"]
    assert all("hid.cpp~" in row[1] for row in rows), rows


def test_the_key_of_a_target_is_its_canonical_type(tmp_path):
    """`word_t` is `unsigned int` in one header and a second typedef of it in an other: one type, one USR."""
    (tmp_path / "w1.h").write_text("typedef unsigned int word_t;\n", encoding="utf-8")
    (tmp_path / "w2.h").write_text("typedef unsigned int base_t;\ntypedef base_t word_t;\n", encoding="utf-8")
    (tmp_path / "u1.c").write_text('#include "w1.h"\n', encoding="utf-8")
    (tmp_path / "u2.c").write_text('#include "w2.h"\n', encoding="utf-8")
    usrs = [*_usrs(tmp_path / "u1.c", "TYPEDEF_DECL", "word_t"), *_usrs(tmp_path / "u2.c", "TYPEDEF_DECL", "word_t")]
    assert len(usrs) == 2 and len(set(usrs)) == 1, usrs


def test_the_qualifiers_of_a_target_are_in_its_key(tmp_path):
    """`const int`, `volatile int` and `int` are three types: three typedefs of one name give three USRs."""
    usrs = []
    for name, target in (("q1.c", "const int"), ("q2.c", "volatile int"), ("q3.c", "int")):
        (tmp_path / name).write_text(f"typedef {target} level_t;\n", encoding="utf-8")
        usrs += _usrs(tmp_path / name, "TYPEDEF_DECL", "level_t")
    assert len(usrs) == 3 and len(set(usrs)) == 3, usrs


def test_an_alias_template_and_its_pattern_have_one_usr(tmp_path):
    """libclang gives the template and its pattern one USR: the two get the key of the target of the pattern.

    The index keeps the pattern, and a reference names the template.  Two
    alias templates of one name and two targets give two USRs, as a typedef
    does.
    """
    rm = "template <class T> struct rm { using type = T; };\n"
    (tmp_path / "a.cpp").write_text(
        f"namespace ns {{\n{rm}template <class T> using rm_t = typename rm<T>::type;\n}}\nns::rm_t<int> v;\n",
        encoding="utf-8",
    )
    (tmp_path / "b.cpp").write_text("namespace ns {\ntemplate <class T> using rm_t = T *;\n}\n", encoding="utf-8")
    first = [*_usrs(tmp_path / "a.cpp", "TYPE_ALIAS_TEMPLATE_DECL", "rm_t"),
             *_usrs(tmp_path / "a.cpp", "TYPE_ALIAS_DECL", "rm_t")]
    assert len(first) == 2 and len(set(first)) == 1 and first[0].startswith("c:@N@ns@rm_t~"), first
    second = _usrs(tmp_path / "b.cpp", "TYPE_ALIAS_TEMPLATE_DECL", "rm_t")
    assert len(second) == 1 and second[0] != first[0], (first, second)


def test_a_typedef_in_a_class_gets_no_key_of_its_target(tmp_path):
    """C++ does not let a class declare a typedef again: the USR of the class (with its place) names it.

    libclang writes the file before it, and the file of each copy of a
    header is an other one: the USR has no file.
    """
    from fw_context_mcp.indexer._usr import file_key

    source = tmp_path / "member.cpp"
    source.write_text("struct Uart { typedef int size_type; size_type n; };\n", encoding="utf-8")
    assert _usrs(source, "TYPEDEF_DECL", "size_type") == [f"c:@S@Uart~{file_key(str(source))}:1:8@T@size_type"]


def test_a_type_of_the_compiler_and_an_empty_list_of_parameters_give_one_key_in_c_and_cpp(tmp_path):
    """ARM gives `__builtin_va_list` the struct `__va_list` in C and `std::__va_list` in C++.

    `void (*)()` has no prototype in C and no parameters in C++: one text of
    one header.
    """
    header = tmp_path / "va.h"
    header.write_text("typedef __builtin_va_list va_t;\ntypedef void (*fn_t)();\n", encoding="utf-8")
    for name in ("u.c", "u.cpp"):
        (tmp_path / name).write_text('#include "va.h"\n', encoding="utf-8")
    target = "--target=thumbv7em-none-eabi"
    for typedef in ("va_t", "fn_t"):
        in_c = _usrs(tmp_path / "u.c", "TYPEDEF_DECL", typedef, target)
        in_cpp = _usrs(tmp_path / "u.cpp", "TYPEDEF_DECL", typedef, target)
        assert len(in_c) == 1 and in_c == in_cpp, (typedef, in_c, in_cpp)


# A header that C and C++ read: C declares the character types as
# `<stddef.h>`, a libc and C23 do (`<uchar.h>`, 7.30), C++ has them as
# keywords.  WHY no `#include <stddef.h>`: clang finds no `stddef.h` for
# msp430 with no sysroot.
_CHARACTER_HEADER = """#ifndef __cplusplus
typedef __WCHAR_TYPE__ wchar_t;
typedef __UINT_LEAST16_TYPE__ char16_t;
typedef __UINT_LEAST32_TYPE__ char32_t;
typedef unsigned char char8_t;
#endif
typedef wchar_t wc_t;
typedef char16_t c16_t;
typedef char32_t c32_t;
typedef const char8_t c8_t;
typedef const wchar_t *pwc_t;
typedef void (*wcb_t)(wchar_t, char32_t);
"""


@pytest.mark.parametrize("target", ["--target=x86_64-linux-gnu", "--target=thumbv7em-none-eabi", "--target=msp430"])
def test_a_typedef_of_a_character_type_has_one_usr_in_c_and_cpp(tmp_path, target):
    """C++ has `wchar_t`, `char16_t`, `char32_t` and `char8_t` as keywords, and C has integer types: one header, one key.

    The targets give `wchar_t` a signed and an unsigned type, and msp430
    gives `char32_t` of C (`uint_least32_t`) an other type than
    `__CHAR32_TYPE__`.
    """
    import clang.cindex as cx

    (tmp_path / "ch.h").write_text(_CHARACTER_HEADER, encoding="utf-8")
    for name, language in (("u.c", ["-x", "c", "-std=gnu11"]), ("u.cpp", ["-x", "c++", "-std=c++20"])):
        (tmp_path / name).write_text('#include "ch.h"\n', encoding="utf-8")
        unit = cx.Index.create().parse(str(tmp_path / name), args=[*language, target, "-ffreestanding"])
        assert not [d.spelling for d in unit.diagnostics if d.severity >= cx.Diagnostic.Error], name
    keys = set()
    for typedef in ("wc_t", "c16_t", "c32_t", "c8_t", "pwc_t", "wcb_t"):
        in_c = _usrs(tmp_path / "u.c", "TYPEDEF_DECL", typedef, target, "-ffreestanding")
        in_cpp = _usrs(tmp_path / "u.cpp", "TYPEDEF_DECL", typedef, target, "-ffreestanding", "-std=c++20")
        assert len(in_c) == 1 and in_c == in_cpp, (typedef, in_c, in_cpp)
        keys.add(in_c[0])
    assert len(keys) == 6, keys


def test_a_definition_of_the_command_line_gives_the_key_of_a_character_type(tmp_path):
    """C reads `wchar_t` from the last definition of `__WCHAR_TYPE__`: `-D` comes after the macros of the compiler."""
    (tmp_path / "ch.h").write_text(_CHARACTER_HEADER, encoding="utf-8")
    for name in ("u.c", "u.cpp"):
        (tmp_path / name).write_text('#include "ch.h"\n', encoding="utf-8")
    flags = ("--target=x86_64-linux-gnu", "-U__WCHAR_TYPE__", "-D__WCHAR_TYPE__=short")
    in_c = _usrs(tmp_path / "u.c", "TYPEDEF_DECL", "wc_t", *flags)
    in_cpp = _usrs(tmp_path / "u.cpp", "TYPEDEF_DECL", "wc_t", *flags)
    plain = _usrs(tmp_path / "u.c", "TYPEDEF_DECL", "wc_t", "--target=x86_64-linux-gnu")
    assert len(in_c) == 1 and in_c == in_cpp and in_c != plain, (in_c, in_cpp, plain)


@pytest.mark.parametrize("text", ["short unsigned int", "unsigned short int", "int short unsigned"])
def test_the_key_of_a_character_type_takes_the_specifiers_in_any_order(tmp_path, text):
    """A build through a GCC driver gives libclang `-undef` and the macros of GCC, which writes `short unsigned int`.

    The text of clang is `unsigned short`.  The key read only the ten texts
    of clang: each unit of C++ with such a typedef raised ValueError, and
    the index run skipped it.
    """
    import clang.cindex as cx

    (tmp_path / "ch.h").write_text(_CHARACTER_HEADER, encoding="utf-8")
    for name in ("u.c", "u.cpp"):
        (tmp_path / name).write_text('#include "ch.h"\n', encoding="utf-8")

    def flags(char16: str) -> tuple[str, ...]:
        return (
            "--target=thumbv7em-none-eabi", "-undef", "-D__WCHAR_TYPE__=unsigned int",
            f"-D__UINT_LEAST16_TYPE__={char16}", "-D__UINT_LEAST32_TYPE__=long unsigned int",
        )

    for name, language in (("u.c", ["-x", "c", "-std=gnu11"]), ("u.cpp", ["-x", "c++", "-std=c++20"])):
        unit = cx.Index.create().parse(str(tmp_path / name), args=[*language, *flags(text)])
        assert not [d.spelling for d in unit.diagnostics if d.severity >= cx.Diagnostic.Error], name
    in_c = _usrs(tmp_path / "u.c", "TYPEDEF_DECL", "c16_t", *flags(text))
    in_cpp = _usrs(tmp_path / "u.cpp", "TYPEDEF_DECL", "c16_t", *flags(text), "-std=c++20")
    of_clang = _usrs(tmp_path / "u.cpp", "TYPEDEF_DECL", "c16_t", *flags("unsigned short"), "-std=c++20")
    assert len(in_c) == 1 and in_c == in_cpp == of_clang, (in_c, in_cpp, of_clang)


def test_a_character_type_of_no_integer_type_is_an_error(tmp_path):
    """A text that is no integer type gives no key: an error, not a key that depends on the language."""
    source = tmp_path / "w.cpp"
    source.write_text("typedef wchar_t wc_t;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'float', which is not an integer type with a known sign"):
        _usrs(source, "TYPEDEF_DECL", "wc_t", "-U__WCHAR_TYPE__", "-D__WCHAR_TYPE__=float")


def test_the_key_of_a_character_type_needs_the_preprocessing_record(tmp_path):
    """With no predefined macro, the key would depend on the language of the unit: an error, not a guess."""
    import clang.cindex as cx

    from fw_context_mcp.indexer._usr import cursor_usr

    source = tmp_path / "w.cpp"
    source.write_text("typedef wchar_t wc_t;\n", encoding="utf-8")
    unit = cx.Index.create().parse(str(source), args=["-x", "c++", "-std=c++17"])
    typedef = next(node for node in unit.cursor.get_children() if node.spelling == "wc_t")
    with pytest.raises(ValueError, match="has no predefined macro"):
        cursor_usr(typedef)


def test_a_type_in_the_body_of_a_type_in_a_function_of_c_keeps_the_form_of_c(tmp_path):
    """C gives such a type the scope of the function, and C++ writes the function with its signature: no form of C++.

    The USR of a field is the USR of its type and its own component, and two
    functions give two types.
    """
    source = tmp_path / "local.c"
    source.write_text(
        "int f(void) { struct A { struct B { int x; } b; } a; a.b.x = 1; return a.b.x; }\n"
        "int g(void) { struct A { struct B { int x; } b; } a; a.b.x = 2; return a.b.x; }\n",
        encoding="utf-8",
    )
    types = sorted(set(_usrs(source, "STRUCT_DECL", "B")))
    fields = sorted(set(_usrs(source, "FIELD_DECL", "x")))
    assert len(types) == 2, types
    assert fields == [f"{usr}@FI@x" for usr in types], (types, fields)
    assert all("@S@A" not in usr and usr.count("local.c~") == 2 for usr in types), types


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
    """`a/x.c`, `a/x2.c` and `a/x.cpp` read one `a/util.h`: one key, one symbol.

    The C++ unit claims the header (``db.claim_order``), thus the symbol has
    the USR of C++, and the calls of the C units name it too.
    """
    rows = _rows(indexed, "SELECT usr FROM symbols WHERE name = 'helper' AND file_path = 'a/util.h'")
    assert len(rows) == 1
    from fw_context_mcp.indexer._usr import file_key

    usr = f"c:{file_key(str(indexed / 'a' / 'util.h'))}@F@helper#"
    assert rows[0][0] == usr
    callers = _rows(indexed, "SELECT DISTINCT from_file FROM refs WHERE to_usr = ? AND ref_kind = 'call'", usr)
    assert sorted(row[0] for row in callers) == ["a/x.c", "a/x.cpp", "a/x2.c", "c/z.c"]


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
    """A function with a USR and the file of its canonical declaration."""

    def __init__(self, usr: str, file: str) -> None:
        import clang.cindex as cx

        self.kind = cx.CursorKind.FUNCTION_DECL
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
