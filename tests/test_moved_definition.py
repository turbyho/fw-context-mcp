"""A definition that moves to a file of an other unit stays in the index (``db._symbols.mark_stale_files``).

One row of ``symbols`` holds each USR, and the first definition wins.  The
parse of the new place came before the delete of the old rows, thus the
upsert dropped the new definition, and the delete then removed the
symbol: it was missing until a full run.  Before the first parse the runner
marks the files whose rows the run replaces or removes; a row of such a
file gives way to any new row, as a row that the run did not insert yet.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

HEADER = "struct B { virtual void f(); virtual ~B() {} };\nstruct D : B { void f() override; };\n"


def _write(root: Path, files: dict[str, str], units: list[str]) -> None:
    (root / ".fw-context").mkdir(parents=True, exist_ok=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-move"\n\n[build]\n\n[index]\ndb_dir = "{root.parent / "db"}"\n', encoding="utf-8",
    )
    for file, text in files.items():
        (root / file).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": file,
         "arguments": (["cc", "-std=gnu11"] if file.endswith(".c") else ["c++", "-std=c++17"]) + ["-c", file]}
        for file in units
    ]), encoding="utf-8")


def _db(root: Path) -> Path:
    return root.parent / "db" / "proj-move" / "index.db"


def _run(root: Path, *names: str) -> list[tuple[str, str, int]]:
    import fw_context_mcp  # noqa: F401  (pysqlite3 redirect first)
    from fw_context_mcp.indexer.runner import run

    _db(root).parent.mkdir(parents=True, exist_ok=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=_db(root), project_root=root,
        project_id="proj-move", project_name="proj-move", index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )
    conn = sqlite3.connect(_db(root))
    try:
        marks = ",".join("?" * len(names))
        return conn.execute(
            f"SELECT qualified_name, file_path, is_definition FROM symbols WHERE qualified_name IN ({marks})"
            " ORDER BY qualified_name",
            names,
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def test_a_body_moved_inline_into_a_header_of_an_other_unit_stays(root):
    """`D::f` leaves `z_d.cpp` (deleted) for an inline body in `b.h`, which `a_main.cpp` owns."""
    _write(root, {
        "b.h": HEADER,
        "a_main.cpp": '#include "b.h"\nvoid B::f() {}\nint main() { D d; B &b = d; b.f(); return 0; }\n',
        "z_d.cpp": '#include "b.h"\nvoid D::f() {}\n',
    }, ["a_main.cpp", "z_d.cpp"])
    assert _run(root, "D::f") == [("D::f", "z_d.cpp", 1)]

    (root / "z_d.cpp").unlink()
    _write(root, {"b.h": HEADER.replace("void f() override;", "void f() override {}")}, ["a_main.cpp"])

    assert _run(root, "D::f") == [("D::f", "b.h", 1)]
    conn = sqlite3.connect(_db(root))
    overrides = conn.execute(
        "SELECT d.qualified_name, b.qualified_name FROM overrides o JOIN symbols d ON d.usr = o.derived_usr"
        " JOIN symbols b ON b.usr = o.base_usr",
    ).fetchall()
    conn.close()
    assert ("D::f", "B::f") in overrides


def test_a_definition_moved_to_a_unit_that_comes_first_stays(root):
    """`helper` moves from `z.cpp` to `a.cpp`: the parse of `a.cpp` comes before the parse of `z.cpp`."""
    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 1; }\nint other() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]

    _write(root, {
        "a.cpp": '#include "h.h"\nint helper() { return 1; }\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint other() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "a.cpp", 1)]


def test_a_definition_that_stays_keeps_its_row_in_an_incremental_run(root):
    """A changed unit that does not define the function gives a declaration: the definition stays."""
    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 1; }\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]

    _write(root, {"a.cpp": '#include "h.h"\nint use() { return helper() + 1; }\n'}, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "z.cpp", 1)]


def test_a_new_row_of_a_unit_of_the_run_is_current_for_the_units_after_it(root):
    """`a.cpp` and `z.cpp` change; the declaration of `z.cpp` must not replace the new definition of `a.cpp`."""
    _write(root, {
        "a.cpp": "int helper() { return 1; }\n",
        "z.cpp": "int helper();\nint use() { return helper(); }\n",
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "a.cpp", 1)]

    _write(root, {
        "a.cpp": "int helper() { return 2; }\n",
        "z.cpp": "int helper();\nint use() { return helper() + 1; }\n",
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "a.cpp", 1)]


def _fail_unit(monkeypatch, name: str, where: str) -> None:
    """Make the parse or the store of the unit *name* fail, as a parse error or a full disk does."""
    from fw_context_mcp.indexer import _unit_processor, runner

    if where == "parse":
        original = runner._parse_unit

        def parse(unit, *args, **kwargs):
            if unit.file.name == name:
                return ("skipped", None, None, None)
            return original(unit, *args, **kwargs)

        monkeypatch.setattr(runner, "_parse_unit", parse)
    else:
        store = _unit_processor.store_symbols_for_unit

        def store_then_fail(conn, unit, *args, **kwargs):
            result = store(conn, unit, *args, **kwargs)
            if unit.file.name == name:
                raise RuntimeError("the disk is full")
            return result

        monkeypatch.setattr(_unit_processor, "store_symbols_for_unit", store_then_fail)


@pytest.mark.parametrize("where", ["parse", "store"])
def test_a_unit_that_fails_keeps_its_definition(root, monkeypatch, where):
    """The parse or the store of `m.cpp` fails: its rows stay, and the declaration of `z.cpp` comes after.

    A failed store rolls back the rows and the end of the stale mark that
    the store did: the runner ends the mark again.
    """
    _write(root, {
        "m.cpp": "int helper() { return 1; }\n",
        "z.cpp": "int helper();\nint use() { return helper(); }\n",
    }, ["m.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "m.cpp", 1)]

    _write(root, {
        "m.cpp": "int helper() { return 2; }\n",
        "z.cpp": "int helper();\nint use() { return helper() + 1; }\n",
    }, ["m.cpp", "z.cpp"])
    _fail_unit(monkeypatch, "m.cpp", where)

    assert _run(root, "helper") == [("helper", "m.cpp", 1)]


def test_a_header_handed_to_a_unit_that_fails_keeps_its_definition(root, monkeypatch):
    """`a.cpp` stops to include `h.h`; `m.cpp` gets it and fails; the declaration of `z.cpp` comes after."""
    _write(root, {
        "h.h": "inline int helper() { return 1; }\n",
        "p.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use_a() { return helper(); }\n',
        "m.cpp": '#include "h.h"\nint use_m() { return helper(); }\n',
        "z.cpp": '#include "p.h"\nint use_z() { return helper(); }\n',
    }, ["a.cpp", "m.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "h.h", 1)]

    _write(root, {
        "a.cpp": "int use_a() { return 0; }\n",
        "z.cpp": '#include "p.h"\nint use_z() { return helper() + 1; }\n',
    }, ["a.cpp", "m.cpp", "z.cpp"])
    _fail_unit(monkeypatch, "m.cpp", "parse")

    assert _run(root, "helper") == [("helper", "h.h", 1)]


@pytest.mark.parametrize("where", ["parse", "store"])
def test_a_weak_definition_does_not_replace_the_strong_one_of_a_failed_unit(root, monkeypatch, where):
    """`a_strong.cpp` changes and fails; the weak `cb` of `b_weak.cpp` must not take its place."""
    _write(root, {
        "a_strong.cpp": "int cb() { return 1; }\n",
        "b_weak.cpp": "__attribute__((weak)) int cb() { return 0; }\n",
    }, ["a_strong.cpp", "b_weak.cpp"])
    assert _run(root, "cb") == [("cb", "a_strong.cpp", 1)]

    _write(root, {
        "a_strong.cpp": "int cb() { return 2; }\n",
        "b_weak.cpp": "__attribute__((weak)) int cb() { return 3; }\n",
    }, ["a_strong.cpp", "b_weak.cpp"])
    _fail_unit(monkeypatch, "a_strong.cpp", where)

    assert _run(root, "cb") == [("cb", "a_strong.cpp", 1)]


def test_a_tentative_definition_does_not_replace_the_one_of_a_failed_unit(root, monkeypatch):
    """`int g_x = 5;` of `t1.c` changes and fails; `int g_x;` of `t2.c` must not take its place."""
    _write(root, {"t1.c": "int g_x = 5;\n", "t2.c": "int g_x;\n"}, ["t1.c", "t2.c"])
    assert _run(root, "g_x") == [("g_x", "t1.c", 1)]

    _write(root, {"t1.c": "int g_x = 6;\n", "t2.c": "int g_x;\nint t2;\n"}, ["t1.c", "t2.c"])
    _fail_unit(monkeypatch, "t1.c", "parse")

    assert _run(root, "g_x") == [("g_x", "t1.c", 1)]


def test_a_header_that_an_earlier_unit_claimed_keeps_its_current_rows(root, monkeypatch):
    """`a.cpp` now includes `x.h` and writes its current definition; `f.cpp`, the old owner, fails.

    The header changed, but its rows are the rows of `a.cpp` now: the
    definition of `z.cpp` after it must not replace them, as in a run
    with no failure.
    """
    _write(root, {
        "x.h": "int foo();\n",
        "a.cpp": "int use_a() { return 0; }\n",
        "f.cpp": '#include "x.h"\nint use_f() { return foo(); }\n',
        "z.cpp": "int foo() { return 0; }\n",
    }, ["a.cpp", "f.cpp", "z.cpp"])
    assert _run(root, "foo") == [("foo", "z.cpp", 1)]

    _write(root, {
        "x.h": "inline int foo() { return 2; }\n",
        "a.cpp": '#include "x.h"\nint use_a() { return foo(); }\n',
        "z.cpp": "int foo() { return 1; }\n",
    }, ["a.cpp", "f.cpp", "z.cpp"])
    _fail_unit(monkeypatch, "f.cpp", "parse")

    assert _run(root, "foo") == [("foo", "x.h", 1)]


def test_an_unchanged_header_that_a_failed_store_claims_keeps_its_definition(root, monkeypatch):
    """`a.cpp` now includes the unchanged `h.h`, and its store fails: the declaration of `z.cpp` comes after."""
    _write(root, {
        "h.h": "inline int foo() { return 1; }\n",
        "p.h": "int foo();\n",
        "a.cpp": "int use_a() { return 0; }\n",
        "f.cpp": '#include "h.h"\nint use_f() { return foo(); }\n',
        "z.cpp": '#include "p.h"\nint use_z() { return foo(); }\n',
    }, ["a.cpp", "f.cpp", "z.cpp"])
    assert _run(root, "foo") == [("foo", "h.h", 1)]

    _write(root, {
        "a.cpp": '#include "h.h"\nint use_a() { return foo(); }\n',
        "f.cpp": '#include "h.h"\nint use_f() { return foo() + 1; }\n',
        "z.cpp": '#include "p.h"\nint use_z() { return foo() + 1; }\n',
    }, ["a.cpp", "f.cpp", "z.cpp"])
    _fail_unit(monkeypatch, "a.cpp", "store")

    assert _run(root, "foo") == [("foo", "h.h", 1)]


def test_a_file_with_rows_and_no_owner_is_stale_too(root):
    """An index of a version without the claims: the move of the first test needs the mark all the same."""
    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 1; }\nint other() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]
    conn = sqlite3.connect(_db(root))
    with conn:
        conn.execute("DELETE FROM header_owners")
        conn.execute("DELETE FROM owner_units")
    conn.close()

    _write(root, {
        "a.cpp": '#include "h.h"\nint helper() { return 1; }\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint other() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "a.cpp", 1)]


def _give_analysis(root: Path, name: str) -> None:
    """Give the symbol *name* an analysis, its copy in ``symbols`` and an embedding, as the analysis and embedding steps do."""
    from fw_context_mcp.indexer.db import open_db, transaction, upsert_llm_analysis_batch

    conn = open_db(_db(root))
    try:
        with transaction(conn):
            [symbol_id] = [row[0] for row in conn.execute("SELECT id FROM symbols WHERE name = ?", (name,))]
            upsert_llm_analysis_batch(conn, [(symbol_id, "returns one", "nothing", "one", "m", "hash")])
            conn.execute(
                "INSERT INTO embeddings (symbol_id, chunk_index, embedding, model, content_hash) VALUES (?, 0, ?, 'm', 'h')",
                (symbol_id, b"\0" * 16),
            )
    finally:
        conn.close()


def _analysis_of(root: Path, name: str) -> tuple[int, list[tuple[str, str, str]], int]:
    """The analysis rows, the copy in ``symbols`` and the embedding rows of the symbol *name*."""
    conn = sqlite3.connect(_db(root))
    try:
        analysis = conn.execute(
            "SELECT COUNT(*) FROM llm_analysis a JOIN symbols s ON s.id = a.symbol_id WHERE s.name = ?", (name,),
        ).fetchone()[0]
        copy = conn.execute("SELECT summary, inputs, outputs FROM symbols WHERE name = ?", (name,)).fetchall()
        embeddings = conn.execute(
            "SELECT COUNT(*) FROM embeddings e JOIN symbols s ON s.id = e.symbol_id WHERE s.name = ?", (name,),
        ).fetchone()[0]
        return analysis, copy, embeddings
    finally:
        conn.close()


def test_a_definition_that_becomes_a_declaration_loses_the_analysis_of_its_body(root):
    """The body of `helper` goes, its prototype stays: the analysis of the body must go with it."""
    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use() { return 1; }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 1; }\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]
    _give_analysis(root, "helper")
    assert _analysis_of(root, "helper") == (1, [("returns one", "nothing", "one")], 1)

    _write(root, {
        "a.cpp": '#include "h.h"\nint use() { return 2; }\n',
        "z.cpp": '#include "h.h"\nint other() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "h.h", 0)]
    # search_code reads the copy of the analysis in symbols.
    assert _analysis_of(root, "helper") == (0, [("", "", "")], 0)


def test_a_definition_that_replaces_a_stale_one_loses_the_old_analysis(root):
    """`helper` moves from `z.cpp` to the earlier `a.cpp` with an other body: the analysis of the old body must go."""
    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 1; }\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]
    _give_analysis(root, "helper")

    _write(root, {
        "a.cpp": '#include "h.h"\nint helper() { return 2; }\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint other() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "a.cpp", 1)]
    assert _analysis_of(root, "helper") == (0, [("", "", "")], 0)


def test_a_vector_table_without_its_module_stops_no_run(root):
    """An index can have `vec_symbols` while this connection did not load sqlite-vec."""
    from fw_context_mcp.indexer.db import mark_stale_files, open_db
    from fw_context_mcp.indexer.db._symbols import _drop_old_data_of_stale_rows

    _write(root, {"z.cpp": "int helper() { return 1; }\n"}, ["z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]

    class NoVec:
        def __init__(self, conn):
            self.conn = conn
            self.asked = False

        def execute(self, sql, *args):
            if "vec_symbols" in sql:
                self.asked = True
                raise sqlite3.OperationalError("no such module: vec0")
            return self.conn.execute(sql, *args)

        def executemany(self, sql, *args):
            return self.conn.executemany(sql, *args)

    conn = open_db(_db(root))
    try:
        [(config_hash, usr)] = [tuple(row) for row in conn.execute(
            "SELECT config_hash, usr FROM symbols WHERE name = 'helper'"
        )]
        mark_stale_files(conn, config_hash, ["z.cpp"])
        proxy = NoVec(conn)
        _drop_old_data_of_stale_rows(proxy, [(config_hash, usr)])
        assert proxy.asked
    finally:
        conn.close()


def test_a_replaced_row_stashes_its_embedding_and_loses_its_vector(root):
    """The embedding step takes the vector of an unchanged text back from the stash; the vector of the row goes."""
    pytest.importorskip("sqlite_vec", reason="sqlite-vec not available")
    from fw_context_mcp.indexer.db import mark_stale_files, open_db
    from fw_context_mcp.indexer.db._embeddings import init_vec_table, upsert_embeddings_vec
    from fw_context_mcp.indexer.db._symbols import _drop_old_data_of_stale_rows

    _write(root, {"z.cpp": "int helper() { return 1; }\n"}, ["z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]
    _give_analysis(root, "helper")

    conn = open_db(_db(root))
    try:
        [(symbol_id, config_hash, usr)] = [tuple(row) for row in conn.execute(
            "SELECT id, config_hash, usr FROM symbols WHERE name = 'helper'"
        )]
        init_vec_table(conn, dim=4, recreate=True)
        upsert_embeddings_vec(conn, [(symbol_id, 0, config_hash, [0.1, 0.2, 0.3, 0.4])])
        mark_stale_files(conn, config_hash, ["z.cpp"])

        _drop_old_data_of_stale_rows(conn, [(config_hash, usr)])

        assert conn.execute("SELECT COUNT(*) FROM vec_symbols WHERE symbol_id = ?", (symbol_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE symbol_id = ?", (symbol_id,)).fetchone()[0] == 0
        assert [tuple(row) for row in conn.execute("SELECT donor_id, content_hash FROM embedding_stash")] == [
            (symbol_id, "h"),
        ]
    finally:
        conn.close()


def test_a_declaration_of_an_earlier_unit_then_the_changed_definition(root):
    """`a.cpp` replaces the stale definition with its declaration, then `z.cpp` gives the new body."""
    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": '#include "h.h"\nint use() { return helper(); }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 1; }\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "z.cpp", 1)]
    _give_analysis(root, "helper")

    _write(root, {
        "a.cpp": '#include "h.h"\nint use() { return helper() + 1; }\n',
        "z.cpp": '#include "h.h"\nint helper() { return 2; }\n',
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "z.cpp", 1)]
    assert _analysis_of(root, "helper") == (0, [("", "", "")], 0)


def test_a_moved_method_loses_the_override_of_its_old_base(root):
    """`D` loses its base `B`, and `D::f` moves from `z.cpp` to the earlier `a.cpp`.

    `insert_overrides_batch` replaces the overrides of a method that a unit
    gives again; `D::f` now overrides nothing, thus no unit gives a row.
    """
    _write(root, {
        "h.h": "struct B { virtual void f(); };\nstruct D : B { void f() override; };\n",
        "a.cpp": '#include "h.h"\nint use(D &d) { d.f(); return 0; }\n',
        "z.cpp": '#include "h.h"\nvoid D::f() {}\n',
    }, ["a.cpp", "z.cpp"])
    assert _run(root, "D::f") == [("D::f", "z.cpp", 1)]
    assert _overrides_of(root, "D::f") == ["B::f"]

    _write(root, {
        "h.h": "struct B { virtual void f(); };\nstruct D { void f(); };\n",
        "a.cpp": '#include "h.h"\nvoid D::f() {}\nint use(D &d) { d.f(); return 0; }\n',
        "z.cpp": '#include "h.h"\nint other() { return 0; }\n',
    }, ["a.cpp", "z.cpp"])

    assert _run(root, "D::f") == [("D::f", "a.cpp", 1)]
    assert _overrides_of(root, "D::f") == []


def _overrides_of(root: Path, name: str) -> list[str]:
    """The qualified names of the methods that the method *name* overrides."""
    conn = sqlite3.connect(_db(root))
    try:
        return [row[0] for row in conn.execute(
            "SELECT b.qualified_name FROM overrides o"
            " JOIN symbols d ON d.usr = o.derived_usr AND d.config_hash = o.config_hash"
            " JOIN symbols b ON b.usr = o.base_usr AND b.config_hash = o.config_hash"
            " WHERE d.qualified_name = ? ORDER BY b.qualified_name",
            (name,),
        )]
    finally:
        conn.close()


def test_a_header_that_an_heir_claims_after_a_moved_definition(root):
    """`a.cpp` stops to include `h.h`, the body leaves `h.h` for `m.cpp`, and `z.cpp` claims `h.h` after `m.cpp`."""
    _write(root, {
        "h.h": "inline int helper() { return 1; }\n",
        "a.cpp": '#include "h.h"\nint use_a() { return helper(); }\n',
        "m.cpp": "int other() { return 0; }\n",
        "z.cpp": '#include "h.h"\nint use_z() { return helper(); }\n',
    }, ["a.cpp", "m.cpp", "z.cpp"])
    assert _run(root, "helper") == [("helper", "h.h", 1)]

    _write(root, {
        "h.h": "int helper();\n",
        "a.cpp": "int use_a() { return 0; }\n",
        "m.cpp": '#include "h.h"\nint helper() { return 1; }\n',
    }, ["a.cpp", "m.cpp", "z.cpp"])

    assert _run(root, "helper") == [("helper", "m.cpp", 1)]


def test_a_stale_strong_definition_still_wins_over_an_earlier_weak_one(root):
    """`b_weak.cpp` comes first in the second run: the strong `cb` of `c_strong.cpp` must win again."""
    _write(root, {
        "b_weak.cpp": "__attribute__((weak)) int cb() { return 0; }\n",
        "c_strong.cpp": "int cb() { return 1; }\n",
    }, ["b_weak.cpp", "c_strong.cpp"])
    assert _run(root, "cb") == [("cb", "c_strong.cpp", 1)]

    _write(root, {
        "b_weak.cpp": "__attribute__((weak)) int cb() { return 10; }\n",
        "c_strong.cpp": "int cb() { return 11; }\n",
    }, ["b_weak.cpp", "c_strong.cpp"])

    assert _run(root, "cb") == [("cb", "c_strong.cpp", 1)]


def test_a_stale_definition_with_an_initializer_still_wins_over_an_earlier_tentative_one(root):
    """`int g_x;` of `t1.c` comes first: `int g_x = 5;` of `t2.c` must win again, as the linker merges them."""
    _write(root, {"t1.c": "int g_x;\n", "t2.c": "int g_x = 5;\n"}, ["t1.c", "t2.c"])
    assert _run(root, "g_x") == [("g_x", "t2.c", 1)]

    _write(root, {"t1.c": "int g_x;\nint t1;\n", "t2.c": "int g_x = 6;\n"}, ["t1.c", "t2.c"])

    assert _run(root, "g_x") == [("g_x", "t2.c", 1)]
