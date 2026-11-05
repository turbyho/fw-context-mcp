"""An element of an init list fills the field that it names, and only that one.

The slot of an element is the field that the element fills (``_slots.py``,
``climb_init_list``):

* ``.ops = { .init = dev_init }`` fills ``init``, not ``ops``, also when
  the value is a compound literal ``(struct ops){ ... }`` or
  ``&(struct ops){ ... }``.
* ``[1] = { .cb = dev_cb }`` and ``{ .init = dev_init }`` inside an array
  fill the field of the struct.
* ``.ops.init = dev_init`` fills ``init``.
* ``.inner.cb = dev_cb`` fills the last ``cb`` of the chain, and
  ``.handlers[2] = dev_cb`` the array.
* A scalar in braces (``.one = { dev_cb }``, ``(cb_t){ dev_cb }``) is its
  one element.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._usr_keys import base_names

SOURCE = """\
typedef int (*init_fn_t)(void);
typedef void (*cb_t)(int);
struct ops { init_fn_t init; cb_t cb; };
struct device { const char *name; struct ops ops; };
struct holder { struct ops *pops; struct ops ops; };
struct inner { cb_t cb; };
struct outer { struct inner cb; cb_t handlers[4]; };
static int dev_init(void) { return 0; }
static void dev_cb(int v) { (void)v; }
static struct device nested = { .name = "a", .ops = { .init = dev_init, .cb = dev_cb } };
static const struct ops table[] = { { dev_init, dev_cb }, { .init = dev_init } };
static struct device chained = { .ops.init = dev_init, .ops.cb = dev_cb };
static struct ops indexed[3] = { [1] = { .cb = dev_cb } };
static struct holder literal = { .ops = (struct ops){ .init = dev_init } };
static struct holder pointed = { .pops = &(struct ops){ .cb = dev_cb } };
static struct ops *pointers[] = { &(struct ops){ .init = dev_init } };
static struct outer same_name = { .cb.cb = dev_cb };
static struct outer element = { .handlers[2] = dev_cb };
struct grid { cb_t h[2][3]; cb_t one; };
static struct outer braced_array = { .handlers = { dev_cb, dev_cb } };
static struct grid deep = { .h[1][2] = dev_cb };
static struct grid deep_braced = { .h[1] = { dev_cb } };
static struct grid scalar_braced = { .one = { dev_cb } };
static struct grid scalar_literal = { .one = (cb_t){ dev_cb } };
typedef cb_t cb_arr_t[4];
struct typed { cb_arr_t h; };
static struct typed typedef_braced = { .h = { dev_cb } };
static struct typed typedef_indexed = { .h[1] = dev_cb };
"""


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("initlist") / "init.c"
    src.write_text(SOURCE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="c", clang_args=["-std=gnu11"])
    return base_names(extract_all(unit, with_refs=True), src)


def _line(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE.splitlines(), 1) if text in line)


def _struct(record: str) -> str:
    """The name of the struct of the USR component *record*, after a check of its mark.

    The USR of a type holds the place of its definition (`ops~init.c:3:8`).
    """
    name = record.split("~", 1)[0]
    row = next(i for i, row in enumerate(SOURCE.splitlines(), 1) if row.startswith(f"struct {name} {{"))
    assert record == f"{name}~init.c:{row}:8", record
    return name


def _sources(extracted, text: str) -> set[tuple[str, str]]:
    """(function, struct::field) of each slot fact on the line of *text*."""
    line = _line(text)
    found = set()
    for source in extracted.slots.sources:
        if source.line == line:
            record, _, field = source.slot.removeprefix("F|c:@S@").partition("@FI@")
            found.add((source.function.rsplit("@", 1)[-1], f"{_struct(record)}::{field}"))
    return found


INIT = ("dev_init", "ops::init")
CB = ("dev_cb", "ops::cb")


@pytest.mark.libclang
def test_a_nested_list_fills_its_own_fields_only(extracted):
    assert _sources(extracted, "static struct device nested") == {CB, INIT}


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "fact"), [
    ("static struct holder literal", INIT),
    ("static struct holder pointed", CB),
    ("*pointers[]", INIT),
])
def test_a_compound_literal_fills_its_own_fields_only(extracted, text, fact):
    assert _sources(extracted, text) == {fact}


@pytest.mark.libclang
def test_a_struct_in_an_array_fills_its_field(extracted):
    # { dev_init, dev_cb } fills its two fields by position, { .init = ... } the same init.
    assert _sources(extracted, "table[]") == {CB, INIT}
    assert _sources(extracted, "indexed[3]") == {CB}


@pytest.mark.libclang
def test_a_chain_of_designators_fills_its_last_field(extracted):
    assert _sources(extracted, "static struct device chained") == {CB, INIT}


@pytest.mark.libclang
def test_a_chain_fills_the_last_field_of_the_name(extracted):
    """``.cb.cb``: the first ``cb`` is a struct, the last one the pointer."""
    assert _sources(extracted, "same_name") == {("dev_cb", "inner::cb")}


@pytest.mark.libclang
def test_an_index_after_the_field_fills_the_array(extracted):
    assert _sources(extracted, "static struct outer element") == {("dev_cb", "outer::handlers")}


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "field"), [
    ("braced_array", "outer::handlers"),
    ("static struct grid deep =", "grid::h"),
    ("deep_braced", "grid::h"),
    ("scalar_braced", "grid::one"),
    ("scalar_literal", "grid::one"),
    ("typedef_braced", "typed::h"),
    ("typedef_indexed", "typed::h"),
])
def test_a_brace_list_that_is_no_struct_still_fills_the_field(extracted, text, field):
    """Only a struct gives its own fields: an array or a scalar in braces is this field."""
    assert _sources(extracted, text) == {("dev_cb", field)}


@pytest.mark.libclang
def test_the_slots_of_the_array_keep_their_references(extracted):
    """The outer array list knows the slot: the vector table keeps its slots."""
    line = _line("table[]")
    slots = sorted((r.to_usr.rsplit("@", 1)[-1], r.slot_index) for r in extracted.references
                   if r.from_line == line and r.ref_kind == "indirect")
    assert slots == [("dev_cb", 0), ("dev_init", 0), ("dev_init", 1)]
