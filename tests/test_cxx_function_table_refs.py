"""A C++ table of functions gives its indirect references and its slots.

In C++, libclang gives the element of ``{ dev_cb, other }`` as a bare
DECL_REF_EXPR, with no implicit-cast wrapper as in C.  The extraction
looked for the function in the CHILDREN of the element only, thus the
table gave no indirect reference and no slot: ``find_dead_code`` could
report its functions as unused, and ``get_vector_table`` lost the slots.
The form ``{ &dev_cb, &other }`` worked, because the ``&`` holds the
reference as a child.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOURCE = """\
typedef void (*cb_t)(int);
static void dev_cb(int v) { (void)v; }
static void other(int v) { (void)v; }
namespace hal { void irq(int v); }
struct pair { cb_t first; cb_t second; };
static cb_t bare[] = { dev_cb, other };
static cb_t taken[] = { &dev_cb, &other };
static cb_t qualified[] = { hal::irq };
static pair designated = { .first = dev_cb };
"""


@pytest.fixture(scope="module", params=[("cpp", "t.cpp", ["-std=c++20"]), ("c", "t.c", ["-std=gnu11"])],
                ids=["c++", "c"])
def refs(request, tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    language, name, args = request.param
    text = SOURCE
    if language == "c":
        text = text.replace("namespace hal { void irq(int v); }", "void irq(int v);")
        text = text.replace("hal::irq", "irq").replace("static pair", "static struct pair")
    src: Path = tmp_path_factory.mktemp("cxxtable") / name
    src.write_text(text, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language=language, clang_args=args)
    result = extract_all(unit, with_refs=True)
    return text, [r for r in result.references if r.ref_kind == "indirect"]


def _slots(refs, text: str) -> list[tuple[str, int | None]]:
    source, rows = refs
    line = next(i for i, row in enumerate(source.splitlines(), 1) if text in row)
    return sorted((r.to_usr.split("@F@")[-1].split("#")[0], r.slot_index) for r in rows if r.from_line == line)


@pytest.mark.libclang
def test_a_bare_function_name_gives_its_slot(refs):
    assert _slots(refs, "bare[]") == [("dev_cb", 0), ("other", 1)]


@pytest.mark.libclang
def test_an_address_gives_the_same_slot(refs):
    assert _slots(refs, "taken[]") == [("dev_cb", 0), ("other", 1)]


@pytest.mark.libclang
def test_a_qualified_name_gives_its_slot(refs):
    assert _slots(refs, "qualified[]") == [("irq", 0)]


@pytest.mark.libclang
def test_a_designated_field_gives_its_reference(refs):
    assert _slots(refs, "designated") == [("dev_cb", None)]
