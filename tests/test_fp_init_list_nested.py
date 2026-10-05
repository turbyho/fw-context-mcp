"""An element of an init list assigns the field that it names, and only that one.

The extraction read the field of an element with ``_extract_lhs_field``,
which takes the first field reference anywhere in the element:

* ``.ops = { .init = dev_init }`` wrote ``ops = dev_init`` next to the
  correct ``init = dev_init``, also when the value was a compound literal
  ``(struct ops){ ... }`` or ``&(struct ops){ ... }``.
* ``[1] = { .cb = dev_cb }`` and ``{ .init = dev_init }`` inside an array
  wrote the field twice, once with the type of the struct.
* ``.ops.init = dev_init`` wrote ``ops = dev_init``, and no ``init`` row.
* ``.inner.cb = dev_cb`` took its type from the first ``cb`` along the
  chain, and ``.handlers[2] = dev_cb`` the type of the whole array.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
    return extract_all(unit, with_refs=True)


def _line(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE.splitlines(), 1) if text in line)


def _rows(extracted, text: str) -> list[tuple[str, str, str]]:
    line = _line(text)
    return sorted((f.lhs_name, f.rhs_name, f.fn_ptr_type)
                  for f in extracted.fp_assignments if f.from_line == line)


@pytest.mark.libclang
def test_a_nested_list_gives_its_own_fields_only(extracted):
    assert _rows(extracted, "static struct device nested") == [
        ("cb", "dev_cb", "cb_t"), ("init", "dev_init", "init_fn_t"),
    ]


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "row"), [
    ("static struct holder literal", ("init", "dev_init", "init_fn_t")),
    ("static struct holder pointed", ("cb", "dev_cb", "cb_t")),
    ("*pointers[]", ("init", "dev_init", "init_fn_t")),
])
def test_a_compound_literal_gives_its_own_fields_only(extracted, text, row):
    assert _rows(extracted, text) == [row]


@pytest.mark.libclang
def test_a_struct_in_an_array_gives_its_field_once(extracted):
    # { dev_init, dev_cb } gives its two fields by position, { .init = ... } the same init.
    assert _rows(extracted, "table[]") == [("cb", "dev_cb", "cb_t"), ("init", "dev_init", "init_fn_t")]
    assert _rows(extracted, "indexed[3]") == [("cb", "dev_cb", "cb_t")]


@pytest.mark.libclang
def test_a_chain_of_designators_assigns_its_last_field(extracted):
    assert _rows(extracted, "static struct device chained") == [
        ("cb", "dev_cb", "cb_t"), ("init", "dev_init", "init_fn_t"),
    ]


@pytest.mark.libclang
def test_a_chain_takes_the_type_of_its_last_field(extracted):
    """``.cb.cb``: the first ``cb`` is a struct, the last one the pointer."""
    assert _rows(extracted, "same_name") == [("cb", "dev_cb", "cb_t")]


@pytest.mark.libclang
def test_an_index_after_the_field_gives_the_type_of_one_element(extracted):
    assert _rows(extracted, "static struct outer element") == [("handlers", "dev_cb", "cb_t")]


@pytest.mark.libclang
@pytest.mark.parametrize(("text", "field"), [
    ("braced_array", "handlers"),
    ("static struct grid deep =", "h"),
    ("deep_braced", "h"),
    ("scalar_braced", "one"),
    ("scalar_literal", "one"),
    ("typedef_braced", "h"),
    ("typedef_indexed", "h"),
])
def test_a_brace_list_that_is_no_struct_still_gives_the_field(extracted, text, field):
    """Only a struct gives its own fields: an array or a scalar in braces is this field."""
    rows = _rows(extracted, text)
    assert rows and {r[:2] for r in rows} == {(field, "dev_cb")}, rows
    assert {r[2] for r in rows} == {"cb_t"}, "the type of one element, every array level off"


@pytest.mark.libclang
def test_the_slots_of_the_array_keep_their_references(extracted):
    """The outer array list knows the slot: the fix must not cost the vector table its slots."""
    line = _line("table[]")
    slots = sorted((r.to_usr.rsplit("@", 1)[-1], r.slot_index) for r in extracted.references
                   if r.from_line == line and r.ref_kind == "indirect")
    assert slots == [("dev_cb", 0), ("dev_init", 0), ("dev_init", 1)]
