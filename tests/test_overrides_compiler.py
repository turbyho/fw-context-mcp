"""The table ``overrides`` comes from the compiler (``indexer/_overrides.py``).

It came from a match of the names and of the text of the signatures.  A
text cannot tell the types: an override that writes a parameter through an
alias had no row, and a method of a class template had none.  The compiler
records each override (``clang_getOverriddenCursors``); each ancestor gets a
row.  The parse of a unit writes the rows of its methods, and a re-parse of
the file replaces them, thus an incremental run follows the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

SHAPES = """\
#include <stdint.h>
typedef void (*cb_t)(int);
using handler_t = cb_t;
struct A { virtual void post(uint32_t id, cb_t cb) = 0; virtual ~A() {} };
struct D : A { void post(unsigned int id, handler_t cb) override { (void)id; (void)cb; } };
template <class T> struct Tq : A { void post(uint32_t, cb_t cb) override { (void)cb; } };
struct B : A { void post(uint32_t, cb_t) override {} };
struct C : B { void post(uint32_t, cb_t) override {} };
struct X { virtual void g() {} };
struct M : A, X { void post(uint32_t, cb_t) override {} void g() override {} };
template <class T> struct IQueue { virtual void put(T cb) = 0; virtual ~IQueue() {} };
struct Impl : IQueue<cb_t> { void put(cb_t cb) override { (void)cb; } };
"""


@pytest.fixture(scope="module")
def overrides(tmp_path_factory) -> set[tuple[str, str]]:
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("overrides") / "shapes.cpp"
    src.write_text(SHAPES, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    result = extract_all(unit, with_refs=False)
    names = {symbol.usr: symbol.qualified_name for symbol in result.symbols}
    return {(names.get(derived, derived), names.get(base, base)) for derived, base in result.overrides}


def test_an_override_through_a_type_alias_is_one(overrides):
    """`unsigned int` and a typedef of the pointer: the text differs, the types do not."""
    assert ("D::post", "A::post") in overrides


def test_a_method_of_a_class_template_overrides(overrides):
    assert ("Tq::post", "A::post") in overrides


def test_each_ancestor_gets_a_row(overrides):
    assert {("C::post", "B::post"), ("C::post", "A::post")} <= overrides


def test_multiple_inheritance_gives_each_base(overrides):
    assert {("M::post", "A::post"), ("M::g", "X::g")} <= overrides


def test_a_base_of_an_instance_of_a_template_is_the_member_of_the_template(overrides):
    """`struct Impl : IQueue<cb_t>`: the compiler gives the member of the instance, which is no symbol."""
    assert ("Impl::put", "IQueue::put") in overrides


def test_a_null_cursor_of_the_compiler_is_no_method(tmp_path: Path, monkeypatch):
    """`Cursor.from_result` gives None for a null cursor of the answer: it is left out, not given as a method."""
    from fw_context_mcp.indexer import _overrides
    from fw_context_mcp.indexer._libclang import cindex as cx
    from fw_context_mcp.indexer.symbols import _get_index

    src = tmp_path / "one.cpp"
    src.write_text("struct A { virtual void f(); };\n", encoding="utf-8")
    unit = _get_index().parse(str(src), args=["-x", "c++", "-std=c++17"])
    method = next(c for c in unit.cursor.walk_preorder() if c.kind == cx.CursorKind.CXX_METHOD)
    answer = (cx.Cursor * 2)(cx.conf.lib.clang_getNullCursor(), method)

    def get(_method, array, count):
        array._obj.contents = answer[0]
        count._obj.value = 2

    monkeypatch.setattr(_overrides, "_functions", lambda: (get, lambda _array: None))
    assert [cursor.spelling for cursor in _overrides._overridden(method)] == ["f"]


# ── the index ───────────────────────────────────────────────────────────────

BEFORE = """\
struct Base { virtual void f(); virtual void g(); virtual ~Base(); };
struct Derived : Base { void f() override; };
void Derived::f() {}
"""
AFTER = """\
struct Base { virtual void f(); virtual void g(); virtual ~Base(); };
struct Derived : Base { void g() override; };
void Derived::g() {}
"""


def _index(root: Path, db_path: Path) -> set[tuple[str, str]]:
    import sqlite3

    import fw_context_mcp  # noqa: F401
    from fw_context_mcp.indexer.runner import run

    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id="proj-overrides", project_name="proj-overrides", index_refs=False,
        index_embeddings=False, analyze_symbols=False, record_git=False,
    )
    conn = sqlite3.connect(db_path)
    try:
        return set(conn.execute(
            "SELECT DISTINCT d.qualified_name, b.qualified_name FROM overrides o"
            " JOIN symbols d ON d.usr = o.derived_usr JOIN symbols b ON b.usr = o.base_usr",
        ).fetchall())
    finally:
        conn.close()


def test_an_incremental_run_follows_a_changed_override(tmp_path: Path):
    """`Derived::f -> Base::f` goes when the source drops it, and `Derived::g -> Base::g` comes."""
    root = tmp_path / "proj"
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-overrides"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n', encoding="utf-8",
    )
    source = root / "main.cpp"
    source.write_text(BEFORE, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([{
        "directory": str(root), "file": "main.cpp", "arguments": ["c++", "-std=c++17", "-c", "main.cpp"],
    }]), encoding="utf-8")
    db_path = tmp_path / "proj-overrides" / "index.db"
    db_path.parent.mkdir(parents=True)

    assert _index(root, db_path) == {("Derived::f", "Base::f")}
    source.write_text(AFTER, encoding="utf-8")
    assert _index(root, db_path) == {("Derived::g", "Base::g")}
