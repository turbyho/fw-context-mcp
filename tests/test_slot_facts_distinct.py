"""One fact gives one slot fact, and only ``=`` puts a function into a slot.

``find_indirect_targets`` showed rows that were equal in every column.
Three cases wrote them:

* A comparison such as ``dev->cb != handler_a`` is no store, and the
  enclosing ``&&`` must not give a second copy.
* Each nested CALL_EXPR of ``reg(wrap((void *)&handler))`` reaches the same
  function, which is one fact.
* A macro that expands once for each instance gives each expansion the
  same file and line, thus the same fact.
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


# The slot of the field ``cb`` of ``struct device``.
CB = "F|c:@S@device@FI@cb"


def _line(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE.splitlines(), 1) if text in line)


def _sources(extracted, line: int) -> list[tuple[str, str]]:
    return [(s.function, s.slot) for s in extracted.slots.sources if s.line == line]


@pytest.mark.libclang
@pytest.mark.parametrize("facts", ["sources", "flows", "arguments", "escapes"])
def test_no_two_facts_are_equal(extracted, facts):
    rows = [astuple(f) for f in getattr(extracted.slots, facts)]
    assert len(rows) == len(set(rows)), [r for r in rows if rows.count(r) > 1]


@pytest.mark.libclang
def test_two_calls_on_one_line_stay_two_call_sites(extracted):
    """A call site is an invocation: the dedup is not for the sites."""
    line = _line("dev->cb(1); dev->cb(2);")
    assert len([s for s in extracted.slots.sites if s.line == line]) == 2


def test_the_binding_reads_the_operator_of_a_binary_expression():
    """The fix rests on ``Cursor.binary_operator`` (binding of libclang 19 and later).

    The ctypes code that it replaced broke under the libclang 22 binding,
    and a broad ``except`` made each operator an assignment without a word.
    """
    from fw_context_mcp.indexer._libclang import cindex

    assert isinstance(getattr(cindex.Cursor, "binary_operator", None), property)


@pytest.mark.libclang
def test_a_comparison_is_not_an_assignment(extracted):
    """The address is taken, and it goes into no slot."""
    line = _line("dev->cb != handler_a")
    assert sorted(_sources(extracted, line)) == [("c:@F@handler_a", ""), ("c:@F@handler_b", "")]


@pytest.mark.libclang
def test_a_comparison_still_gives_its_indirect_references(extracted):
    """The call graph must not change: the functions are still referenced there."""
    line = _line("dev->cb != handler_a")
    targets = {r.to_usr for r in extracted.references
               if r.from_line == line and r.ref_kind == "indirect"}
    assert targets == {"c:@F@handler_a", "c:@F@handler_b"}


@pytest.mark.libclang
def test_an_assignment_gives_one_fact(extracted):
    line = _line("dev->cb = handler_a;")
    assert _sources(extracted, line) == [("c:@F@handler_a", CB)]


@pytest.mark.libclang
def test_an_assignment_inside_a_macro_is_kept(extracted):
    """The operator comes from the AST, thus a macro that expands to ``=`` still assigns."""
    line = _line("SET_CB(dev, handler_b);")
    assert _sources(extracted, line) == [("c:@F@handler_b", CB)]


@pytest.mark.libclang
def test_a_macro_that_expands_twice_on_one_line_gives_one_fact(extracted):
    line = _line("FOR_EACH(DEFINE_OPS)")
    assert [slot for _, slot in _sources(extracted, line)] == ["F|c:@S@ops@FI@init"]


@pytest.mark.libclang
def test_nested_calls_that_reach_one_target_give_one_fact(extracted):
    line = _line("reg(wrap(")
    # `wrap(void *)`: the parameter holds the function.
    assert _sources(extracted, line) == [("c:@F@handler_a", "P|c:@F@wrap|0")]
