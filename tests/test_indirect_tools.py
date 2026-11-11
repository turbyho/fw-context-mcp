"""find_indirect_call_sites and find_indirect_targets read the slot facts and their resolution.

The project is indexed with the full pipeline, thus the answer comes from
the walk, the step slot_edges and the tool, as it does for a user.  No
answer matches a name: the slot of a field and the code key of a function
are USRs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._paging import assert_whole_and_once, notice, walk_pages, with_project
from tests._toml import toml_string

pytest.importorskip("clang.cindex", reason="libclang not available")
pytestmark = pytest.mark.libclang

PROJECT_ID = "proj-indirect-tools"
SOURCE = """\
typedef void (*cb_t)(int);
struct drv { cb_t on_data; };
static struct drv g_drv;
void set_cb(cb_t h) { g_drv.on_data = h; }
static void rx(int v) { (void)v; }
static void tx(int v) { (void)v; }
void init(void) { set_cb(rx); set_cb(tx); }
void fire(int v) {
    g_drv.on_data(v);
    g_drv.on_data(v + 1);
    cb_t copy = g_drv.on_data;
    copy(v);
}
typedef void (*entry_t)(void);
#define APP_RESET (*(unsigned int *)0x08004004)
void boot(void) { ((entry_t)APP_RESET)(); }
typedef void (*hook_t)(long);
void hal_register(hook_t *slot);
static hook_t hal_cb;
void hal_init(void) { hal_register(&hal_cb); }
void hal_irq(void) { hal_cb(7); }
static cb_t ev_slot;
void set1(cb_t h) { ev_slot = h; }
void set2(cb_t h) { ev_slot = h; }
static void ev(int v) { (void)v; }
void ev_init(void) {
    set1(ev);
    set2(ev);
}
static cb_t a_slot, b_slot;
static void do_reg_a(cb_t h) { a_slot = h; }
static void do_reg_b(cb_t h) { b_slot = h; }
static void hx(int v) { (void)v; }
void regs(void (*ra)(cb_t), void (*rb)(cb_t)) {
    ra(hx);
    rb(hx);
    rb(hx);
}
void wire(void) { regs(do_reg_a, do_reg_b); }
void schedule(cb_t h);
static void blink(int v) { schedule(blink); (void)v; }
static cb_t c_slot, d_slot;
static void do_reg_c(cb_t h) { c_slot = h; }
static void do_reg_d(cb_t h) { d_slot = h; }
static void hy(int v) { (void)v; }
#define BOTH(x) do { pc(0); pd(x); } while (0)
void regs2(void (*pc)(cb_t), void (*pd)(cb_t)) {
    pc(hy);
    BOTH(hy);
}
void wire2(void) { regs2(do_reg_c, do_reg_d); }
"""
CPP_SOURCE = """\
typedef void (*cb_t)(int);
static cb_t g_cb;
void with_lambda() {
    g_cb = [](int v) { (void)v; };
}
"""


def _index(root: Path, base: Path, project_id: str, name: str, source: str, compiler: str, std: str) -> None:
    from fw_context_mcp.indexer.runner import run

    (root / ".fw-context").mkdir(parents=True)
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{project_id}"\n\n[build]\n\n[index]\ndb_dir = {toml_string(base)}\n', encoding="utf-8",
    )
    (root / name).write_text(source, encoding="utf-8")
    (root / "compile_commands.json").write_text(json.dumps([{
        "directory": str(root), "file": name,
        "arguments": [compiler, std, "-c", name, "-o", name + ".o"],
    }]), encoding="utf-8")
    db_path = base / project_id / "index.db"
    db_path.parent.mkdir(parents=True)
    run(
        compile_commands=root / "compile_commands.json", db_path=db_path, project_root=root,
        project_id=project_id, project_name=project_id, index_refs=True, index_embeddings=False,
        analyze_symbols=False, record_git=False,
    )


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("indirect")
    root = base / "proj"
    _index(root, base, PROJECT_ID, "main.c", SOURCE, "cc", "-std=gnu11")
    return root


@pytest.fixture(scope="module")
def cpp_project(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("indirect_cpp")
    root = base / "proj"
    _index(root, base, PROJECT_ID + "-cpp", "main.cpp", CPP_SOURCE, "c++", "-std=c++17")
    return root


def _rows(answer: list[dict]) -> list[dict]:
    return [r for r in answer if "total" not in r and "warning" not in r]


def _call_sites(project: Path, name: str, **kwargs) -> list[dict]:
    from fw_context_mcp.mcp.handlers.callgraph import find_indirect_call_sites

    return find_indirect_call_sites(name, project_root=str(project), **kwargs)


def _targets(project: Path, name: str, **kwargs) -> list[dict]:
    from fw_context_mcp.mcp.handlers.callgraph import find_indirect_targets

    return find_indirect_targets(name, project_root=str(project), **kwargs)


class TestCallSites:
    def test_the_calls_through_a_field_and_through_a_copy_of_it(self, project):
        rows = _rows(_call_sites(project, "on_data"))
        assert [(r["line"], r["caller"], r["kind"]) for r in rows] == [
            (9, "fire", "pointer"), (10, "fire", "pointer"), (12, "fire", "pointer"),
        ]
        assert rows[0]["slot"] == "drv::on_data"
        assert rows[0]["type"] == "void (int)"
        for row in rows:
            assert row["targets_total"] == 2
            assert row["targets"] == [{"name": "rx", "origin": "slot"}, {"name": "tx", "origin": "slot"}]

    def test_the_indirect_calls_that_a_function_makes(self, project):
        rows = _rows(_call_sites(project, "fire"))
        assert [r["line"] for r in rows] == [9, 10, 12]

    def test_a_jump_to_an_address_names_the_address_and_no_target(self, project):
        rows = _rows(_call_sites(project, "boot"))
        assert len(rows) == 1
        assert rows[0]["kind"] == "address"
        assert rows[0]["address"] == "APP_RESET"
        assert rows[0]["targets"] == [] and rows[0]["targets_total"] == 0

    def test_the_pages_walk_whole_and_once(self, project):
        seen, total = walk_pages(
            lambda offset: _call_sites(project, "on_data", limit=1, offset=offset),
            lambda r: (r["line"], r["col"]),
        )
        assert total == 3
        assert_whole_and_once(seen, total)

    def test_the_hint_names_the_next_call(self, project):
        assert notice(_call_sites(project, "on_data", limit=1))["hint"] == with_project(
            "find_indirect_call_sites('on_data', offset=1) reads the next page.", project,
        )

    def test_a_page_after_the_end_names_the_total(self, project):
        assert _call_sites(project, "on_data", offset=900) == [
            {"info": "No indirect call site at offset 900; the answer holds 3."},
        ]

    def test_a_kind_with_no_indirect_call_says_so(self, project):
        answer = _call_sites(project, "drv")
        assert len(answer) == 1 and "name a function pointer field or variable" in answer[0]["info"]


class TestSymbolContext:
    def test_a_field_shows_its_call_sites_and_its_targets(self, project):
        from fw_context_mcp.mcp.handlers.source import get_symbol_context

        answer = get_symbol_context("on_data", project_root=str(project))
        assert [r["line"] for r in answer["indirect_call_sites"]] == [9, 10, 12]
        assert answer["indirect_call_sites_total"] == 3
        assert [r["function"] for r in answer["indirect_targets"]] == ["rx", "tx"]
        assert answer["indirect_targets_total"] == 2
        assert "slot_escapes" not in answer
        assert "resolution" not in answer

    def test_a_function_shows_the_indirect_calls_it_makes(self, project):
        from fw_context_mcp.mcp.handlers.source import get_symbol_context

        answer = get_symbol_context("fire", project_root=str(project))
        assert [r["line"] for r in answer["indirect_call_sites"]] == [9, 10, 12]
        assert "indirect_targets" not in answer


class TestTargets:
    def test_the_functions_of_a_field_and_where_the_code_stored_them(self, project):
        rows = _rows(_targets(project, "on_data"))
        assert [(r["function"], r["origin"]) for r in rows] == [("rx", "slot"), ("tx", "slot")]
        for row in rows:
            assert row["stored_in"] == ["parameter 0 of set_cb"]
            assert row["stored_line"] == 7
            assert row["line"] in (5, 6)

    def test_a_slot_that_code_out_of_view_fills_says_so(self, project):
        """`hal_register(&hal_cb)`: no code of the build stores a function, and the slot escapes."""
        answer = _targets(project, "hal_cb")
        assert len(answer) == 2
        assert "Code out of view can write" in answer[0]["warning"]
        assert answer[1]["info"].startswith("No code of the build stores a function into 'hal_cb'")

    def test_each_way_to_the_slot_gives_its_place(self, project):
        """`set1(ev); set2(ev);`: two parameters flow into one variable."""
        (row,) = _rows(_targets(project, "ev_slot"))
        assert row["function"] == "ev"
        assert row["stored_places"] == 2
        assert row["stored_line"] == 27
        assert row["stored_in"] == ["parameter 0 of set1", "parameter 0 of set2"]

    def test_an_argument_of_an_indirect_call_is_stored_only_for_its_targets(self, project):
        """`ra(hx)` reaches `do_reg_a` and `rb(hx)` reaches `do_reg_b`: each slot has its own places."""
        (a_row,) = _rows(_targets(project, "a_slot"))
        (b_row,) = _rows(_targets(project, "b_slot"))
        assert (a_row["stored_line"], a_row["stored_places"]) == (35, 1)
        assert (b_row["stored_line"], b_row["stored_places"]) == (36, 2)

    def test_two_calls_of_one_macro_expansion_keep_their_own_targets(self, project):
        """`BOTH(hy)` calls `pc(0)` and `pd(hy)` at one place: `hy` goes to the target of `pd` only."""
        (c_row,) = _rows(_targets(project, "c_slot"))
        (d_row,) = _rows(_targets(project, "d_slot"))
        assert (c_row["stored_line"], c_row["stored_places"]) == (48, 1)
        assert (d_row["stored_line"], d_row["stored_places"]) == (49, 1)

    def test_a_function_that_stores_itself_is_its_own_context(self, project):
        """`schedule(blink)` in `blink`: a timer that re-arms itself."""
        rows = _rows(_targets(project, "blink"))
        assert [(r["relation"], r["line"], r["context"]) for r in rows] == [("stored", 41, "blink")]

    def test_the_code_that_stores_a_lambda_is_its_owner(self, cpp_project):
        rows = _rows(_targets(cpp_project, "with_lambda::<lambda>"))
        stored = [r for r in rows if r["relation"] == "stored"]
        assert [(r["line"], r["context"]) for r in stored] == [(4, "with_lambda")]

    def test_where_the_code_stores_a_function_and_the_calls_that_reach_it(self, project):
        rows = _rows(_targets(project, "rx"))
        stored = [r for r in rows if r["relation"] == "stored"]
        called = [r for r in rows if r["relation"] == "called"]
        assert [(r["line"], r["slot"], r["context"]) for r in stored] == [(7, "parameter 0 of set_cb", "init")]
        assert [(r["line"], r["caller"], r["origin"]) for r in called] == [
            (9, "fire", "slot"), (10, "fire", "slot"), (12, "fire", "slot"),
        ]

    def test_the_pages_walk_whole_and_once(self, project):
        seen, total = walk_pages(
            lambda offset: _targets(project, "rx", limit=1, offset=offset),
            lambda r: (r["relation"], r["line"], r["col"]),
        )
        assert total == 4
        assert_whole_and_once(seen, total)
