"""One fact gives one fp_assignments row, and only ``=`` gives an assignment row.

``find_indirect_targets`` showed rows that were equal in every column.
The extraction wrote them:

* Every BINARY_OPERATOR was read as an assignment.  A comparison such as
  ``dev->cb != handler_a`` became a row, and the enclosing ``&&`` wrote
  the same row a second time.
* Each nested CALL_EXPR of ``reg(wrap((void *)&handler))`` found the same
  target and wrote the same row.
* A macro that expands once for each instance gave each expansion the
  same file and line, thus the same row.
"""

from __future__ import annotations

from dataclasses import astuple
from pathlib import Path

import pytest

SOURCE = """\
typedef int (*init_fn_t)(void);
typedef void (*cb_t)(int);

struct device { cb_t cb; };
struct ops { init_fn_t init; };

void handler_a(int v) { (void)v; }
void handler_b(int v) { (void)v; }
static int dev_init(void) { return 0; }

void reg(void *p);
void *wrap(void *p);

#define SET_CB(d, f) ((d)->cb = (f))
#define DEFINE_OPS(n) static struct ops ops_##n = { .init = dev_init };
#define FOR_EACH(fn) fn(0) fn(1)

FOR_EACH(DEFINE_OPS)

int check(struct device *dev) {
    if (dev->cb != handler_a && dev->cb != handler_b) {
        return 1;
    }
    return 0;
}

void setup(struct device *dev) {
    dev->cb = handler_a;
    SET_CB(dev, handler_b);
    reg(wrap((void *)&handler_a));
    dev->cb(1); dev->cb(2);
}
"""


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("fp") / "fp.c"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="c", clang_args=["-std=c11"])
    return extract_all(unit, with_refs=True)


def _line(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE.splitlines(), 1) if text in line)


@pytest.mark.libclang
def test_no_two_rows_are_equal(extracted):
    rows = [astuple(f) for f in extracted.fp_assignments]
    assert len(rows) == len(set(rows)), [r for r in rows if rows.count(r) > 1]


@pytest.mark.libclang
def test_two_calls_on_one_line_stay_two_call_sites(extracted):
    """A call site is an invocation: the dedup is for fp_assignments only."""
    line = _line("dev->cb(1); dev->cb(2);")
    assert len([s for s in extracted.indirect_call_sites if s.from_line == line]) == 2


@pytest.mark.libclang
def test_without_the_c_api_each_binary_operator_assigns_as_before(tmp_path):
    from unittest import mock

    from fw_context_mcp.indexer import symbols
    from fw_context_mcp.indexer.compile_commands import CompilationUnit

    src = tmp_path / "fp.c"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="c", clang_args=["-std=c11"])
    with mock.patch.object(symbols, "_binary_operator_spelling_call", return_value=None):
        rows = symbols.extract_all(unit, with_refs=True).fp_assignments
    line = _line("dev->cb != handler_a")
    assert {f.rhs_name for f in rows if f.from_line == line} == {"handler_a", "handler_b"}


@pytest.mark.libclang
def test_a_comparison_is_not_an_assignment(extracted):
    line = _line("dev->cb != handler_a")
    assert [f for f in extracted.fp_assignments if f.from_line == line] == []


@pytest.mark.libclang
def test_a_comparison_still_gives_its_indirect_references(extracted):
    """The call graph must not change: the functions are still referenced there."""
    line = _line("dev->cb != handler_a")
    targets = {r.to_usr for r in extracted.references
               if r.from_line == line and r.ref_kind == "indirect"}
    assert targets == {"c:@F@handler_a", "c:@F@handler_b"}


@pytest.mark.libclang
def test_an_assignment_gives_one_row(extracted):
    line = _line("dev->cb = handler_a;")
    rows = [f for f in extracted.fp_assignments if f.from_line == line]
    assert [(f.lhs_name, f.rhs_name, f.method) for f in rows] == [("cb", "handler_a", "assignment")]


@pytest.mark.libclang
def test_an_assignment_inside_a_macro_is_kept(extracted):
    """The operator comes from the AST, thus a macro that expands to ``=`` still assigns."""
    line = _line("SET_CB(dev, handler_b);")
    rows = [f for f in extracted.fp_assignments if f.from_line == line]
    assert [(f.lhs_name, f.rhs_name) for f in rows] == [("cb", "handler_b")]


@pytest.mark.libclang
def test_a_macro_that_expands_twice_on_one_line_gives_one_row(extracted):
    line = _line("FOR_EACH(DEFINE_OPS)")
    rows = [f for f in extracted.fp_assignments if f.from_line == line and f.lhs_name == "init"]
    assert len(rows) == 1, rows


@pytest.mark.libclang
def test_nested_calls_that_reach_one_target_give_one_row(extracted):
    line = _line("reg(wrap(")
    rows = [f for f in extracted.fp_assignments if f.from_line == line]
    assert len(rows) == 1, rows
