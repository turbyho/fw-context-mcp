"""The virtual methods that each method overrides, from the record of the compiler.

WHY this module exists: the table ``overrides`` came from a match of names
and of the text of the signatures (the post-processing of an index run).  A
text cannot tell the types: an override that writes a parameter through an
alias (`unsigned int` for `uint32_t`, a typedef of the pointer to a
function) had no row, and an override of a method of a class template
(`struct Impl : IQueue<cb_t>`) had none, because the table of inheritance
holds no template.  The resolution of the slots gives the argument of a
virtual call to the parameters of each override (``_slot_resolution``),
thus a missing row lost the call.

``clang_getOverriddenCursors`` is the answer of the compiler.  It gives the
methods that a method overrides directly; the module follows them to each
ancestor, as the table held each one.

Limit: a method of a class template whose base depends on a parameter
(`template <class T> struct Mixin : IQueue<T>`) overrides nothing that the
compiler can name in the template, and the walk of a unit does not visit
the implicit instances of the class.  Such a method has no row.

The python bindings of libclang do not give the function, thus the module
binds it with ``ctypes``, as ``skipped_ranges`` does.  The function is in
libclang from version 2.9, and the project pins libclang 22.  The cursors
of the answer get the translation unit of the method through
``Cursor.from_result``: the binding registers no errcheck for a function
it does not know, and a cursor with no unit raised in each use.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Iterable
from functools import cache

from ._libclang import cindex as cx

__all__ = ["VIRTUAL_METHOD_KINDS", "extract_overrides"]

# The kinds of a method that can override and that the index keeps as a
# symbol: a destructor is virtual too.  A virtual conversion function
# (`virtual operator bool()`) is no symbol of the index (``symbols.
# _SYMBOL_KINDS``), thus a row for it would name no symbol.
VIRTUAL_METHOD_KINDS = frozenset({cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR})


@cache
def _functions() -> tuple[Callable[..., None], Callable[..., None]]:
    """Give ``(clang_getOverriddenCursors, clang_disposeOverriddenCursors)`` with their types.

    WHY a cache: the types are written on the shared library object of the
    bindings, and the work is the same each time (see ``skipped_ranges``).
    """
    lib = cx.conf.lib
    get = lib.clang_getOverriddenCursors
    get.argtypes = [cx.Cursor, ctypes.POINTER(ctypes.POINTER(cx.Cursor)), ctypes.POINTER(ctypes.c_uint)]
    get.restype = None
    dispose = lib.clang_disposeOverriddenCursors
    dispose.argtypes = [ctypes.POINTER(cx.Cursor)]
    dispose.restype = None
    return get, dispose


def _overridden(method: cx.Cursor) -> list[cx.Cursor]:
    """The methods that *method* overrides directly.

    A null cursor of the answer is left out: ``Cursor.from_result`` gives
    None for it, and no method is there.
    """
    get, dispose = _functions()
    array = ctypes.POINTER(cx.Cursor)()
    count = ctypes.c_uint(0)
    get(method, ctypes.byref(array), ctypes.byref(count))
    if not count.value:
        return []
    try:
        cursors = (cx.Cursor.from_result(cx.Cursor.from_buffer_copy(array[i]), method) for i in range(count.value))
        return [cursor for cursor in cursors if cursor is not None]
    finally:
        dispose(array)


def extract_overrides(
    methods: Iterable[cx.Cursor], code_usr: Callable[[cx.Cursor], str],
) -> list[tuple[str, str]]:
    """Give ``(derived USR, base USR)`` for each method of *methods* and each virtual method that it overrides.

    Each ancestor gets a row, not only the direct one: `C::f` overrides
    `B::f` and through it `A::f`, and a reader of the table asks for any
    of them.  A method that a class declares and a file defines out of the
    class is one USR, thus one row.

    *code_usr* gives the key of a method (``symbols._code_usr``).  WHY: the
    compiler gives the member of an implicit instance as the base of
    `struct Impl : IQueue<cb_t>`, which is no symbol of the index, and the
    slots and the call edges name the member of the template.  The walk
    follows the cursors of the compiler, not the keys: two instances of
    one template have one key, but each has its own ancestors.
    """
    pairs: set[tuple[str, str]] = set()
    for method in methods:
        derived = code_usr(method)
        if not derived:
            continue
        seen: set[str] = set()
        work = _overridden(method)
        while work:
            base = work.pop()
            usr = base.get_usr()
            if not usr or usr in seen:
                continue
            seen.add(usr)
            base_usr = code_usr(base)
            if base_usr and base_usr != derived:
                pairs.add((derived, base_usr))
            work.extend(_overridden(base))
    return sorted(pairs)
