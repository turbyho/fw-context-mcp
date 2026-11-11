"""The slot facts of the bodies of template instances, and how the index keeps them.

A call in a template body that depends on a parameter has no target in the
written code: ``post(&Q::run<F>, &f)`` names an overload set until an
instance chooses the function.  The bodies of the instances that the unit
uses give those facts (``_slots.collect_slot_facts``).  Such a fact can lie
in a header that an other unit owns, and each unit that uses the instance
gives it, thus the index keeps it with its unit (column ``unit``), and a
new parse of that unit replaces it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._toml import toml_string
from tests._usr_keys import base_names

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

TEMPLATES = """\
typedef void (*cb_t)(void *);
void post(cb_t cb, void *e);
struct Q {
    template <typename F> static void run(void *p) { (*(F *)p)(); }
    template <typename F> void call(F f) { post(&Q::run<F>, &f); }
};
"""

SHAPES = TEMPLATES + """\
typedef void (*hook_t)(int);
hook_t g_hook;
#define TWICE(f) do { f(1); f(2); } while (0)
template <class T> void each(T t) { g_hook(1); TWICE(g_hook); t(3); }
struct Fn { void operator()() {} };
void h1(int);
void h2(int);
void use(Q &q) { q.call(Fn()); each(h1); each(h2); }
"""

def _run(file: str) -> str:
    """The USR of ``Q::run`` of the templates in *file* (a file key, or a base name after ``base_names``).

    `struct Q` is at line 3, column 8 of ``TEMPLATES``: the USR of a type
    holds the place of its definition.
    """
    return f"c:@S@Q~{file}:3:8@FT@>1#Trun#*v#v#S"


def _is_post(key: str, prefix: str = "") -> bool:
    """The code key (or with *prefix* the slot) of ``post``: its USR numbers the types of the unit."""
    return key.startswith(prefix + "c:@F@post#") and (not prefix or key.endswith("|0"))


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("instances") / "shapes.cpp"
    src.write_text(SHAPES, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    return base_names(extract_all(unit, with_refs=True).slots, src)


def _line(text: str) -> int:
    return next(i for i, row in enumerate(SHAPES.splitlines(), 1) if text in row)


def test_an_address_in_a_template_body_comes_from_the_instance(facts):
    """The written code names an overload set: only the instance names the function."""
    line = _line("post(&Q::run<F>, &f)")
    assert not [s for s in facts.sources if s.line == line]
    found = [(s.function, s.slot) for s in facts.from_instances.sources if s.line == line]
    assert len(found) == 1 and found[0][0] == _run("shapes.cpp") and _is_post(found[0][1], "P|"), found


def test_a_call_of_the_template_and_of_its_instances_is_one_site(facts):
    """``g_hook(1)`` depends on no parameter: the template and two instances show it at one place."""
    line = _line("template <class T> void each")
    every = [*facts.sites, *facts.from_instances.sites]
    column = SHAPES.splitlines()[line - 1].index("g_hook(1)") + 1
    assert [s.ordinal for s in every if s.line == line and s.column == column] == [0]


def test_two_calls_of_one_macro_expansion_stay_two_sites(facts):
    line = _line("template <class T> void each")
    column = SHAPES.splitlines()[line - 1].index("TWICE(g_hook)") + 1
    every = [*facts.sites, *facts.from_instances.sites]
    assert sorted(s.ordinal for s in every if s.line == line and s.column == column) == [0, 1]


def test_a_call_that_depends_on_the_parameter_is_a_site_of_the_instance(facts):
    """``t(3)`` calls a function pointer in the instance ``each<void (*)(int)>``."""
    line = _line("template <class T> void each")
    column = SHAPES.splitlines()[line - 1].index("t(3)") + 1
    assert not [s for s in facts.sites if s.line == line and s.column == column]
    assert [s.kind for s in facts.from_instances.sites if s.line == line and s.column == column] == ["pointer"]


def test_no_fact_of_an_instance_repeats_a_fact_of_the_written_code(facts):
    for name in ("sources", "flows", "arguments", "escapes"):
        assert not set(getattr(facts, name)) & set(getattr(facts.from_instances, name)), name


# ── the index ───────────────────────────────────────────────────────────────

POST_C = """\
#include "q.h"
void post(cb_t cb, void *e) { cb(e); }
"""
UNIT_A = """\
#include "q.h"
struct A { void operator()() {} };
void use_a(Q &q) { q.call(A()); }
"""
UNIT_B = """\
#include "q.h"
struct B { void operator()() {} };
void use_b(Q &q) { q.call(B()); }
"""


def _project(root: Path, base: Path) -> Path:
    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "proj-instances"\n\n[build]\n\n[index]\ndb_dir = {toml_string(base)}\n', encoding="utf-8",
    )
    (root / "q.h").write_text(TEMPLATES, encoding="utf-8")
    for name, text in (("a.cpp", UNIT_A), ("b.cpp", UNIT_B), ("post.cpp", POST_C)):
        (root / name).write_text(text, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([
        {"directory": str(root), "file": name, "arguments": ["c++", "-std=c++17", "-c", name, "-o", name + ".o"]}
        for name in ("a.cpp", "b.cpp", "post.cpp")
    ]), encoding="utf-8")
    db_path = base / "proj-instances" / "index.db"
    db_path.parent.mkdir(parents=True)
    return db_path


def _index(root: Path, db_path: Path) -> None:
    from fw_context_mcp.indexer.runner import run

    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id="proj-instances", project_name="proj-instances", index_refs=True,
        index_embeddings=False, analyze_symbols=False, record_git=False,
    )


def _header_run(db_path: Path) -> str:
    """The USR of ``Q::run`` of `q.h` of the project of *db_path* (see ``_project``)."""
    from fw_context_mcp.indexer._usr import file_key

    return _run(file_key(str(db_path.parent.parent / "proj" / "q.h")))


def _rows(db_path: Path) -> list[tuple]:
    from fw_context_mcp.indexer.db import open_db

    conn = open_db(db_path)
    try:
        return sorted(
            (row["file"], row["line"], row["function"], row["slot"], row["unit"].split("|", 1)[1])
            for row in conn.execute("SELECT file, line, function, slot, unit FROM slot_sources WHERE function = ?",
                                    (_header_run(db_path),))
        )
    finally:
        conn.close()


def _edges(db_path: Path) -> set[tuple]:
    from fw_context_mcp.indexer.db import open_db
    from fw_context_mcp.indexer.models import RefOrigin

    conn = open_db(db_path)
    try:
        return {
            (row["from_usr"], row["to_usr"])
            for row in conn.execute(
                "SELECT from_usr, to_usr FROM refs WHERE ref_kind = 'call' AND (origins & ?) != 0",
                (int(RefOrigin.SLOT),),
            )
        }
    finally:
        conn.close()


@pytest.fixture
def indexed(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    db_path = _project(root, tmp_path)
    _index(root, db_path)
    return root, db_path


def test_each_unit_keeps_the_facts_of_its_instances_in_the_shared_header(indexed):
    _root, db_path = indexed
    rows = _rows(db_path)
    assert {file for file, *_rest in rows} == {"q.h"}
    assert all(_is_post(slot, "P|") for _file, _line, _function, slot, _unit in rows), rows
    assert sorted(Path(unit).name for *_rest, unit in rows) == ["a.cpp", "b.cpp"]


def test_the_indirect_call_in_post_reaches_the_template_of_the_instances(indexed):
    _root, db_path = indexed
    assert any(_is_post(caller) and target == _header_run(db_path) for caller, target in _edges(db_path))


def test_a_new_parse_of_one_unit_replaces_its_facts(indexed):
    """No pile-up in the header of an other unit, and no loss of the facts of the other unit."""
    root, db_path = indexed
    before = _rows(db_path)
    for name in ("b.cpp", "a.cpp"):
        (root / name).write_text((root / name).read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
        _index(root, db_path)
        assert _rows(db_path) == before, name


# ── review of phase 5d ──────────────────────────────────────────────────────

LOCALS = """\
typedef void (*hook_t)(void);
template <class T> void run() { hook_t local = &T::handler; local(); }
struct A { static void handler() {} };
struct B { static void handler() {} };
void use() { run<A>(); run<B>(); }
"""


def test_a_local_variable_of_an_instance_is_the_variable_of_the_template(tmp_path):
    """One call in the template is one site, also with two instances."""
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "locals.cpp"
    src.write_text(LOCALS, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="cpp", clang_args=["-std=c++17"])
    slots = extract_all(unit, with_refs=True).slots
    sites = [s for s in (*slots.sites, *slots.from_instances.sites) if s.line == 2]
    assert len(sites) == 1 and "@FT@" in sites[0].slot and sites[0].slot.endswith("@local"), sites
    stored = {(s.function, s.slot) for s in slots.from_instances.sources if s.line == 2}
    assert {slot for _function, slot in stored} == {sites[0].slot}
    assert {base_names(function, src).rsplit("@S@", 1)[1] for function, _slot in stored} == {
        "A~locals.cpp:3:8@F@handler#S", "B~locals.cpp:4:8@F@handler#S",
    }


def _conn(tmp_path: Path):
    from fw_context_mcp.indexer.db import open_db

    return open_db(tmp_path / "index.db")


def _site(line: int, slot: str, unit: str) -> tuple:
    return ("cfg", "h.h", line, 5, 0, "c:@F@f#", "pointer", slot, "void ()", "", "", unit)


def _insert_sites(conn, rows) -> None:
    conn.executemany(
        "INSERT INTO slot_sites (config_hash, file, line, col, ordinal, caller, kind, slot, slot_type, reason,"
        " callee, unit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_the_readers_keep_the_index_of_the_unit_off_the_one_copy_check(tmp_path):
    """With no statistics the planner read that index once for each row: 40 000 sites took 51 s."""
    from fw_context_mcp.indexer.db._indirect import ONE_UNIT_SITES

    conn = _conn(tmp_path)
    plan = " ".join(
        row[-1] for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM slot_sites s WHERE s.config_hash = ? AND {ONE_UNIT_SITES}",
            ("cfg",),
        )
    )
    conn.close()
    # The outer scan may read it; the check of each row must not.
    assert "SEARCH u USING INDEX idx_slot_sites_unit" not in plan, plan


def test_one_copy_of_a_site_is_shown_and_the_pages_have_a_total_order(tmp_path):
    from fw_context_mcp.indexer.db._indirect import count_indirect_sites, indirect_sites

    conn = _conn(tmp_path)
    _insert_sites(conn, [
        _site(4, "V|x", ""), _site(4, "V|x", "h|b.cpp"),      # one call, two copies
        _site(4, "V|y", "h|b.cpp"), _site(4, "V|z", "h|c.cpp"),  # two calls, one place and one ordinal
    ])
    assert count_indirect_sites(conn, "cfg", caller="c:@F@f#") == 3
    pages = [
        indirect_sites(conn, "cfg", caller="c:@F@f#", limit=1, offset=offset)[0]["slot"] for offset in range(3)
    ]
    conn.close()
    assert sorted(pages) == ["V|x", "V|y", "V|z"]


def test_the_purge_of_a_file_deletes_the_facts_of_each_unit_in_it(indexed):
    from fw_context_mcp.indexer.db import open_db, purge_file_records

    root, db_path = indexed
    conn = open_db(db_path)
    try:
        row = conn.execute("SELECT id, config_hash FROM files WHERE path LIKE '%q.h'").fetchone()
        purge_file_records(conn, row["config_hash"], row["id"], "q.h", db_dir=db_path.parent)
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM slot_sources WHERE file = 'q.h'").fetchone()[0]
    finally:
        conn.close()
    assert left == 0


def test_the_counts_of_the_resolution_see_one_copy_of_a_site(tmp_path):
    from fw_context_mcp.indexer._slot_resolution import resolve_slot_edges

    conn = _conn(tmp_path)
    _insert_sites(conn, [_site(4, "V|x", ""), _site(4, "V|x", "h|b.cpp"), _site(4, "V|x", "h|c.cpp")])
    counts = resolve_slot_edges(conn, "cfg")
    conn.close()
    assert counts["sites"] == 1
