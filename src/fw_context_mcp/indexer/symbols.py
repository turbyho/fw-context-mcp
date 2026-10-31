"""Extract symbols, references, inheritance edges, and macros from a CompilationUnit using libclang.

This module is the single-pass extraction engine of the fw-context indexer.
For each translation unit it produces:

* **Symbols** — functions, methods, classes, enums, variables, fields,
  namespaces.  Variable declarations are split into ``varglobal`` (file/class
  scope) and ``varlocal`` (inside function bodies) based on semantic parent.
* **References** — direct calls, reads, member accesses, indirect
  (function-pointer) edges, and implicit constructor invocations.
* **Inheritance** — C++ base-class edges with access specifier and virtual
  flag for each class/struct definition.
* **Macros** — ``#define`` name, value, and function-like flag.
* **Slot facts** — the indirect calls and where the functions that they
  reach are stored (``_slots.py``); the step ``slot_edges`` of the
  post-processing resolves them into call edges.

**Key design decisions:**

* **Single TU walk** — ``extract_all`` parses once and walks the AST once.
  Symbols and references are extracted in separate sub-walks over the same
  cursor stream, trading memory for speed (no re-parse).

* **Skip-files optimisation** — when a header was already visited by an
  earlier TU, its subtree is skipped during subsequent walks.
  ``_iter_cursor_skip`` uses a manual stack (instead of native
  ``walk_preorder``) to support subtree omission.  The native method is ~3×
  faster and is used when *skip_files* is empty.

* **Function pointers** — the walk gives an ``indirect`` reference for each
  function whose address the code takes (``_emit_fn_ptr_targets``,
  ``_handle_fn_ptr_as_argument``).  What runs a function that an SDK
  registers is a rule of the SDK model (``_sdk_model.py``), which the
  post-processing applies to the slot facts.

* **No name resolution** — each reference comes from a cursor that names
  its target.  A scan of the tokens and of the source text once took the
  target from a name (``&Class::method``); it read comments and the return
  type of a definition (``Gap &Gap::onShutdown()``), and its true finds were
  addresses in template bodies, which the slot facts of the template
  instances now give (``_slots.collect_slot_facts``).

* **Declaration-to-definition promotion** — ``_process_one_symbol`` tracks
  ``seen_usrs`` as ``USR → bool(definition)``.  A forward declaration is
  recorded as ``False``; when the definition is later encountered in the
  same TU, the flag is promoted to ``True`` — the definition wins.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from ._libclang import cindex as cx
from ._overrides import VIRTUAL_METHOD_KINDS, extract_overrides
from ._usr import cursor_usr, file_key
from .compile_commands import CompilationUnit
from .models import (
    InheritanceRecord,
    InternalDecl,
    Macro,
    Reference,
    RefOrigin,
    Symbol,
)
from .nvic import runtime_vector_reference

if TYPE_CHECKING:
    # `_slots` imports this module: the walk of the slots uses its helpers.
    from ._slots import SlotFacts

_log = logging.getLogger(__name__)

_INDEX: cx.Index | None = None
#: The resource directory of the libclang-ng wheel, read at import, before
#: ``_get_index`` replaces it (see there).
_WHEEL_RESOURCE_DIR = cx.Config.resource_dir
_index_lock = threading.Lock()


def _get_index() -> cx.Index:
    """Return the libclang Index singleton.

    ``cx.Index.create()`` is lightweight and Index objects internally
    serialize concurrent ``parse()`` calls, so a single shared instance
    is sufficient.

    At each call, ``Config.resource_dir`` gets the directory of the compiler
    headers that ``_clang_resource`` finds.  libclang-ng adds it as
    ``-resource-dir`` to each parse that has none: a unit without a GCC
    driver then gets clang's ``stddef.h``.  WHY not the directory of the
    wheel: it lacks the generated ARM and RISC-V intrinsic headers.  WHY at
    each call and not once: the GCC driver path looks the headers up for
    each unit (``_driver_query.clang_resource_include``), and the two paths
    must give one unit the same headers, also after a copy that a long MCP
    process saw removed or unpacked later.  When the lookup finds nothing,
    the wheel directory serves: its headers are of the right version, and
    fewer headers are better than none.  Each parse of a unit goes through
    this function (``asm``, ``manifest`` and ``ops`` call it too); only the
    target probe of ``_driver_query`` parses an empty file on its own.
    """
    global _INDEX
    from ._clang_resource import find_resource_include

    found = find_resource_include()
    cx.Config.resource_dir = str(found.include.parent) if found is not None else _WHEEL_RESOURCE_DIR
    if _INDEX is None:
        with _index_lock:
            if _INDEX is None:
                _INDEX = cx.Index.create()
    return _INDEX


def _iter_cursor_skip(
    root_cursor: cx.Cursor,
    skip_files: frozenset[str],
    resolve_fn: Callable[[str], Path],
) -> Generator[cx.Cursor, None, None]:
    """Pre-order AST walk that skips subtrees of already processed source files.

    When a cursor's source file is in *skip_files*, the cursor and its
    entire subtree are omitted from the traversal — children are never
    pushed onto the stack.

    Uses a manual stack instead of the native ``walk_preorder``, which
    does not support subtree skipping.  The native method is about 3×
    faster (implemented in C) and is used when *skip_files* is empty.

    Each yielded cursor is guaranteed NOT to originate from a file in
    *skip_files*, so the caller does not need to re-check.

    Args:
        root_cursor: The top-level ``TRANSLATION_UNIT`` cursor.
        skip_files: Frozen set of resolved file paths to skip.
            A frozen set avoids accidental mutation across call sites.
        resolve_fn: Path resolver (typically an ``@lru_cache``-decorated
            local function in ``extract_all``).  Each call resolves a
            libclang file name string to a canonical ``Path``.

    Yields:
        Libclang cursors in pre-order, skipping any whose source file
        is in *skip_files*.
    """
    stack: list[cx.Cursor] = [root_cursor]
    while stack:
        c = stack.pop()
        loc = c.location
        if loc.file:
            fname = str(resolve_fn(str(loc.file.name)))
            if fname in skip_files:
                continue  # omit cursor + subtree

        yield c

        # Push children in reverse order for pre-order semantics.
        # Materializing into a list is necessary — get_children()
        # returns a lazy iterable, and reversed() needs a sequence.
        children = list(c.get_children())
        for child in reversed(children):
            stack.append(child)


# Frozen set for O(1) containment check on cursor kind during the symbol
# walk.  A frozenset is hashable (can be used as a dict key) and immutable
# (no accidental mutation in a long-running indexing process).
_SYMBOL_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.CLASS_DECL,
    cx.CursorKind.CLASS_TEMPLATE,
    cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    cx.CursorKind.STRUCT_DECL,
    cx.CursorKind.UNION_DECL,
    cx.CursorKind.ENUM_DECL,
    cx.CursorKind.ENUM_CONSTANT_DECL,
    cx.CursorKind.TYPEDEF_DECL,
    cx.CursorKind.TYPE_ALIAS_DECL,
    cx.CursorKind.VAR_DECL,
    cx.CursorKind.FIELD_DECL,
    cx.CursorKind.NAMESPACE,
})

# Kinds where we index declarations even without a definition.
# Forward declarations of functions/methods are recorded so that
# find_all_callers_recursive can resolve calls even when the definition
# is in a different TU.  Definitions win in seen_usrs promotion.
_DECL_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
})

# Callable definitions that establish an "enclosing function" for references.
# When a cursor with one of these kinds is encountered during the ref walk
# and it is a definition, it is pushed onto fn_stack — subsequent references
# within its extent are attributed to this function as the caller (from_usr).
_CALLABLE_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
})

# Reference expression kinds → ref_kind label
_REF_KINDS = {
    cx.CursorKind.CALL_EXPR: "call",
    cx.CursorKind.DECL_REF_EXPR: "ref",
    cx.CursorKind.MEMBER_REF_EXPR: "member",
}


# Cursor kinds that are valid targets for an indirect call (function pointers)
_INDIRECT_TARGET_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
})


def _cursor_kind_label(kind: cx.CursorKind) -> str:
    """Map a libclang ``CursorKind`` to a short string label.

    Returns the user-facing kind string (e.g. ``"function"``, ``"class"``,
    ``"method"``) used to populate ``Symbol.kind``.  Falls back to the
    lowercase cursor kind name for unrecognised kinds.
    """
    mapping = {
        cx.CursorKind.FUNCTION_DECL: "function",
        cx.CursorKind.FUNCTION_TEMPLATE: "function",
        cx.CursorKind.CXX_METHOD: "method",
        cx.CursorKind.CONSTRUCTOR: "constructor",
        cx.CursorKind.DESTRUCTOR: "destructor",
        cx.CursorKind.CLASS_DECL: "class",
        cx.CursorKind.CLASS_TEMPLATE: "class",
        cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION: "class",
        cx.CursorKind.STRUCT_DECL: "struct",
        cx.CursorKind.UNION_DECL: "union",
        cx.CursorKind.ENUM_DECL: "enum",
        cx.CursorKind.ENUM_CONSTANT_DECL: "enum_constant",
        cx.CursorKind.TYPEDEF_DECL: "typedef",
        cx.CursorKind.TYPE_ALIAS_DECL: "typedef",
        cx.CursorKind.VAR_DECL: "variable",
        cx.CursorKind.FIELD_DECL: "field",
        cx.CursorKind.NAMESPACE: "namespace",
    }
    return mapping.get(kind, kind.name.lower())


def _is_anonymous_struct_or_union(cursor: cx.Cursor) -> bool:
    """True when *cursor* is a struct or union without a name.

    Both kinds count: a standard anonymous member (``union { int a; };``)
    and an unnamed type of a field (``struct { int x; } field;``).  A
    ``typedef struct { ... } T;`` has the name ``T`` and does not count.
    ``is_anonymous()`` asks the AST.  The earlier code matched the text
    ``"(unnamed"`` and ``"(anonymous"`` in the spelling, which is not a
    stable interface; measured on libclang 22 in C and C++, the two agree
    for each of these forms.
    """
    if cursor.kind not in (cx.CursorKind.STRUCT_DECL, cx.CursorKind.UNION_DECL):
        return False
    return bool(cursor.is_anonymous())


def _build_anon_usr_to_field(
    tu_cursor: cx.Cursor,
    skip_files: frozenset[str] | None = None,
    resolve_fn: Callable[[str], Path] | None = None,
) -> dict[str, str]:
    """Build a mapping from anonymous struct/union USR → enclosing field name.

    Walks struct/union/class/namespace bodies (skipping function bodies)
    and for each FIELD_DECL whose immediate child is an anonymous struct
    or union, records the anon USR → field name association.

    This is used as a pre-scan before symbol extraction so that anonymous
    structs defined as ``struct { ... } _payload;`` get indexed with the
    field name (``_payload``) instead of ``"(unnamed struct at ...)"``.

    When *skip_files* and *resolve_fn* are provided, subtrees rooted in
    already-processed header files are skipped via an early-return in
    the internal ``_walk`` function.  The skip check covers all cursor
    kinds — not just the ones ``_walk`` recurses into — keeping the
    guard consistent with ``_iter_cursor_skip`` semantics.
    """
    mapping: dict[str, str] = {}

    def _walk(cursor: cx.Cursor) -> None:
        if skip_files and resolve_fn:
            loc = cursor.location
            if loc.file:
                fpath = str(resolve_fn(str(loc.file.name)))
                if fpath in skip_files:
                    return  # omit subtree from already-processed header

        if cursor.kind == cx.CursorKind.FIELD_DECL:
            for child in cursor.get_children():
                if _is_anonymous_struct_or_union(child):
                    child_usr = cursor_usr(child)
                    if child_usr:
                        mapping[child_usr] = cursor.spelling
            return  # don't walk into field children (they're the anon struct members)

        if cursor.kind in (
            cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.UNION_DECL,
            cx.CursorKind.CLASS_DECL,
            cx.CursorKind.NAMESPACE,
            cx.CursorKind.TRANSLATION_UNIT,
        ):
            for child in cursor.get_children():
                _walk(child)

    _walk(tu_cursor)
    return mapping


def _qualified_name(
    cursor: cx.Cursor,
    anon_map: dict[str, str] | None = None,
) -> str:
    """Build the fully qualified name of a cursor via semantic parent traversal.

    Walks up the semantic parent chain (skipping the translation unit root)
    and joins each ancestor's spelling with ``::``.  Anonymous namespaces
    and unnamed entities produce empty segments which are collapsed by
    the join.

    When *anon_map* is provided and *cursor* is an anonymous struct/union
    with a recorded field name, the field name is used in place of the
    anonymous marker (e.g. ``"_radio_cmd"`` instead of
    ``"struct (unnamed at ...)"``).

    Returns a string like ``"namespace::Class::method"``, or an empty
    string for the translation unit root.
    """
    parts: list[str] = []
    c = cursor
    while c and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        spelling = c.spelling
        usr = cursor_usr(c) if anon_map else None
        field_name = anon_map.get(usr) if usr and anon_map else None
        if field_name:
            parts.append(field_name)
        elif spelling:
            parts.append(spelling)
        c = c.semantic_parent
    parts.reverse()
    return "::".join(parts)


def _signature(cursor: cx.Cursor) -> str:
    """Build a human-readable signature.

    For callables returns ``"return_type name(param1, param2)"``.
    For variables and fields returns ``"type name"``.
    Returns an empty string for other non-callable cursors.
    """
    # Variables and fields: return type + name
    if cursor.kind in (cx.CursorKind.VAR_DECL, cx.CursorKind.FIELD_DECL):
        try:
            return f"{cursor.type.spelling} {cursor.spelling}"
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("_signature: type.spelling failed for %s", cursor.spelling)
            return cursor.spelling

    # Callables only beyond this point
    if cursor.kind not in (
        cx.CursorKind.FUNCTION_DECL,
        cx.CursorKind.FUNCTION_TEMPLATE,
        cx.CursorKind.CXX_METHOD,
        cx.CursorKind.CONSTRUCTOR,
        cx.CursorKind.DESTRUCTOR,
    ):
        return ""
    try:
        result_type = cursor.result_type.spelling
        params = ", ".join(
            f"{p.type.spelling} {p.spelling}".strip()
            for p in cursor.get_arguments()
        )
        return f"{result_type} {cursor.spelling}({params})"
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("_signature: get_arguments failed for %s", cursor.spelling)
        return cursor.displayname


def _end_line(cursor: cx.Cursor, loc) -> int:
    """Last source line of the cursor's extent (full body for definitions).

    Uses libclang's exact AST extent — handles multi-line signatures, braces in
    strings/comments, macros, and templates correctly. Guards against the extent
    ending in a different file (can happen with macro expansions): returns 0 so
    callers fall back to a heuristic.
    """
    try:
        ext = cursor.extent
        end = ext.end
        if end.file and loc.file and end.file.name == loc.file.name and end.line >= loc.line:
            return end.line
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("_end_line: extent failed for %s", cursor.spelling)
        pass
    return 0


def _docstring(cursor: cx.Cursor) -> str:
    """Extract and clean the Doxygen/Javadoc comment attached to *cursor*.

    Strips leading comment markers (``/**``, ``//``, ``*``) and trailing
    whitespace from each line, then joins with newlines.  Returns an
    empty string when no raw comment is attached.

    libclang parses comments attached to the declaration — even when the
    comment appears before the declaration in source — via
    ``cursor.raw_comment``.
    """
    raw = cursor.raw_comment
    if not raw:
        return ""
    # Strip comment markers: /**, //, *, trailing whitespace
    lines = raw.splitlines()
    cleaned: list[str] = []
    for line in lines:
        line = re.sub(r"^\s*(/\*+|//+|\*+/?)\s?", "", line)
        line = line.rstrip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def _find_fn_refs_in_expr(
    cursor: cx.Cursor,
    _skip_usr: str | None = None,
    _depth: int = 0,
    expected: cx.Type | None = None,
) -> list[tuple[cx.Cursor, cx.SourceLocation]]:
    """Recursively extract function/method declarations referenced inside an expression.

    Returns ``(target, site)`` pairs: *site* is the location of the
    expression that names the target (the DECL_REF_EXPR or MEMBER_REF_EXPR).
    WHY the site and not the location of the enclosing expression: the walk
    reaches one reference from each enclosing call — ``reg(wrap(f))`` claims
    ``f`` for ``reg`` and again for ``wrap`` — and the row of a reference is
    keyed on its column.  The site gives the two claims one key.

    Depth-limited to prevent stack overflow on deeply nested C++ expressions.

    *expected* is the type of the storage that receives the function, when
    the caller knows it.  It picks one function out of an overload set
    (see ``_resolve_overload_set``).  It does not reach the arguments of a
    nested call: they go to the parameters of that call.
    """
    _FN_REF_MAX_DEPTH = 500
    if _depth > _FN_REF_MAX_DEPTH:
        _log.warning("_find_fn_refs_in_expr: max depth %d exceeded", _FN_REF_MAX_DEPTH)
        return []

    results: list[tuple[cx.Cursor, cx.SourceLocation]] = []

    # Direct reference to a callable (bare function name, method ref, etc.)
    if cursor.kind in (cx.CursorKind.DECL_REF_EXPR, cx.CursorKind.MEMBER_REF_EXPR):
        ref = cursor.referenced
        if ref is not None and ref.kind == cx.CursorKind.OVERLOADED_DECL_REF:
            # Only with a storage type: without one, the set is the callee
            # of a dependent call in a template (``h(x)``), and a pick
            # would turn a direct call into an indirect reference.
            if expected is None:
                return results
            for target in _resolve_overload_set(ref, expected):
                if not (_skip_usr and cursor_usr(target) == _skip_usr) and target.location.file:
                    results.append((target, cursor.location))
            return results
        if ref is not None and ref.kind in _INDIRECT_TARGET_KINDS:
            # Skip if this is the callee of the enclosing function call
            if _skip_usr and cursor_usr(ref) == _skip_usr:
                return results
            if ref.location.file:
                results.append((ref, cursor.location))
        return results

    # Address-of operator (&) — peel and recurse into the operand
    if cursor.kind == cx.CursorKind.UNARY_OPERATOR:
        for child in cursor.get_children():
            results.extend(_find_fn_refs_in_expr(child, _skip_usr, _depth + 1, expected))
        return results

    # Nested call expression — e.g. callback(&Class::method, this).
    # Always recurse into arguments to find function pointer targets.
    # When the nested callee is directly resolvable AND has a valid file
    # location (not a built-in), upgrade _skip_usr to that callee's USR.
    # This prevents emitting indirect edges for the callee's OWN children:
    # e.g. inside ``callback(&handler, this)``, ``callback`` itself is the
    # callee, so its parameters (handler's address) should NOT also be
    # recorded as if they were called by ``callback``.
    # If the nested callee cannot be resolved (template wrapper), keep the
    # outer _skip_usr — we still descend into arguments to find targets.
    if cursor.kind == cx.CursorKind.CALL_EXPR:
        nested_callee = cursor.referenced
        nested_skip = _skip_usr
        if nested_callee is not None:
            nested_callee_usr = cursor_usr(nested_callee)
            if nested_callee_usr:
                loc = nested_callee.location
                if loc.file:
                    nested_skip = nested_callee_usr
        for child in cursor.get_children():
            results.extend(_find_fn_refs_in_expr(child, nested_skip, _depth + 1))
        return results

    # Default: recurse into all children (handles implicit casts, parentheses, etc.)
    # The body of a lambda is code of its own: the storage type of the
    # element does not reach it.
    inner_expected = None if cursor.kind == cx.CursorKind.LAMBDA_EXPR else expected
    for child in cursor.get_children():
        results.extend(_find_fn_refs_in_expr(child, _skip_usr, _depth + 1, inner_expected))

    return results


# Known callback-wrapper template names used by _spelling_is_callback_wrapper
# and _is_fn_ptr_type to detect function-pointer types stored behind a
# template facade.  Tuple for O(N) linear scan over a small fixed set.
_CALLBACK_TYPE_PREFIXES = (
    "Callback<", "function<", "std::function<", "EventHandler<",
)


def _spelling_is_callback_wrapper(spelling: str) -> bool:
    """True when *spelling* contains a known callback-wrapper template name."""
    if "<" not in spelling or ">" not in spelling:
        return False
    for prefix in _CALLBACK_TYPE_PREFIXES:
        if spelling.startswith(prefix):
            return True
        if "::" in spelling and prefix in spelling:
            return True
    return False


def _is_fn_ptr_type(t: cx.Type) -> bool:
    """True when *t* is (or resolves via typedef to) a function pointer.

    Handles three cases:
    1. Direct pointer-to-function types (e.g. ``void (*)(int)``)
    2. Template-based callback wrappers via canonical spelling
       (``Callback<void()>``, ``std::function<…>``)
    3. Unresolved types on cross-compiled toolchains — falls back to
       the original spelling (before canonical resolution), which
       libclang preserves even when the underlying type is unknown.
    """
    try:
        canon = t.get_canonical()
        if canon.kind == cx.TypeKind.POINTER:
            pointee = canon.get_pointee()
            return pointee.kind in (cx.TypeKind.FUNCTIONPROTO, cx.TypeKind.FUNCTIONNOPROTO)
        if canon.kind == cx.TypeKind.MEMBERPOINTER:
            pointee = canon.get_pointee()
            return pointee.kind in (cx.TypeKind.FUNCTIONPROTO, cx.TypeKind.FUNCTIONNOPROTO)
        if _spelling_is_callback_wrapper(canon.spelling):
            return True
    except (ValueError, TypeError, RuntimeError, AttributeError):
        pass
    # Fallback: original spelling for cross-compiled toolchains where
    # canonical resolution yields an incomplete type
    try:
        if _spelling_is_callback_wrapper(t.spelling):
            return True
    except (ValueError, TypeError, RuntimeError, AttributeError):
        pass
    return False


def _first_child_unwrapped(cursor: cx.Cursor) -> cx.Cursor | None:
    """Return the first child of *cursor*, unwrapping ``UNEXPOSED_EXPR`` nodes.

    Used to extract the callee expression from a ``CALL_EXPR`` when
    ``cursor.referenced`` is ``None`` — the first child is the callee,
    subsequent children are arguments.  ``UNEXPOSED_EXPR`` wrappers
    (common in template instantiations and macro expansions) are
    unwrapped recursively until a concrete expression kind is reached.
    """
    try:
        children = list(cursor.get_children())
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("_first_child_unwrapped: get_children failed")
        return None
    if not children:
        return None
    first = children[0]
    while first.kind == cx.CursorKind.UNEXPOSED_EXPR:
        try:
            grandkids = list(first.get_children())
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("_first_child_unwrapped: UNEXPOSED_EXPR children failed")
            break
        if not grandkids:
            break
        first = grandkids[0]
    return first


def _process_one_base_specifier(
    child: cx.Cursor,
    cls_usr: str,
    access_map: dict[cx.AccessSpecifier, str],
) -> InheritanceRecord | None:
    """Build an InheritanceRecord from one CXX_BASE_SPECIFIER child cursor.

    Returns ``None`` for non-base-specifier cursors or when the base class
    cannot be resolved (e.g. forward-declared template base with no
    instantiation visible in this TU).

    ``is_virtual_base()`` (binding of libclang 20 and later) tells a
    virtual base.
    """
    if child.kind != cx.CursorKind.CXX_BASE_SPECIFIER:
        return None
    base_ref = child.referenced
    if base_ref is None:
        return None
    base_usr = cursor_usr(base_ref)
    if not base_usr:
        return None
    base_loc = base_ref.location
    if not base_loc.file:
        return None
    access = access_map.get(child.access_specifier, "public")
    return InheritanceRecord(
        derived_usr=cls_usr,
        base_usr=base_usr,
        access=access,
        is_virtual=child.is_virtual_base(),
    )


def _extract_inheritance(class_cursors: list[cx.Cursor]) -> list[InheritanceRecord]:
    """Extract C++ inheritance edges from collected class/struct definition cursors."""
    inheritance: list[InheritanceRecord] = []
    _access_map = {
        cx.AccessSpecifier.PUBLIC: "public",
        cx.AccessSpecifier.PROTECTED: "protected",
        cx.AccessSpecifier.PRIVATE: "private",
    }
    for cls_cursor in class_cursors:
        cls_usr = cursor_usr(cls_cursor)
        if not cls_usr:
            continue
        try:
            for child in cls_cursor.get_children():
                rec = _process_one_base_specifier(child, cls_usr, _access_map)
                if rec is not None:
                    inheritance.append(rec)
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("base class traversal failed for %s", cls_cursor.spelling)
            continue
    return inheritance


def _split_macro_definition(tokens: list) -> tuple[bool, str, str]:
    """Split the tokens of one ``#define`` into shape, parameters and value.

    ``tokens[0]`` is the macro name.  What follows is the parameter list of
    a function-like macro and then the replacement text.

    Returns ``(is_function_like, params, value)``.  *params* is the
    parameter list without its parentheses, written ``a, b``.  It is empty
    BOTH for an object-like macro and for ``NAME()``, thus the flag and not
    the string says which of the two a macro is.

    WHY the offsets decide whether a list opens, and not the spelling: a
    macro is function-like only when ``(`` touches the name.  ``#define
    SPACED (x)`` is object-like, and ``(x)`` is its replacement text.

    WHY the parameter list is split off at all: both used to live in the
    ``value`` column glued together — ``MBED_ASSERT`` read ``( expr ) do {
    … }`` — thus a reader that wanted the replacement text got the
    parameters with it, and a reader that wanted the parameters had to
    parse the string.
    """
    if len(tokens) < 2:
        return False, "", ""

    rest = " ".join(t.spelling for t in tokens[1:])
    opens_a_list = (
        tokens[1].spelling == "("
        and tokens[1].extent.start.offset == tokens[0].extent.end.offset
    )
    if not opens_a_list:
        return False, "", rest

    # Find the ")" that closes the list.  The depth is counted rather than
    # the first ")" taken: the lexer hands over whatever the file holds,
    # and a malformed #define must not make the split cut the value.
    depth = 0
    close = None
    for position in range(1, len(tokens)):
        spelling = tokens[position].spelling
        if spelling == "(":
            depth += 1
        elif spelling == ")":
            depth -= 1
            if depth == 0:
                close = position
                break
    if close is None:
        # No closing parenthesis — this is not a parameter list.  Keep every
        # token in the value, which is what a reader got before the split.
        return False, "", rest

    # Between the parentheses stand parameter names, commas, and ``...``.
    # The commas carry no information a joined list does not, thus they go.
    params = ", ".join(t.spelling for t in tokens[2:close] if t.spelling != ",")
    value = " ".join(t.spelling for t in tokens[close + 1:])
    return True, params, value


def _extract_macros(tu_cursor: cx.Cursor, resolve_fn, skip_files=None) -> list[Macro]:
    """Extract ``#define`` macro definitions, skipping headers in *skip_files*.

    Uses the module-level ``_log`` logger — no logging changes needed
    when the *skip_files* parameter is supplied.
    """
    macros: list[Macro] = []
    for child in tu_cursor.get_children():
        if child.kind != cx.CursorKind.MACRO_DEFINITION:
            continue
        loc = child.location
        if not loc.file:
            continue

        fpath = str(resolve_fn(loc.file.name))
        if skip_files and fpath in skip_files:
            continue  # macro from already processed header

        # The tokens answer every question about the shape of the #define,
        # thus they are read once and `_split_macro_definition` reads them.
        #
        # WHY the tokens decide whether a macro is function-like, and not
        # libclang: the binding (libclang 18.1.1, and also the one of
        # libclang 22) has no ``Cursor.is_macro_function_like`` — the name is
        # absent from ``clang/cindex.py``.  The call raised AttributeError on EVERY
        # macro, and a broad ``except`` turned each one into a silent False.
        # Measured over six indexes: 734 386 macros, 0 marked function-like,
        # and 453 256 of them held a value that opens with a parameter list.
        is_fn_like = False
        params = ""
        value = ""
        try:
            is_fn_like, params, value = _split_macro_definition(list(child.get_tokens()))
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("macro token extraction failed for %s", child.spelling)
            is_fn_like = False
            params = ""
            value = ""

        macros.append(Macro(
            name=child.spelling,
            value=value,
            params=params,
            line=loc.line,
            is_function_like=is_fn_like,
            file=fpath,
        ))
    return macros


#: The references of one unit, by the key of their site (see ``_claim_ref``).
_SeenRefs = dict[tuple, Reference]

#: A bound on the steps from an instance to its template; a real chain has 1 to 3.
_TEMPLATE_CHAIN_LIMIT = 16


def _same_place(a: cx.Cursor, b: cx.Cursor) -> bool:
    """Tell if the two cursors have the same location (file, line and column)."""
    la, lb = a.location, b.location
    if not la.file or not lb.file:
        return False
    return (la.file.name, la.line, la.column) == (lb.file.name, lb.line, lb.column)


def _has_place_of(instance: cx.Cursor, template: cx.Cursor) -> bool:
    """Tell if *instance* has the place of *template*: of its declaration, or of its definition.

    WHY the definition too: ``specialized_template`` gives the first
    declaration of the template, and an instance has the place of the
    declaration that it instantiates.  A member defined out of its class
    (``template <class T> int A<T>::put() {...}``) and a function template
    declared before its definition give an instance at the definition.
    """
    if _same_place(instance, template):
        return True
    definition = template.get_definition()
    return definition is not None and _same_place(instance, definition)


def _code_usr(target: cx.Cursor) -> str:
    """Give the USR of the code that *target* names: the template of an implicit instance, else its own.

    WHY: an implicit instantiation (``read_twice<Sensor>``, ``Box<int>::get``)
    is no symbol of the index: the code of it is the template.  An edge to
    the USR of the instance was invisible to ``find_callers`` of the
    template.  Measured: 14 to 19 % of the call edges of a C++ project went
    to a USR that is no symbol, and this mapping repairs 16 to 53 % of them
    (probe s6, plan of the call edges, phase 4).

    WHY the test of the place: clang gives an implicit instantiation the
    location of its template, and an explicit specialization
    (``template <> int f<char>(char)``, ``template <> int Box<short>::get()``)
    its own.  An explicit specialization is written code with a symbol of
    its own, thus it keeps its USR.  ``specialized_template`` alone gives
    the template for both.  The step repeats, because a member of an
    instance of a partial specialization names the member of the partial
    specialization first.

    Only a function maps.  The instance of a static data member of a class
    template is a symbol of its own (with ``template_usr``), thus a
    reference to it stays on it.

    Limit: a template and an explicit specialization that one macro
    expansion writes have the same place (the place of the expansion),
    thus the specialization maps to the template there.
    """
    if target.spelling == "operator()" and target.kind in _CLOSURE_CALL_KINDS:
        expression = _lambda_of(target)
        if expression is not None:
            return _lambda_usr(expression)
    if target.kind not in _INSTANCE_FUNCTION_KINDS:
        return cursor_usr(target)
    current = target
    for _ in range(_TEMPLATE_CHAIN_LIMIT):
        template = current.specialized_template
        if template is None or not _has_place_of(current, template):
            break
        current = template
    return cursor_usr(current) or cursor_usr(target)


# The cursor kinds of a function that a template instance can be.
_INSTANCE_FUNCTION_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.CONVERSION_FUNCTION,
})
_INSTANCE_CLASS_KINDS = frozenset({
    cx.CursorKind.CLASS_DECL,
    cx.CursorKind.STRUCT_DECL,
    cx.CursorKind.UNION_DECL,
})


def _is_implicit_instance(cursor: cx.Cursor) -> bool:
    """Tell if *cursor* is an implicit instantiation: it has the place of its template (see ``_code_usr``)."""
    template = cursor.specialized_template
    return template is not None and _has_place_of(cursor, template)


def _instance_key(cursor: cx.Cursor) -> str:
    """The key of a template instance: its USR and its declaration.

    WHY not the USR alone: libclang 22 gives the instances of one template
    for two lambdas ONE USR (`post<#...@Sa>`: a closure type is `@Sa`
    whatever the lambda), thus only the first instance was walked, and
    only the first lambda got its edge.  The two instances are two
    declarations, and the hash of the cursor tells them apart.
    """
    return f"{cursor_usr(cursor)}#{cursor.hash}"


def _seed_instance(seeds: dict[str, cx.Cursor], cursor: cx.Cursor) -> None:
    """Add *cursor* to *seeds* when it is a function that is an implicit template instance."""
    if cursor.kind in _INSTANCE_FUNCTION_KINDS and _is_implicit_instance(cursor):
        if cursor_usr(cursor):
            seeds.setdefault(_instance_key(cursor), cursor)


def _seed_explicit_instantiation(seeds: dict[str, cx.Cursor], cursor: cx.Cursor) -> None:
    """Add the members of an explicit class instantiation (``template class Box<int>;``) to *seeds*.

    WHY: no call of the unit names those members, but the instantiation
    defines each of them.  Measured: without it, a ``template class
    FIFO<...>;`` of a project lost 17 edges (probe s6).  An explicit
    specialization (``template <> struct Box<char>``) also has a template,
    but its members are written code and no implicit instance, thus
    ``_seed_instance`` leaves them out.

    Limit: libclang 22 gives no cursor for an explicit instantiation of a
    function (``template int twice<Dev>(Dev&);``), thus the body of such an
    instance gives edges only when a call of the unit names it.
    """
    if cursor.kind in _INSTANCE_CLASS_KINDS and cursor.specialized_template is not None and cursor.is_definition():
        for method in cursor.type.get_methods():
            _seed_instance(seeds, method)


# The kinds of the call operator of a closure (a generic lambda has a template).
_CLOSURE_CALL_KINDS = frozenset({cx.CursorKind.CXX_METHOD, cx.CursorKind.FUNCTION_TEMPLATE})
# The kinds whose code can own a lambda: the key of the lambda starts with
# the key of that code (`_lambda_usr`).
_LAMBDA_OWNER_KINDS = _CALLABLE_KINDS | _INSTANCE_FUNCTION_KINDS


def _lambda_of(cursor: cx.Cursor) -> cx.Cursor | None:
    """Give the LAMBDA_EXPR that *cursor* is, or whose closure class or call operator it is.

    A closure class is an anonymous CLASS_DECL with the place of its lambda
    (measured, libclang 22).  The cursor at that place tells it from an
    unnamed struct with an ``operator()``: it is the LAMBDA_EXPR for a
    closure, the STRUCT_DECL else.  An implicit instance of a template has
    the place of the template, thus the closure of a lambda in an instance
    body gives the lambda of the written template.
    """
    if cursor.kind == cx.CursorKind.LAMBDA_EXPR:
        return cursor
    if cursor.kind == cx.CursorKind.CLASS_DECL:
        closure: cx.Cursor | None = cursor
    elif cursor.kind in _CLOSURE_CALL_KINDS and cursor.spelling == "operator()":
        closure = cursor.semantic_parent
    else:
        return None
    if closure is None or closure.kind != cx.CursorKind.CLASS_DECL or not closure.is_anonymous():
        return None
    if closure.location.file is None:
        return None
    seen = _seen_lambdas().get(_closure_key(closure))
    if seen is not None:
        return seen
    at = cx.Cursor.from_location(closure.translation_unit, closure.location)
    return at if at is not None and at.kind == cx.CursorKind.LAMBDA_EXPR else None


def _lambda_usr(expression: cx.Cursor) -> str:
    """Give the key of the lambda *expression*: its owner and its place.

    WHY not the USR of libclang: the closure class and its call operator of
    two lambdas of one function with one signature have ONE USR (measured,
    libclang 22: both lambdas of `Client::start` give
    `c:@S@Client@F@start#@Sa@F@operator()#I#1`), and the lambda expression
    has none.  The owner is the code that holds the lambda (`_code_usr` of
    it: the template for an instance, the outer lambda for a nested one),
    or the scope of a lambda outside a function.  The place makes it one of
    a kind, also in a header that several units read.  Limit: two lambdas
    that one macro expansion writes have one place.
    """
    closure = expression.type.get_declaration()
    owner = closure.semantic_parent if closure is not None else None
    if owner is not None and owner.kind in _LAMBDA_OWNER_KINDS:
        owner_key = _code_usr(owner)
    else:
        owner_key = cursor_usr(owner) if owner is not None else ""
    place = expression.location
    file_name = file_key(place.file.name) if place.file is not None else ""
    return f"{owner_key or 'c:'}@lambda@{file_name}:{place.line}:{place.column}"


# The lambdas that the walks of the unit have seen, by the USR and place of
# their closure: `_lambda_of` finds a lambda through it when the cursor at
# the place of the closure is no LAMBDA_EXPR (a lambda written in the body
# of a macro: the place is the expansion).  One registry for each thread
# that extracts a unit (`extract_all` starts it).
_SEEN_LAMBDAS = threading.local()


def _seen_lambdas() -> dict[tuple[str, str, int, int], cx.Cursor]:
    registry = getattr(_SEEN_LAMBDAS, "by_closure", None)
    if registry is None:
        registry = {}
        _SEEN_LAMBDAS.by_closure = registry
    return registry


def _closure_key(closure: cx.Cursor) -> tuple[str, str, int, int]:
    place = closure.location
    return (cursor_usr(closure), place.file.name if place.file else "", place.line, place.column)


def _remember_lambda(expression: cx.Cursor) -> None:
    """Register the closure of the lambda *expression* (see `_SEEN_LAMBDAS`)."""
    closure = expression.type.get_declaration()
    if closure is not None and closure.kind == cx.CursorKind.CLASS_DECL:
        _seen_lambdas()[_closure_key(closure)] = expression


def _lambda_body_cursors(expression: cx.Cursor) -> Generator[cx.Cursor, None, None]:
    """Yield the cursors of the body of the lambda *expression*.

    The body is the compound statement of the lambda.  A class or a
    function defined in it is code of its own and is left out; a nested
    lambda is in it.
    """
    stack = [child for child in expression.get_children() if child.kind == cx.CursorKind.COMPOUND_STMT]
    while stack:
        node = stack.pop()
        yield node
        if node.kind in _INSTANCE_CLASS_KINDS or (node.kind in _CALLABLE_KINDS and node.is_definition()):
            continue
        stack.extend(node.get_children())


def _lambda_symbol(expression: cx.Cursor) -> Symbol | None:
    """Give the symbol of the lambda *expression* (kind ``lambda``).

    A lambda is code of its own: the calls in its body are calls of it, and
    a call of it (`l()`) is an edge to it.  Its parent is the code that
    holds it, and its name is `<lambda>` after the name of that code.  Its
    last line is the last line of its body: a lambda in a macro argument has
    an extent of no width (measured, libclang 22).
    """
    place = expression.location
    if place.file is None:
        return None
    closure = expression.type.get_declaration()
    owner = closure.semantic_parent if closure is not None else None
    call = None
    if closure is not None:
        call = next((child for child in closure.get_children() if child.spelling == "operator()"), None)
    parent_usr = ""
    owner_name = ""
    if owner is not None and owner.kind in _LAMBDA_OWNER_KINDS:
        parent_usr = _code_usr(owner)
        owner_lambda = _lambda_of(owner)
        owner_name = (
            _lambda_qualified_name(owner_lambda) if owner_lambda is not None
            else _qualified_name(owner, {})
        )
    return Symbol(
        name="<lambda>",
        qualified_name=f"{owner_name}::<lambda>" if owner_name else "<lambda>",
        kind="lambda",
        file=place.file.name,
        line=place.line,
        column=place.column,
        is_definition=True,
        signature=call.type.spelling if call is not None else "",
        docstring="",
        usr=_lambda_usr(expression),
        end_line=max(
            [expression.extent.end.line] + [
                child.extent.end.line for child in expression.get_children()
                if child.kind == cx.CursorKind.COMPOUND_STMT
            ]
        ),
        parent_usr=parent_usr,
    )


def _lambda_qualified_name(expression: cx.Cursor) -> str:
    """The qualified name of a lambda: the name of its owner and `<lambda>`."""
    symbol = _lambda_symbol(expression)
    return symbol.qualified_name if symbol is not None else "<lambda>"


def _walk_template_instances(
    seeds: dict[str, cx.Cursor], refs: list[Reference], seen_ref: _SeenRefs,
    internal_decls: dict[str, InternalDecl],
) -> list[cx.Cursor]:
    """Walk the bodies of the template instances that the unit uses, claim their calls, and give the definitions.

    WHY: a call in a template body that names a dependent type
    (``device.sample()`` in ``template <class T> int read_twice(T& device)``)
    has no target in the template: only an instantiation resolves it.  The
    AST walk visits the written code, thus such calls had no edge.  The
    body of each implicit instance has the target; its edge goes to the
    template (caller and target, ``_code_usr``) at the place of the call in
    the template, with the origin ``INSTANTIATION``.  Measured on three
    projects, the cursors give 91 to 97 % of the edges of the indexing API
    of libclang for instantiations, at 0.3 to 0.7 % of the time of a parse
    (probe s6).  Bodies that no cursor reaches: implicit or defaulted
    special members, and some members of an ``extern template``.

    The work list starts from *seeds*: the instances that the written code
    of the unit names.  A body can name more instances, and a class
    instance can define members that no call names (a destructor, a
    virtual method): ``Type.get_methods()`` of each class instance that a
    walked body belongs to adds them.  A body is walked where it lies, also
    in a header of the skip set, because the instance of this unit is not
    the instance of the unit that owns the header.  An explicit
    specialization is written code, not an instance: the AST walk has it.

    Only calls give an edge here.  A function address in an instance body
    is a slot fact: the definitions that the walk gives go to
    ``_slots.collect_slot_facts``, in the order of the walk.  A function
    with internal linkage that a body names gets its place in
    *internal_decls*, as in the walk of the written code: a dependent call
    of the template names it only in the instance.

    A body is walked as the AST walk reads it: the calls in a lambda belong
    to the lambda (`_lambda_usr`), and a call of a lambda (``l()``) is an
    edge to it.  A function defined inside the body (a method of a local
    class) is a definition of its own: the walk does not descend into it,
    and it is walked as an instance when a call names it.
    """
    work = list(seeds.values())
    queued = set(seeds)
    walked: set[str] = set()
    definitions: list[cx.Cursor] = []
    classes: dict[str, cx.Cursor] = {}
    classes_done: set[str] = set()
    while True:
        if not work:
            for class_usr, class_cursor in list(classes.items()):
                if class_usr in classes_done:
                    continue
                classes_done.add(class_usr)
                for method in class_cursor.type.get_methods():
                    _queue_instance(method, queued, work)
            if not work:
                return definitions
        declaration = work.pop()
        definition = declaration.get_definition()
        if definition is None or not definition.is_definition() or not _is_implicit_instance(definition):
            continue
        if not cursor_usr(definition):
            continue
        definition_key = _instance_key(definition)
        if definition_key in walked:
            continue
        walked.add(definition_key)
        definitions.append(definition)
        parent = definition.semantic_parent
        if parent is not None and parent.kind in _INSTANCE_CLASS_KINDS and _is_implicit_instance(parent):
            classes.setdefault(cursor_usr(parent), parent)
        definition_key = _code_usr(definition)
        for node, frame in _body_cursors(definition):
            if node.kind not in _REF_KINDS:
                continue
            target = node.referenced
            if target is None:
                continue
            if node.kind == cx.CursorKind.DECL_REF_EXPR:
                _note_internal_function(node, internal_decls)
            _queue_instance(target, queued, work)
            if node.kind != cx.CursorKind.CALL_EXPR:
                continue
            caller = definition_key if frame is None else _lambda_usr(frame)
            site = node.location
            if not site.file or not target.location.file or not cursor_usr(target):
                continue
            _claim_ref(refs, seen_ref, Reference(
                to_usr=_code_usr(target), from_file=site.file.name, from_line=site.line,
                from_usr=caller, ref_kind="call", from_col=site.column,
                origins=RefOrigin.INSTANTIATION,
            ))


def _body_cursors(
    definition: cx.Cursor,
) -> Generator[tuple[cx.Cursor, cx.Cursor | None], None, None]:
    """Yield the cursors of the body of *definition* in preorder, without nested definitions.

    Each cursor comes with the innermost LAMBDA_EXPR whose body holds it,
    or None: the body of a lambda is the lambda's code (see
    ``_walk_template_instances``), and its captures are the code around it.
    A function or a class defined inside the body is code of its own.
    """
    stack: list[tuple[cx.Cursor, cx.Cursor | None]] = [
        (child, None) for child in reversed(list(definition.get_children()))
    ]
    while stack:
        node, frame = stack.pop()
        if node.kind in _INSTANCE_CLASS_KINDS or (node.kind in _CALLABLE_KINDS and node.is_definition()):
            continue
        yield node, frame
        is_lambda = node.kind == cx.CursorKind.LAMBDA_EXPR
        if is_lambda:
            _remember_lambda(node)
        stack.extend(
            (child, node if is_lambda and child.kind == cx.CursorKind.COMPOUND_STMT else frame)
            for child in reversed(list(node.get_children()))
        )


def _queue_instance(cursor: cx.Cursor, queued: set[str], work: list[cx.Cursor]) -> None:
    """Put *cursor* on the work list of ``_walk_template_instances`` when it is a new implicit instance."""
    if cursor.kind in _INSTANCE_FUNCTION_KINDS and _is_implicit_instance(cursor):
        key = _instance_key(cursor)
        if cursor_usr(cursor) and key not in queued:
            queued.add(key)
            work.append(cursor)


def _claim_ref(refs: list[Reference], seen_ref: _SeenRefs, ref: Reference) -> bool:
    """Add *ref* to *refs*, or join its origins to the reference of the same site.

    The key of a site is the key of a row of ``refs``: the target, the
    file, the line, the column, the caller, the kind and the slot.  Two
    analyses that find one site give one reference with both origins, and
    not two references — ``find_callers`` would count the call twice.

    Two rules of the slots meet here:

    * Two slots of one table can hold the same function on the same line, as
      in ``{ reset, reset }``.  Those are two facts, so the slot joins the
      identity and both are kept.
    * The same reference can be reached twice — once by an enclosing array
      list that knows the slot, and again by the nested list inside it,
      which does not.  Those are one fact, and the slotted view is the
      better one.

    A slotted claim therefore also takes a key of its line without the
    column, and a later slotless claim of the same target, line, caller and
    kind joins the slotted reference.  WHY without the column: the two claims
    name different cursors of one element.  The enclosing list gives the
    location of the element (``{ 0, isr_a }`` at its ``{``), and the nested
    list the location of the function inside it.  The AST walk is
    preorder, so the enclosing list always claims first.

    Returns True when *ref* is a new reference.
    """
    key = (ref.to_usr, ref.from_file, ref.from_line, ref.from_col, ref.from_usr, ref.ref_kind)
    line_key = (ref.to_usr, ref.from_file, ref.from_line, ref.from_usr, ref.ref_kind)
    if ref.slot_index is None:
        claim: tuple = key
        known = seen_ref.get(key) or seen_ref.get(("slotted", *line_key))
    else:
        claim = (*key, ref.slot_index)
        known = seen_ref.get(claim)
    if known is not None:
        known.origins |= ref.origins
        return False
    seen_ref[claim] = ref
    if ref.slot_index is not None:
        seen_ref.setdefault(("slotted", *line_key), ref)
    refs.append(ref)
    return True


def _emit_fn_ptr_targets(
    expr_cursor: cx.Cursor,
    caller_usr: str | None,
    seen_ref: _SeenRefs,
    refs: list[Reference],
    skip_usr: str | None = None,
    in_init_list: bool = False,
    slot_index: int | None = None,
    expected_type: cx.Type | None = None,
) -> None:
    """Emit an indirect reference for each function whose address *expr_cursor* takes.

    *expected_type* is the type of the storage, for an overload set.
    *in_init_list* is True for an element of an initializer list, which
    is scanned as a whole (see below).  *slot_index* is the position of
    *expr_cursor* inside a positional init list, and reaches the stored
    reference unchanged.  Only the init-list caller sets it; every other
    caller has no position to give.

    Walks the children of *expr_cursor* looking for function declarations
    (including those nested inside unary operators and casts).  Where the
    address goes, and which calls reach it, is the slot facts
    (``indexer/_slots.py``).
    """
    loc = expr_cursor.location
    if not loc.file:
        return

    # The expressions that can hold the function.  WHY the element itself
    # when it is a DECL_REF_EXPR: in C++, libclang gives the element of
    # ``{ dev_cb, other }`` as a bare DECL_REF_EXPR, with no implicit-cast
    # wrapper as in C.  Its children are only qualifiers (``ns::``), thus
    # a scan of the children found nothing, and a C++ table of functions
    # gave no indirect reference and no slot.  ``{ &dev_cb }`` worked,
    # because the UNARY_OPERATOR has the reference as a child.
    #
    # A call that is an init-list element (``{ f_init, pick(fb) }``) is
    # scanned as a whole too: the CALL_EXPR branch of the search skips its
    # callee, which a scan of its children took as the stored function.
    if expr_cursor.kind == cx.CursorKind.DECL_REF_EXPR or (
        in_init_list and expr_cursor.kind == cx.CursorKind.CALL_EXPR
    ):
        scanned = [expr_cursor]
    else:
        scanned = list(expr_cursor.get_children())

    for child in scanned:
        targets = _find_fn_refs_in_expr(child, skip_usr, expected=expected_type)
        if not targets:
            continue
        for target, site in targets:
            if cursor_usr(target) == skip_usr:
                continue
            target_usr = _code_usr(target)
            target_loc = target.location
            if target_loc.file and site.file:
                _claim_ref(refs, seen_ref, Reference(
                    to_usr=target_usr,
                    from_file=site.file.name,
                    from_line=site.line,
                    from_usr=caller_usr,
                    ref_kind="indirect",
                    slot_index=slot_index,
                    from_col=site.column,
                    origins=RefOrigin.DIRECT,
                ))


def _build_refs(
    tu: cx.TranslationUnit,
    tu_path_str: str,
    resolve_fn,
    anon_usr_to_field: dict[str, str],
    _log,
    skip_files: frozenset[str] | None = None,
) -> tuple[list[Reference], list[cx.Cursor], dict[str, InternalDecl]]:
    """Walk the TU AST extracting references.

    When *skip_files* is a non-empty frozenset, subtrees of already
    processed headers are skipped via :func:`_iter_cursor_skip` —
    references from those headers were already captured during the
    first TU that included them.

    **Phase 1 — AST walk**: traverses the translation unit in pre-order.
    A ``fn_stack`` tracks the enclosing function for each cursor — needed
    because references store the caller's USR (``from_usr``).  The stack
    is maintained by two pop conditions:
    * File-mismatch pop — when the cursor moves into a different source
      file and the top-of-stack function's span belongs to the TU's own
      file, it is popped (we left that function's body).
    * Line-overflow pop — when the cursor's line exceeds the top-of-stack
      function's ``end_line`` (which is non-zero only for TU-resident
      definitions), it is popped (we passed the closing brace).

    Returns (refs, instances, internal_decls): *internal_decls* holds the
    place of each function with internal linkage that the unit names
    (``_note_internal_function``), and *instances* are the
    definitions of the template instances that the unit walked.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    refs: list[Reference] = []
    seen_ref: _SeenRefs = {}
    internal_decls: dict[str, InternalDecl] = {}

    fn_stack: list[tuple[str, int, str]] = []
    # The caller of each cursor in the body of a lambda, by cursor hash:
    # the lambda (see _lambda_body_cursors).  A lambda shares its lines with
    # the code around it (`reg([] { work(); }); other();`), and a lambda in
    # a macro argument has no extent of its own, thus the tree decides and
    # not a line or an offset.
    lambda_callers: dict[tuple[int, int], str] = {}
    # The constructor calls of the initializers of objects at namespace or
    # class scope, by cursor hash: they run before main.  A VAR_DECL comes
    # before its initializer in the preorder walk.
    static_init_calls: set[int] = set()
    # The template instances that the written code of this unit names, by
    # USR, in the order of the walk (see _walk_template_instances).
    instance_seeds: dict[str, cx.Cursor] = {}

    for cursor in (
        _iter_cursor_skip(tu.cursor, skip_files, resolve_fn)
        if skip_files
        else tu.cursor.walk_preorder()
    ):
        _cl = cursor.location
        _cl_file = str(Path(_cl.file.name).resolve()) if _cl and _cl.file else None
        _cl_line = _cl.line if _cl else -1
        # Pop condition 1 — file mismatch: the top-of-stack function was
        # defined in the TU's own file (tu_path_str) but the cursor is now
        # in a different file (e.g. an included header).  The function
        # body ended before the include; pop it.
        while fn_stack and fn_stack[-1][2] and _cl_file and fn_stack[-1][2] != _cl_file:
            fn_stack.pop()
        # Pop condition 2 — line overflow: the top-of-stack function has a
        # known end_line (>0, set only for TU-resident definitions) and the
        # cursor line has advanced past it.  We left the function body.
        while fn_stack and fn_stack[-1][1] > 0 and _cl is not None and _cl_line > fn_stack[-1][1]:
            fn_stack.pop()
        cur_fn = fn_stack[-1][0] if fn_stack else None
        cur_fn = lambda_callers.get((cursor.hash, cursor.kind.value), cur_fn)

        if cursor.kind in _CALLABLE_KINDS and cursor.is_definition():
            own_usr = cursor_usr(cursor)
            if own_usr:
                cur_fn = own_usr
            try:
                _ext = cursor.extent
                if _ext.start.file:
                    _ext_file = _ext.start.file.name
                    _resolved_file = str(resolve_fn(_ext_file))
                    if _resolved_file == tu_path_str:
                        fn_stack.append((cur_fn or '', _ext.end.line, tu_path_str))
                    else:
                        # Header-resident definition (inline body in an
                        # included header).  Store the resolved file path so
                        # the file-mismatch pop (condition 1) fires when the
                        # walk leaves the header, and a guarded end_line so
                        # the line-overflow pop (condition 2) fires when the
                        # walk passes the definition within the header.
                        # Previously both were zeroed, so the definition
                        # never popped and its USR leaked into ``from_usr``
                        # for unrelated cursors (file-scope declarations and
                        # the bodies of following functions).
                        _end = 0
                        if (
                            _ext.end.file
                            and _ext.end.file.name == _ext_file
                            and _ext.end.line >= _ext.start.line
                        ):
                            _end = _ext.end.line
                        fn_stack.append((cur_fn or '', _end, _resolved_file))
                else:
                    fn_stack.append((cur_fn or '', 0, ""))
            except (ValueError, TypeError, RuntimeError, AttributeError):
                _log.debug(
                    "cursor.extent failed for %s",
                    cursor.spelling,
                )
                fn_stack.append((cur_fn or '', 0, ""))

        if cursor.kind == cx.CursorKind.VAR_DECL:
            static_init_calls |= _static_init_constructor_calls(cursor)
        _seed_explicit_instantiation(instance_seeds, cursor)

        _process_ref_cursor(
            cursor, cur_fn, refs, seen_ref, resolve_fn, static_init_calls, instance_seeds,
        )
        if cursor.kind == cx.CursorKind.DECL_REF_EXPR:
            _note_internal_function(cursor, internal_decls)

        # The body of a lambda is code of the lambda: the calls in it are
        # its calls.  The expression itself (its address), its captures and
        # the default arguments of its parameters belong to the code around
        # it.  A nested lambda comes later in the walk and takes its body.
        if cursor.kind == cx.CursorKind.LAMBDA_EXPR:
            _remember_lambda(cursor)
            key = _lambda_usr(cursor)
            for node in _lambda_body_cursors(cursor):
                lambda_callers[(node.hash, node.kind.value)] = key

    # The calls that only the instances of the templates resolve.
    instances = _walk_template_instances(instance_seeds, refs, seen_ref, internal_decls)

    return refs, instances, internal_decls


def _is_plain_assignment(cursor: cx.Cursor) -> bool:
    """Tell if the BINARY_OPERATOR *cursor* is ``=``.

    WHY: every BINARY_OPERATOR was read as an assignment, thus a comparison
    such as ``hi2c->XferISR != I2C_Master_ISR_DMA`` stored a function into
    the field (measured over every index on one machine: 353 such rows).
    The slot facts (``_slots.py``) take the left side of ``=`` only.

    ``binary_operator`` (binding of libclang 19 and later) reads the AST,
    not the tokens, thus it also answers for an operator that a macro
    expands to.  A compound operator such as ``+=`` is a
    COMPOUND_ASSIGNMENT_OPERATOR, a different cursor kind, thus ``=`` is
    the only assignment here.

    No ``except`` around the call: the ctypes code that this replaced broke
    under the libclang 22 binding (a TypeError in each call), and a broad
    ``except`` made each operator an assignment without a word.  An error
    of the binding must stop the parse of the unit, where the run reports
    it.
    """
    return cursor.binary_operator == cx.BinaryOperator.Assign


def _is_tentative_definition(cursor: cx.Cursor) -> bool:
    """Tell if *cursor* is a C tentative definition: `static cb_t g_cb;`, `cb_t b;` at file scope.

    C11 6.9.2: a file-scope object with no initializer and no ``extern``
    is a tentative definition, and the end of the unit makes it a
    definition when the unit has no other one.  libclang 22 reports it as
    no definition (measured: ``is_definition()`` is False, and
    ``get_definition()`` is None), thus the walk dropped each such
    variable: a function pointer that firmware fills at run time
    (`static cb_t g_cb;`) was no symbol, and no tool could name it.  In
    C++ such a declaration is a definition already, and a declaration
    with ``extern`` or in a class is not one.
    """
    if cursor.kind != cx.CursorKind.VAR_DECL:
        return False
    parent = cursor.semantic_parent
    if parent is None or parent.kind != cx.CursorKind.TRANSLATION_UNIT:
        return False
    if cursor.storage_class == cx.StorageClass.EXTERN:
        return False
    return cursor.get_definition() is None


def _process_one_symbol(
    cursor: cx.Cursor,
    symbols: list[Symbol],
    seen_usrs: dict[str, bool],
    class_cursors: list[cx.Cursor],
    anon_usr_to_field: dict[str, str],
    _log,
) -> bool:
    """Process a single cursor during AST walk for symbol extraction.

    Returns True if the cursor was added as a new symbol.

    **Kind classification** — ``VAR_DECL`` cursors are split into two
    sub-kinds based on their semantic parent:
    * ``varglobal`` — global, namespace, linkage-spec, or class-scope
      variables (including ``static`` class members).
    * ``varlocal`` — variables inside function/method/constructor bodies.

    **Template USR resolution** — for non-template declarations that are
    instantiations of a template (e.g. ``Callback<void(int)>``), the
    ``specialized_template`` cursor is resolved through a three-level
    fallback chain:
    1. Direct ``cursor.specialized_template``.
    2. Parent's ``semantic_parent.specialized_template`` — handles members
       of template classes.
    3. ``cursor.type.get_declaration().specialized_template`` — handles
       typedef/variable declarations where the type itself is a template
       instantiation.

    **Deduplication** — ``seen_usrs`` maps USR → ``bool(definition)``.
    When a forward declaration is encountered first, it is recorded with
    ``False``.  When the definition is later met in the same TU, the flag
    is promoted to ``True``.  A duplicate declaration (with the same
    definition status) is silently skipped — returns ``False``.
    """
    loc = cursor.location
    if not loc.file:
        return False

    is_tentative = not cursor.is_definition() and _is_tentative_definition(cursor)
    is_def = cursor.is_definition() or is_tentative
    if not is_def and cursor.kind not in _DECL_KINDS:
        return False

    usr = cursor_usr(cursor)
    if not usr:
        return False

    enum_val: int | None = None
    if cursor.kind == cx.CursorKind.ENUM_CONSTANT_DECL:
        try:
            enum_val = cursor.enum_value
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("enum_value failed for %s at %s:%d", cursor.spelling, loc.file.name, loc.line)

    is_virtual = bool(cursor.is_virtual_method()) if cursor.kind in (cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR) else False
    is_pure = bool(cursor.is_pure_virtual_method()) if cursor.kind in (cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR) else False

    sem_parent = cursor.semantic_parent
    kind = _cursor_kind_label(cursor.kind)
    # Split VAR_DECL into varglobal / varlocal based on semantic parent.
    # A variable declared at file, namespace, or class scope is a
    # varglobal (shared state); a variable inside a function body is a
    # varlocal (stack/register).  This split powers find_variables for
    # global-state discovery without flooding results with loop counters.
    if kind == "variable":
        if sem_parent and sem_parent.kind in (
            cx.CursorKind.TRANSLATION_UNIT,
            cx.CursorKind.NAMESPACE,
            cx.CursorKind.LINKAGE_SPEC,
            cx.CursorKind.CLASS_DECL,
            cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.UNION_DECL,
            cx.CursorKind.CLASS_TEMPLATE,
            cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        ):
            kind = "varglobal"
        else:
            kind = "varlocal"

    parent_usr = ""
    if sem_parent and sem_parent.kind in (
        cx.CursorKind.CLASS_DECL,
        cx.CursorKind.STRUCT_DECL,
        cx.CursorKind.UNION_DECL,
        cx.CursorKind.CLASS_TEMPLATE,
        cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    ):
        parent_usr = cursor_usr(sem_parent) or ""

    if not parent_usr and kind in ("varlocal", "variable"):
        if sem_parent and sem_parent.kind in _CALLABLE_KINDS:
            # A local of a lambda belongs to the lambda (`_code_usr` of the
            # call operator of its closure).
            parent_usr = _code_usr(sem_parent) or ""

    is_template = cursor.kind in (
        cx.CursorKind.CLASS_TEMPLATE,
        cx.CursorKind.FUNCTION_TEMPLATE,
        cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    )
    template_usr = ""
    if not is_template:
        # Three-level fallback for template USR resolution.
        # Level 1: direct specialized_template (most common).
        # Level 2: parent's specialized_template — members of template
        #   classes (e.g. Callback<void()>::operator()) inherit their
        #   template USR from the enclosing class.
        # Level 3: type declaration lookup — typedef/variable whose type
        #   is a template instantiation (e.g. ``Callback<void()> cb;``).
        _template_check_kinds = (
            cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.FUNCTION_DECL, cx.CursorKind.CXX_METHOD,
            cx.CursorKind.CONSTRUCTOR, cx.CursorKind.FIELD_DECL,
            cx.CursorKind.VAR_DECL, cx.CursorKind.PARM_DECL,
        )
        # ``specialized_template`` (binding of libclang 21 and later) gives
        # the template that a cursor instantiates, or None.
        if cursor.kind in _template_check_kinds:
            specialized = cursor.specialized_template
            if specialized is not None:
                template_usr = cursor_usr(specialized) or ""
            if not template_usr:
                parent = cursor.semantic_parent
                if parent is not None:
                    parent_spec = parent.specialized_template
                    if parent_spec is not None:
                        template_usr = cursor_usr(parent_spec) or ""
            if not template_usr:
                decl = cursor.type.get_declaration()
                if decl is not None and decl != cursor:
                    decl_spec = decl.specialized_template
                    if decl_spec is not None:
                        template_usr = cursor_usr(decl_spec) or ""

    # Deduplication: seen_usrs[usr] = bool(is_definition).
    # Forward declaration (False) → definition (True) is promoted.
    # Duplicate declaration or definition with the same status is skipped.
    prev = seen_usrs.get(usr)
    if prev is not None:
        if is_def and not prev:
            seen_usrs[usr] = True
        else:
            return False
    else:
        seen_usrs[usr] = is_def

    is_anon = _is_anonymous_struct_or_union(cursor)
    field_name = anon_usr_to_field.get(usr) if is_anon else None
    symbol_name = field_name if field_name else cursor.spelling
    symbol_qname = _qualified_name(cursor, anon_usr_to_field)

    symbols.append(Symbol(
        name=symbol_name,
        qualified_name=symbol_qname,
        kind=kind,
        file=loc.file.name,
        line=loc.line,
        column=loc.column,
        is_definition=is_def,
        signature=_signature(cursor),
        docstring=_docstring(cursor),
        usr=usr,
        end_line=_end_line(cursor, loc),
        enum_value=enum_val,
        is_virtual=is_virtual,
        is_pure_virtual=is_pure,
        parent_usr=parent_usr,
        is_template=is_template,
        template_usr=template_usr,
        link_name=_link_name(cursor),
        is_tentative=is_tentative,
    ))

    if cursor.kind in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL) and is_def:
        class_cursors.append(cursor)

    return True


# The kinds whose symbol a linker can resolve by name.
_LINKABLE_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.CONVERSION_FUNCTION,
    cx.CursorKind.VAR_DECL,
})


def _link_name(cursor: cx.Cursor) -> str:
    """Return the name the linker resolves to *cursor*, or "".

    The mangled name of the compiler, for external linkage only: a name
    that an assembly unit writes (`bl main`, a vector slot) reaches a
    symbol only through it.  Measured with libclang 22: `main` in C++ has
    the USR `c:@F@main#` and the mangled name `main`; `extern "C"` and C
    give the plain name; a C++ function gives `_Z…`; `static` and an
    anonymous namespace have internal linkage, and no other object can
    name them.  A template gives no mangled name, and its instances are no
    symbols.  For a constructor libclang gives one of the names that GCC
    emits (`C1`/`C2`), thus a call by the other name finds no symbol.

    A member of a class template is a template too, and libclang gives a
    false name for a static data member of one (measured: `Box<T>::count`
    gives `_ZN3Box5countE`, which no object holds).
    """
    if cursor.kind not in _LINKABLE_KINDS or cursor.linkage != cx.LinkageKind.EXTERNAL:
        return ""
    parent = cursor.semantic_parent
    if parent is not None and parent.kind in (
        cx.CursorKind.CLASS_TEMPLATE,
        cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
    ):
        return ""
    return cursor.mangled_name or ""


# The declarations that hold other declarations at the level of a file.
_DECLARATION_SCOPES = frozenset({
    cx.CursorKind.LINKAGE_SPEC,
    cx.CursorKind.NAMESPACE,
    cx.CursorKind.CLASS_DECL,
    cx.CursorKind.STRUCT_DECL,
    cx.CursorKind.UNION_DECL,
})


def _weak_usrs(tu_cursor: cx.Cursor) -> set[str]:
    """Return the USRs that a declaration of the translation unit makes weak.

    GCC defines a symbol weakly when one declaration of it in the
    translation unit has the attribute `weak`, also a declaration in a
    header before the definition (measured, arm-none-eabi-gcc: `nm` gives
    `W`).  libclang gives the attribute as an unexposed attribute, whose
    tokens name the macro (`__weak`, `__WEAK`) and not the attribute.  The
    declaration that holds it, pretty printed with no body, names it:
    `__attribute__((weak)) void f(void)`.  A definition does not print an
    attribute that it inherits, thus each declaration is read, and the
    walk does not skip headers that an earlier translation unit read.

    Only a declaration with external linkage and with an attribute is
    printed.  Not seen: `#pragma weak` (libclang gives no attribute for
    it), `weak` on a declaration after the definition, which clang drops
    and GCC keeps, and a declaration in a function body
    (`extern void f(void) __attribute__((weak));`), which the walk of the
    file level does not reach.
    """
    policy = cx.PrintingPolicy.create(tu_cursor)
    policy.set_property(cx.PrintingPolicyProperty.TerseOutput, 1)
    found: set[str] = set()
    work = list(tu_cursor.get_children())
    while work:
        cursor = work.pop()
        if cursor.kind in _DECLARATION_SCOPES:
            work.extend(cursor.get_children())
            continue
        if cursor.kind not in _LINKABLE_KINDS or cursor.linkage != cx.LinkageKind.EXTERNAL:
            continue
        if not any(child.kind.is_attribute() for child in cursor.get_children()):
            continue
        if "weak" in _attribute_names(cursor.pretty_printed(policy)):
            found.add(cursor_usr(cursor))
    return found


_ATTRIBUTE_OPEN = re.compile(r"__attribute__\s*\(\(|\[\[")


def _attribute_names(text: str) -> set[str]:
    """The names of the attributes that the declaration *text* lists.

    `__attribute__((weak, alias("f")))` gives `weak` and `alias`, and
    `[[gnu::weak]]` gives `weak`.  A name in the declarator (`weak_var`)
    is no attribute.  `__weak__` is `weak`, as GCC reads it.
    """
    names: set[str] = set()
    quoted = False
    at = 0
    while at < len(text):
        char = text[at]
        if quoted:
            # A string (an initializer, a message) holds no attribute.
            quoted = char != '"' or text[at - 1] == "\\"
            at += 1
            continue
        if char == '"':
            quoted = True
            at += 1
            continue
        opened = _ATTRIBUTE_OPEN.match(text, at)
        if opened is None:
            at += 1
            continue
        close = "))" if opened.group().startswith("__attribute__") else "]]"
        items, at = _top_level_items(text, opened.end(), close)
        names.update(_attribute_name(item) for item in items)
    names.discard("")
    return names


def _top_level_items(text: str, start: int, close: str) -> tuple[list[str], int]:
    """The items of the list at *start* up to *close*, split at the commas
    outside parentheses, brackets and string literals, and the index after
    the list."""
    items: list[str] = []
    item: list[str] = []
    depth = 0
    quoted = False
    at = start
    while at < len(text):
        char = text[at]
        if quoted:
            quoted = char != '"' or text[at - 1] == "\\"
        elif char == '"':
            quoted = True
        elif depth == 0 and text.startswith(close, at):
            break
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            items.append("".join(item))
            item = []
            at += 1
            continue
        item.append(char)
        at += 1
    items.append("".join(item))
    return items, at + len(close)


def _attribute_name(item: str) -> str:
    """`gnu::weak` and `__weak__` are `weak`; arguments are no part of a name."""
    name = item.strip().split("(", 1)[0].strip().split("::")[-1]
    return name.removeprefix("__").removesuffix("__")


def _process_ref_cursor(
    cursor: cx.Cursor,
    cur_fn: str | None,
    refs: list[Reference],
    seen_ref: _SeenRefs,
    resolve_fn: Callable[..., object],
    static_init_calls: set[int],
    instance_seeds: dict[str, cx.Cursor],
) -> None:
    """Process a single cursor for references and indirect calls — dispatcher."""
    loc = cursor.location
    if not loc.file:
        return

    _handle_direct_refs(cursor, cur_fn, refs, seen_ref, static_init_calls, instance_seeds)
    if cursor.kind == cx.CursorKind.CALL_EXPR:
        _handle_fn_ptr_as_argument(cursor, cur_fn, refs, seen_ref)
    _handle_fn_ptr_cases(cursor, cur_fn, refs, seen_ref)


def _note_internal_function(cursor: cx.Cursor, internal_decls: dict[str, InternalDecl]) -> None:
    """Record the definition of the function with internal linkage that *cursor* names, by its USR.

    WHY: clang gives a `static` function of a header one USR in a C unit
    and an other in a C++ unit, and the index holds the symbol of only one
    of them.  The place of the definition and the name match the USRs of
    one function (``_internal_usrs``).  A `static` function template is no
    concern: only a C++ unit can compile it, and each C++ unit gives it one
    USR.
    """
    target = cursor.referenced
    if target is None or target.kind != cx.CursorKind.FUNCTION_DECL or target.linkage != cx.LinkageKind.INTERNAL:
        return
    usr = _code_usr(target)
    if not usr or usr in internal_decls:
        return
    definition = target.get_definition() or target
    location = definition.location
    if location.file is None:
        return
    internal_decls[usr] = InternalDecl(definition.spelling, location.file.name, location.line, location.column)


def _add_ref(refs: list[Reference], seen_ref: _SeenRefs, to_usr: str,
             loc: cx.SourceLocation, from_usr: str | None,
             ref_kind: str, origins: RefOrigin) -> bool:
    """Claim the reference at the location *loc* of a cursor (see ``_claim_ref``)."""
    return _claim_ref(refs, seen_ref, Reference(
        to_usr=to_usr, from_file=loc.file.name, from_line=loc.line,
        from_usr=from_usr, ref_kind=ref_kind, from_col=loc.column, origins=origins,
    ))


def _handle_direct_refs(cursor: cx.Cursor, cur_fn: str | None, refs: list[Reference],
                       seen_ref: _SeenRefs, static_init_calls: set[int],
                       instance_seeds: dict[str, cx.Cursor]) -> None:
    """Direct call/ref/member references from ``cursor.referenced``, and run-time vectors.

    WHY no fallback for a CALL_EXPR without a target: two fallbacks once
    wrote a call edge for it.  The field-call fallback took the method of a
    MEMBER_REF_EXPR child; measured on 100 units, it wrote no edge, because
    a call with a resolved member has a resolved target too.  The
    constructor fallback wrote an edge to every constructor of the type of
    each variable that the call passes, typically a dependent call in a
    template body.  An unresolved call names no constructor, thus those
    edges were guesses (seen in libstdc++: ``std::push_heap`` to the
    constructor of its comparator).

    A constructor call in the initializer of an object at namespace or
    class scope gets ``ref_kind='implicit_construct'``: its hash is in
    *static_init_calls* (see :func:`_static_init_constructor_calls`).

    Limit: a construction that has no CALL_EXPR in the source gets no
    edge.  These are a member that a constructor does not name in its
    initializer list, and each member construction of an implicit
    constructor, which has no body.  A rule-based derivation of those was
    measured and dropped: each review found more C++ forms that it got
    wrong (defaulted copy constructors, unions, aggregates, template
    instances).  A missing edge is honest; a wrong edge reads like a fact.
    """
    loc = cursor.location
    ref_kind = _REF_KINDS.get(cursor.kind)
    if ref_kind is not None:
        referenced = cursor.referenced
        if referenced is not None:
            # A seed of the walk of the template instances: the written code
            # of this unit names the instance.
            _seed_instance(instance_seeds, referenced)
            to_usr = _code_usr(referenced)
            ref_loc = referenced.location
            if referenced.kind == cx.CursorKind.CONVERSION_FUNCTION:
                # The implicit conversion of a lambda to a function pointer
                # (`reg([](int y) { ... })`) takes the address of the
                # lambda: no call, and no symbol for the conversion.
                closure_lambda = _lambda_of(referenced.semantic_parent)
                if closure_lambda is not None:
                    _add_ref(refs, seen_ref, _lambda_usr(closure_lambda), loc, cur_fn,
                             "indirect", RefOrigin.DIRECT)
                    return
            if to_usr and ref_loc.file:
                if (cursor.kind == cx.CursorKind.CALL_EXPR
                        and referenced.kind == cx.CursorKind.CONSTRUCTOR
                        and cursor.hash in static_init_calls):
                    ref_kind = "implicit_construct"
                _add_ref(refs, seen_ref, to_usr, loc, cur_fn, ref_kind, RefOrigin.DIRECT)

        if cursor.kind == cx.CursorKind.CALL_EXPR:
            _handle_runtime_vector(cursor, referenced, cur_fn, refs, seen_ref)


def _handle_runtime_vector(cursor: cx.Cursor, referenced: cx.Cursor | None,
                           cur_fn: str | None, refs: list[Reference],
                           seen_ref: _SeenRefs) -> None:
    """Record a vector table entry this call installs at run time.

    A CMSIS target that moves its table into RAM fills it with
    ``NVIC_SetVector``, and the static table then says nothing about
    which handler services the interrupt.  See ``nvic`` for what makes a
    call qualify and why the IRQ number is stored unshifted.

    Deduplicated on the key of its site, the IRQ number (``slot_index``)
    included, so one call site emitted twice by the preprocessor stays one
    edge, and two IRQs that one line gives the same handler stay two.
    """
    found = runtime_vector_reference(cursor, referenced, cur_fn)
    if found is None:
        return
    _claim_ref(refs, seen_ref, found)


def _handle_fn_ptr_as_argument(cursor: cx.Cursor, cur_fn: str | None,
                               refs: list[Reference], seen_ref: _SeenRefs) -> None:
    """Function pointers passed as call arguments.

    Emits an ``indirect`` reference for every callable target found in the
    arguments, also when the callee cannot be resolved (e.g.
    ``_timeout.attach(callback(&WDT::_timeout_interrupt), delay)`` where
    ``attach`` is template-obscured).  The parameter that receives the
    function is a slot fact (``_slots.py``).
    """
    direct_callee = cursor.referenced
    direct_callee_usr = cursor_usr(direct_callee) if direct_callee else None

    for arg in cursor.get_arguments():
        targets = _find_fn_refs_in_expr(arg, direct_callee_usr)
        for target, site in targets:
            raw_usr = cursor_usr(target)
            target_usr = _code_usr(target)
            if raw_usr and raw_usr != direct_callee_usr and site.file:
                _add_ref(refs, seen_ref, target_usr, site, cur_fn, "indirect", RefOrigin.DIRECT)
    _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs, direct_callee_usr)


def _init_list_is_positional(cursor: cx.Cursor) -> bool:
    """Report whether every element of an init list is given by position.

    A POSITIONAL list writes its elements in the order of the array, so the
    position of an element IS its index::

        void (*table[])(void) = { reset, nmi, hard_fault };   // 0, 1, 2

    A DESIGNATED list names the destination of each element, so the source
    order tells you nothing about the index::

        void (*table[])(void) = { [11] = svc, [3] = hard_fault };

    Only a positional list can give a slot number.  One designated element
    makes the whole list unsafe to count, because the elements after it
    continue from the index it named — not from their own position.

    The test reads the first token of each element: an array designator
    starts with ``[`` and a field designator with ``.``.  Two notes on why
    this is a token test and not an AST test:

    * The AST cannot tell the two apart here.  A designator ``[5] = &fn``
      exposes an INTEGER_LITERAL with two children as its first child — and
      so does the plain value ``1 + 2``.  A measurement over 12 lists put
      the AST rule wrong on 1 of them and this rule wrong on none.
    * A float element such as ``{ .5, &fn }`` does NOT read as a designator,
      because clang emits ``.5`` as one FLOATING_LITERAL token, not as ``.``
      followed by ``5``.  This was measured, not assumed.
    * A C++ lambda also starts with ``[`` (``{ [](void *) {...}, nullptr }``).
      The AST tells it: the lambda starts where its element starts, and a
      designator ``[5] = &fn`` starts before its first child.

    Returns False when the tokens cannot be read.  A slot number that is
    wrong is worse for a reader than a slot number that is absent, so an
    unreadable list gives up its positions instead of guessing them.
    """
    try:
        for element in cursor.get_children():
            tokens = list(element.get_tokens())
            if tokens and tokens[0].spelling in ("[", ".") and not _starts_with_lambda(element):
                return False
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False
    return True


def _starts_with_lambda(element: cx.Cursor) -> bool:
    """Tell if *element* starts with a lambda: it, or a first child at its start, is a LAMBDA_EXPR."""
    start = element.extent.start.offset
    node: cx.Cursor | None = element
    while node is not None and node.extent.start.offset == start:
        if node.kind == cx.CursorKind.LAMBDA_EXPR:
            return True
        node = next(iter(node.get_children()), None)
    return False


# Every way libclang spells an array type.  A slot number is an array index,
# so a list that initialises anything else has no slot to give.
_ARRAY_TYPE_KINDS = frozenset({
    cx.TypeKind.CONSTANTARRAY,
    cx.TypeKind.INCOMPLETEARRAY,
    cx.TypeKind.VARIABLEARRAY,
    cx.TypeKind.DEPENDENTSIZEDARRAY,
})


def _init_list_gives_slots(cursor: cx.Cursor) -> bool:
    """Report whether the positions in this init list are array indices.

    A position is only a slot number when all four hold:

    1. **The list is the outermost one.**  libclang reports a
       ``semantic_parent`` for the list that belongs to a declaration and
       None for a list nested inside another list.  This matters because an
       enclosing list already covers the whole subtree: for the Zephyr shape
       ``struct entry table[] = { {0, isr_a}, {0, isr_b} }`` the outer pass
       finds ``isr_a`` at outer position 0, which is the true slot, while
       the inner list would offer position 1 — the position of the ``fn``
       FIELD.  Recording both leaves two contradicting slots for one
       reference.  Measured over 18 lists in C and C++: the None test agreed
       with the true nesting every time.
    2. **The list initialises an array.**  A struct list numbers fields.
    3. **The array is one-dimensional.**  A table such as
       ``void (*fsm[2][2])(void)`` has a row and a column, and no single
       number is its slot.  The element type is read through
       ``get_canonical`` so a typedef cannot hide the second dimension.
    4. **The elements are positional** — see ``_init_list_is_positional``.

    Returns False when any type or parent query fails, on the same ground as
    the positional test: an absent slot beats a wrong one.
    """
    try:
        if cursor.semantic_parent is None:
            return False
        list_type = cursor.type
        if list_type.kind not in _ARRAY_TYPE_KINDS:
            return False
        element = list_type.get_array_element_type().get_canonical()
        if element.kind in _ARRAY_TYPE_KINDS:
            return False
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False
    return _init_list_is_positional(cursor)


# The cursor kinds that wrap a value without changing what it is: the
# value of a designated element can sit inside each of them.
_VALUE_WRAPPERS = frozenset({
    cx.CursorKind.UNEXPOSED_EXPR,
    cx.CursorKind.PAREN_EXPR,
    cx.CursorKind.UNARY_OPERATOR,
    cx.CursorKind.CSTYLE_CAST_EXPR,
    cx.CursorKind.CXX_STATIC_CAST_EXPR,
    cx.CursorKind.CXX_REINTERPRET_CAST_EXPR,
    cx.CursorKind.CXX_CONST_CAST_EXPR,
    cx.CursorKind.CXX_NEW_EXPR,
})
# The cursor kinds of a struct value that holds an init list of its own.
_LIST_VALUES = frozenset({
    cx.CursorKind.COMPOUND_LITERAL_EXPR,
    cx.CursorKind.CXX_FUNCTIONAL_CAST_EXPR,
})


def _holds_nested_list(value: cx.Cursor) -> bool:
    """Tell if *value* is a struct value that an init list of its own gives.

    ``{ ... }``, ``(struct ops){ ... }``, ``&(struct ops){ ... }`` and the
    C++ ``ops{ ... }`` all give their fields through an INIT_LIST_EXPR of
    their own, which the walk reaches and reads.  The last child of a
    wrapper is the wrapped value: a cast gives its TYPE_REF first.

    Only a list of a struct (or union, or an array of them) counts.  A
    list of an array of function pointers, ``.handlers = { f, g }``, or a
    scalar in braces, ``.cb = { f }``, has no designators of its own: its
    functions go to THIS field, and the element gives the row.
    """
    while value.kind in _VALUE_WRAPPERS:
        inner = list(value.get_children())
        if not inner:
            return False
        value = inner[-1]
    if value.kind in _LIST_VALUES:
        inner = list(value.get_children())
        if not inner:
            return False
        value = inner[-1]
    if value.kind != cx.CursorKind.INIT_LIST_EXPR:
        return False
    try:
        return _strip_arrays(value.type.get_canonical()).kind == cx.TypeKind.RECORD
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False


def _strip_arrays(type_: cx.Type) -> cx.Type:
    """The element type of *type_* after every array level, or *type_* itself.

    A typedef of an array (``typedef cb_t cb_arr_t[4]``) is an array too:
    its declared type is read, and not its canonical type, thus the
    element keeps its own name (``cb_t``), the name that a call site has.
    The loop stops after a fixed number of steps: each step removes one
    level of sugar or of array, and a real type has few of them.
    """
    for _ in range(32):
        if type_.kind in _ARRAY_TYPE_KINDS:
            type_ = type_.element_type
        elif type_.kind == cx.TypeKind.TYPEDEF and type_.get_canonical().kind in _ARRAY_TYPE_KINDS:
            type_ = type_.get_declaration().underlying_typedef_type
        else:
            break
    return type_


def _field_type_spelling(field_ref: cx.Cursor) -> str:
    """The type of the storage that receives the function: the field, or its element.

    A function can go into an array field only as an element, through an
    index (``.handlers[2] = f``, ``.h[1][2] = f``) or a brace list
    (``.handlers = { f, g }``).  Thus each array level comes off.  The
    type of the field (``cb_t[4]``) would never join on the type of a
    call site (``cb_t``).
    """
    try:
        return _strip_arrays(field_ref.type).spelling
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return ""


def _storage_type(type_: cx.Type) -> cx.Type | None:
    """The type of one function pointer that *type_* stores, or None.

    The array levels come off (``cb_t[4]`` stores ``cb_t``).  A type that
    does not end in a pointer to a function gives None: a struct list has
    no single storage type.
    """
    try:
        storage = _strip_arrays(type_)
        canonical = storage.get_canonical()
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return None
    if canonical.kind in (cx.TypeKind.POINTER, cx.TypeKind.MEMBERPOINTER):
        return storage
    return None


def _resolve_overload_set(overload_ref: cx.Cursor, expected: cx.Type) -> list[cx.Cursor]:
    """Pick the function that an overloaded name gives to *expected*.

    WHY: in an init list, libclang gives the syntactic form of the
    element.  ``static cb_t table[] = { ov, tf<int> };`` comes as a
    DECL_REF_EXPR to an OVERLOADED_DECL_REF, the overload set, and not to
    the function that the conversion chose, as it does for ``gcb = ov``.
    A C++ table of overloaded or template handlers thus gave no indirect
    reference and no slot.

    The choice repeats what the compiler did, as far as it is certain:

    * The function whose type is the type that *expected* points to,
      when there is exactly one.  A plain pointer takes a free function or
      a static method; a member pointer takes a non-static method.  A
      ``noexcept`` function also goes into a pointer without
      ``noexcept``, when no exact match exists.
    * Else the one function template, when the set holds no function:
      ``tf<int>`` gives the template, because an implicit specialization
      is no symbol of the index.
    * Else the only member of the set.

    Anything else gives no target: a wrong edge in the call graph is
    worse than a missing one.
    """
    # WHY from_result: the libclang 22 binding registers
    # clang_getOverloadedDecl without the errcheck that gives the cursor its
    # translation unit, thus ``.type`` of the raw cursor raised
    # AttributeError, and the ``except`` below made each set give no target.
    # ``from_result`` does what the errcheck did: a null cursor becomes None.
    lib = cx.conf.lib
    count = int(lib.clang_getNumOverloadedDecls(overload_ref))
    decls = [cx.Cursor.from_result(lib.clang_getOverloadedDecl(overload_ref, i), overload_ref) for i in range(count)]
    decls = [d for d in decls if d is not None and d.kind in _INDIRECT_TARGET_KINDS]
    functions = [d for d in decls if d.kind != cx.CursorKind.FUNCTION_TEMPLATE]
    templates = [d for d in decls if d.kind == cx.CursorKind.FUNCTION_TEMPLATE]
    try:
        canonical = expected.get_canonical()
        member = canonical.kind == cx.TypeKind.MEMBERPOINTER
        wanted = canonical.get_pointee().get_canonical().spelling
        fitting = [
            f for f in functions
            if (f.kind == cx.CursorKind.CXX_METHOD and not f.is_static_method()) == member
        ]
        spellings = {id(f): f.type.get_canonical().spelling for f in fitting}
    except (ValueError, TypeError, RuntimeError, AttributeError):
        fitting, spellings, wanted = [], {}, ""
    for same in (
        lambda text: text == wanted,
        lambda text: text.replace(" noexcept", "") == wanted,
    ):
        matching = [f for f in fitting if same(spellings[id(f)])]
        if len(matching) == 1:
            return matching
        if matching:
            break
    if not functions and len(templates) == 1:
        return templates
    if len(decls) == 1:
        return decls
    return []


def _init_list_element_field(
    element: cx.Cursor,
) -> tuple[str | None, str, str, cx.Type | None]:
    """The field that one element of an INIT_LIST_EXPR assigns: (USR, name, type, field type).

    Gives ``(None, "", "", None)`` when the element assigns no function to
    a field of this list.  The type is ``""`` when the caller must read it
    from the element, as before.  The field type is the libclang type of
    the field: the caller reads from it which functions the field really
    receives (``_stored_functions``) and the storage type of an overload
    set.

    libclang gives a designated element as an UNEXPOSED_EXPR whose children
    are the designators (MEMBER_REF, or an index for ``[n] =``), and then
    the value.  ``_extract_lhs_field``, which the element used before,
    takes the FIRST field reference anywhere in the element.  That gave
    these wrong rows, measured on the local indexes and on probes:

    * A value that is a nested struct (``.ops = { .init = f }``,
      ``.ops = (struct ops){ .init = f }``, ``{ .cmd = X, .handler = h }``
      in an array) wrote ``ops = f`` or ``cmd = h`` next to the correct
      row of the nested list.  Such an element now gives no row.  The
      nested list is an INIT_LIST_EXPR of its own, and the walk gives its
      fields.  The element still gives its indirect references, with the
      slot of an array (see ``_claim_ref``).
    * A chain of designators wrote its FIRST field: ``.ops.init = f`` gave
      ``ops = f``.  The chain now gives its LAST field, and the type of
      that field, because a name can repeat along the chain
      (``.cb.cb = f``).  An array field gives the type of its element
      (see ``_field_type_spelling``).

    * An element that names no field assigns to no VARIABLE.  The old
      reading took the first variable or field access in the VALUE as the
      field: ``{ &obj.handler }`` wrote ``obj = handler``, and
      ``{ tn<N> }`` would write ``N = tn``.  Such an element now gives no
      field.
    """
    if _holds_nested_list(element):
        return (None, "", "", None)
    children = list(element.get_children())
    if len(children) >= 2:
        if _holds_nested_list(children[-1]):
            return (None, "", "", None)
        designators = children[:-1]
        member_at = [i for i, c in enumerate(designators) if c.kind == cx.CursorKind.MEMBER_REF]
        if member_at:
            last = member_at[-1]
            ref = designators[last].referenced
            if ref is not None and ref.kind == cx.CursorKind.FIELD_DECL:
                return (cursor_usr(ref), ref.spelling, _field_type_spelling(ref), ref.type)
    return (None, "", "", None)


def _scalar_count(type_: cx.Type) -> int:
    """The number of scalars in the array *type_* (each level of a known size), else 0."""
    count = 1
    current = type_.get_canonical()
    while current.kind in _ARRAY_TYPE_KINDS:
        if current.kind != cx.TypeKind.CONSTANTARRAY or current.element_count <= 0:
            return 0
        count *= current.element_count
        current = current.element_type.get_canonical()
    return count


def _is_fn_pointer_storage(type_: cx.Type) -> bool:
    """Tell if *type_*, after its array levels, is a pointer to a function."""
    try:
        canonical = _strip_arrays(type_).get_canonical()
        if canonical.kind == cx.TypeKind.MEMBERPOINTER:
            return True
        return canonical.kind == cx.TypeKind.POINTER and canonical.get_pointee().kind in (
            cx.TypeKind.FUNCTIONPROTO, cx.TypeKind.FUNCTIONNOPROTO,
        )
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False


def _positional_struct_fields(cursor: cx.Cursor) -> list[tuple[str | None, str, str, cx.Type | None]]:
    """The field of each leading element of a positional struct init list.

    ``static struct ops o = { dev_init, dev_cb };`` names no field: the
    position of each element is its field, in the order of the struct.
    The designated path (``_init_list_element_field``) has no name to read
    there, thus such an element got no field type at all.

    The list maps a position to a field only while that is certain, and
    stops at the first doubt.  The elements after the stop keep the old
    reading, which gives them no field.

    * The list must be positional: no element may carry a designator.
      ``_init_list_is_positional`` reads ``.`` and ``[``; the old GNU form
      ``cb: f`` shows as a MEMBER_REF before the value, as ``.cb = f``
      does, and is caught here.
    * The type must be a struct, a union or a C++ aggregate class with no
      base class.  A positional element of a C++17 aggregate initializes
      the bases first.  A union takes one element, its first member.
    * The fields come from the type (``get_fields``), in their order, and
      not from the children of the declaration.  The children have no
      field for an anonymous member: they have the declaration of its
      record.  The type gives the field of the member:

      - An anonymous struct or union member (C11, C++) is a field that
        libclang names after its record.  A braced element initializes it
        through its own list.  An element without braces stops the list
        (brace elision, below).
      - An anonymous member of the Microsoft extension (``struct pair;``
        with ``-fms-extensions``) is a field with no name, which stops the
        list.  The children show only a declaration of the record, which
        does not tell a member from a declaration, thus a list of the
        children gave the elements after it to the wrong fields.
      - A field of an unnamed type (``struct { ... } f;``) takes one
        element, as each named field does.  A stop at the unnamed record
        lost each field after it (measured: the timer thread and the timer
        setup function of the configuration of an RTOS).
      - An unnamed bit-field takes no element and is skipped.
    * A field that is a struct, a union or an array, with an element that
      has no braces of its own, stops the list: brace elision spreads that
      element over the inner values.  A field that is an array of function
      pointers takes that first element, then the list stops.  A string
      literal fills a whole ``char`` array.
    * A braced element of a struct field gives its own fields through the
      nested list; the outer field gets no row.  Every other named field
      gets its position, as a designated field does.

    Each entry is ``(USR, name, type, field type)`` as
    ``_init_list_element_field`` gives it.
    """
    try:
        list_type = cursor.type.get_canonical()
        if list_type.kind != cx.TypeKind.RECORD or not _init_list_is_positional(cursor):
            return []
        # The definition, not the first declaration: a C header declares an
        # opaque type (`typedef struct ops ops_t;`) and an other header
        # defines it.  The declaration of the type is the first one, which
        # has no member, thus such a list mapped no field (measured: the
        # cipher tables of an SDK, 50 functions with no slot).
        record = list_type.get_declaration()
        record = record.get_definition() or record
        children = list(record.get_children())
        elements = list(cursor.get_children())
        template = record.specialized_template
        if template is not None and not children:
            # An implicit instance of a class template shows no children
            # (measured, libclang 22): its template gives its base classes.
            children = list(template.get_children())
        members = list(list_type.get_fields())
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return []
    if any(c.kind == cx.CursorKind.CXX_BASE_SPECIFIER for c in children):
        return []
    if any(_has_member_designator(e) for e in elements):
        return []
    union = record.kind == cx.CursorKind.UNION_DECL
    mapped: list[tuple[str | None, str, str, cx.Type | None]] = []
    for member in members:
        if len(mapped) == len(elements) or (union and mapped):
            break
        if not member.spelling:
            if member.is_bitfield():
                continue
            break
        element = elements[len(mapped)]
        try:
            whole_type = member.type.get_canonical()
            inner = _strip_arrays(member.type).get_canonical()
        except (ValueError, TypeError, RuntimeError, AttributeError):
            break
        storage = _is_fn_pointer_storage(member.type)
        aggregate = inner.kind == cx.TypeKind.RECORD or whole_type.kind in _ARRAY_TYPE_KINDS
        if aggregate and _is_char_array(member.type) and _is_string_literal(element):
            mapped.append((None, "", "", None))
            continue
        if aggregate and not _initializes_whole_field(element, member.type):
            if storage:
                # Brace elision: an array of function pointers takes one
                # element for each of its slots (`std::array<cb_t, 3> a =
                # {h1, h2}`, `{ f1, f2, 3 }` for `{ cb_t cbs[2]; int n; }`).
                # A braced element among them stops the list.
                count = _scalar_count(member.type)
                taken = elements[len(mapped):len(mapped) + count] if count else []
                if not taken or any(e.kind == cx.CursorKind.INIT_LIST_EXPR for e in taken):
                    mapped.append(_field_entry(member))
                    break
                mapped.extend(_field_entry(member) for _ in taken)
                continue
            break
        if aggregate and not storage:
            mapped.append((None, "", "", None))
            continue
        mapped.append(_field_entry(member))
    return mapped


_CHAR_TYPE_KINDS = frozenset({
    cx.TypeKind.CHAR_S, cx.TypeKind.CHAR_U, cx.TypeKind.SCHAR, cx.TypeKind.UCHAR,
    cx.TypeKind.WCHAR, cx.TypeKind.CHAR16, cx.TypeKind.CHAR32,
})


def _is_char_array(field_type: cx.Type) -> bool:
    """Tell if *field_type* is one array of characters, which one string literal fills."""
    try:
        canonical = field_type.get_canonical()
        return (canonical.kind in _ARRAY_TYPE_KINDS
                and canonical.element_type.get_canonical().kind in _CHAR_TYPE_KINDS)
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False


def _initializes_whole_field(element: cx.Cursor, field_type: cx.Type) -> bool:
    """Tell if *element*, an aggregate value with braces, is the whole field.

    Braces alone do not say it.  ``(cb_t){ fa }`` is a scalar, and a
    struct literal into an array of structs fills one element of the
    array: in both cases brace elision goes on, and the next element is
    not the next field.  The value is the whole field when its type is
    the type of the field.
    """
    value = element
    while value.kind in _VALUE_WRAPPERS:
        children = list(value.get_children())
        if not children:
            return False
        value = children[-1]
    if value.kind != cx.CursorKind.INIT_LIST_EXPR and value.kind not in _LIST_VALUES:
        return False
    try:
        return value.type.get_canonical().spelling == field_type.get_canonical().spelling
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False


def _has_member_designator(element: cx.Cursor) -> bool:
    """Tell if *element* names its field: a MEMBER_REF before the value (``.cb = f``, ``cb: f``)."""
    children = list(element.get_children())
    return any(c.kind == cx.CursorKind.MEMBER_REF for c in children[:-1])


def _is_string_literal(value: cx.Cursor) -> bool:
    """Tell if *value* is a string literal, through the wrappers of a value."""
    while value.kind in _VALUE_WRAPPERS:
        inner = list(value.get_children())
        if not inner:
            return False
        value = inner[-1]
    return value.kind == cx.CursorKind.STRING_LITERAL


def _field_entry(field_decl: cx.Cursor) -> tuple[str | None, str, str, cx.Type | None]:
    """The ``(USR, name, type, field type)`` entry of a field that can receive a function."""
    return (cursor_usr(field_decl), field_decl.spelling, _field_type_spelling(field_decl),
            field_decl.type)


def _handle_fn_ptr_cases(cursor: cx.Cursor, cur_fn: str | None,
                        refs: list[Reference], seen_ref: _SeenRefs) -> None:
    """The functions whose address an assignment, a variable initializer or an init list takes.

    * **Binary operator** — ``obj.onData = &handler``, ``global_cb = &fn``,
      also a comparison (``cb != &fn``).
    * **Variable initializer** — ``static void (*fp)(int) = &handler``.
    * **Init-list member** — ``{.on_data = &handler, .cb = &fallback}``:
      the type of the field (or of the array element) chooses a function
      of an overload set, and a positional array list gives the slot
      number of each element (a vector table).

    Each gives an ``indirect`` reference.  The slot that receives the
    function is a slot fact (``_slots.py``).
    """

    if cursor.kind == cx.CursorKind.BINARY_OPERATOR:
        _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs)
    elif cursor.kind == cx.CursorKind.VAR_DECL and _is_fn_ptr_type(cursor.type):
        _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs)
    elif cursor.kind == cx.CursorKind.INIT_LIST_EXPR:
        # libclang gives the children in source order, and for a positional
        # array list that order is the array index — so the enumeration
        # counter is the slot number.  A hole keeps its child (`{ a, 0, c }`
        # has three), which is what keeps the count aligned with the array.
        gives_slots = _init_list_gives_slots(cursor)
        # The element type of an array list: the storage of each element
        # that names no field.
        array_storage = _storage_type(cursor.type)
        # A positional struct list: the position of an element is its field.
        positional_fields = _positional_struct_fields(cursor)
        for position, child in enumerate(cursor.get_children()):
            if position < len(positional_fields):
                field_type = positional_fields[position][3]
            else:
                field_type = _init_list_element_field(child)[3]
            storage = (_storage_type(field_type) if field_type is not None else None) or array_storage
            _emit_fn_ptr_targets(child, cur_fn, seen_ref, refs, in_init_list=True,
                                 expected_type=storage,
                                 slot_index=position if gives_slots else None)


# The scopes whose variables have static storage duration and get their
# constructor before main: namespace scope, and a class for a static data
# member.  A variable of a function body is not in this set, a static local
# included: its constructor runs from the function.
_STATIC_OBJECT_SCOPES = frozenset({
    cx.CursorKind.TRANSLATION_UNIT, cx.CursorKind.NAMESPACE,
    cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL, cx.CursorKind.UNION_DECL,
    cx.CursorKind.CLASS_TEMPLATE,
})


def _static_init_constructor_calls(var: cx.Cursor) -> set[int]:
    """Give the hashes of the constructor calls that the initialization of *var* makes before main.

    *var* must be the definition of a variable at namespace scope or of a
    static data member, else the answer is empty.  libclang gives a
    CALL_EXPR for each construction, with the constructor that the compiler
    selected as its referenced cursor.  Each constructor call in the
    initializer of such a variable runs before main and from no function:
    the object itself (``Foo g;``, ``Foo g(7);``, ``Foo arr[2];``) and each
    temporary (``Foo g = Foo(1);``, ``Foo g = 5;``).  The tools read those
    edges as global constructors (``implicit_construct``, no caller).  The
    body of a lambda in the initializer runs when the lambda is called,
    thus its calls are not part of the set.

    WHY the whole initializer and not ``semantic_parent``: libclang gives
    the VAR_DECL as the semantic parent of the CALL_EXPR of ``Foo g;``, but
    no semantic parent for the CALL_EXPR of a temporary or a conversion.

    WHY not from the declaration: an earlier pass wrote an edge from each
    VAR_DECL and FIELD_DECL of class type to EVERY constructor of the
    class.  That gave the overloads that the compiler did not select, an
    edge for an extern declaration, and an edge for a member declaration,
    which constructs nothing at its own line.

    A local object, a static local, a new-expression and a member
    initializer construct from the function that holds them, thus they stay
    calls.
    """
    calls: set[int] = set()
    try:
        if not var.is_definition() or var.semantic_parent is None:
            return calls
        if var.semantic_parent.kind not in _STATIC_OBJECT_SCOPES:
            return calls
        pending = list(var.get_children())
        while pending:
            node = pending.pop()
            if node.kind == cx.CursorKind.LAMBDA_EXPR:
                continue
            if node.kind == cx.CursorKind.CALL_EXPR:
                target = node.referenced
                if target is not None and target.kind == cx.CursorKind.CONSTRUCTOR:
                    calls.add(node.hash)
            pending.extend(node.get_children())
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("static init: walk failed for %s", var.spelling)
    return calls


@dataclass
class ExtractionResult:
    """Result of a single extract_all() call on one translation unit."""
    tu: cx.TranslationUnit | None = None
    symbols: list = field(default_factory=list)
    references: list = field(default_factory=list)
    inheritance: list = field(default_factory=list)
    macros: list = field(default_factory=list)
    # (derived USR, base USR) of each virtual method that a method of the
    # unit overrides, from the compiler (indexer/_overrides.py).
    overrides: list[tuple[str, str]] = field(default_factory=list)
    # The slot facts of the indirect calls (indexer/_slots.py), or None
    # when the references are not extracted.
    slots: SlotFacts | None = None
    # The place of each function with internal linkage that the unit names,
    # by its USR in this language (indexer/_internal_usrs.py).
    internal_decls: dict[str, InternalDecl] = field(default_factory=dict)
    newly_seen_files: set[str] = field(default_factory=set)
    # The error diagnostics of the parse (see _parse_diagnostics).
    parse_errors: int = 0
    parse_fatal: bool = False
    parse_first_error: str = ""


def _parse_diagnostics(tu: cx.TranslationUnit) -> tuple[int, bool, str]:
    """Give the count of the error diagnostics of *tu*, whether one is fatal, and the first one.

    WHY the index keeps them: a parse with errors can hide code.  After a
    fatal error ("too many errors") clang stops and instantiates no
    template, thus calls and symbols are missing from the index.  On 4 Mbed
    projects that once cost 76 to 216 units each, and nothing told the user
    (plans of 2026-10-05).  A non-fatal error is reported too: the parse goes
    on, but a new one can be the first sign of a wrong flag or header.

    The first error carries the name and the line of its file, not the
    whole path: the text is for a person who reads the status of a build.
    """
    errors = [d for d in tu.diagnostics if d.severity >= cx.Diagnostic.Error]
    if not errors:
        return 0, False, ""
    fatal = any(d.severity == cx.Diagnostic.Fatal for d in errors)
    first = errors[0]
    where = ""
    if first.location.file:
        where = f" ({Path(first.location.file.name).name}:{first.location.line})"
    return len(errors), fatal, f"{first.spelling}{where}"[:300]

def extract_all(
    unit: CompilationUnit,
    with_refs: bool = False,
    *,
    return_tu: bool = False,
    skip_files: set[str] | frozenset[str] | None = None,
) -> ExtractionResult:
    """Parse one translation unit and return extracted symbols, references, and inheritance.

    This is the single-pass entry point for all data extraction from a
    ``CompilationUnit``.  It parses the file with libclang, walks the AST
    once, and produces multiple outputs simultaneously.

    When *skip_files* is a non-empty ``set[str]``, subtrees rooted in
    already-processed header files are skipped during all AST walks
    (symbols, references, macros, anon-field mapping, and content fill).
    This eliminates 91–97 % of duplicate work for headers already
    visited by earlier translation units in the same indexing run.

    No filtering is done at the extraction level — all symbols and references
    from all included files are extracted.  Filtering (project vs vendor)
    happens downstream in ``store_symbols_for_unit`` via ``is_project``.

    Args:
        unit: The translation unit to parse (file path + compiler flags).
        with_refs: When True, also extract call, reference, and member-access
            edges, and the slot facts of the indirect calls (also of the
            bodies of the template instances).  Inheritance is always
            extracted regardless of this flag.
        skip_files: Optional set of resolved file paths whose subtrees
            should be skipped.  Passed through to all internal walkers.
            When ``None`` or empty, the native ``walk_preorder`` is used
            (faster for the first TU with an empty skip set).

    Returns:
        An ``ExtractionResult`` with symbols, references, inheritance,
        macros, the slot facts of the indirect calls,
        and newly_seen_files (the set of all source file paths encountered
        during the symbol walk — used to accumulate the skip set for
        subsequent translation units).
    """

    import logging
    import time as _time
    _log = logging.getLogger(__name__)
    _t_start = _time.monotonic()
    # The lambdas of an earlier unit are no lambdas of this one.
    _SEEN_LAMBDAS.by_closure = {}
    tu = _get_index().parse(
        str(unit.file),
        args=unit.clang_args,
        options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    _t_parse = _time.monotonic() - _t_start
    parse_errors, parse_fatal, parse_first_error = _parse_diagnostics(tu)

    cwd = unit.directory

    # maxsize=None — the cache lives only for this extract_all() call
    # (one TU).  Even 10 000 unique header paths (~2 MB) is negligible,
    # and unbounded cache avoids eviction churn during the AST walk.
    @cache
    def _resolve(path: str) -> Path:
        p = Path(path)
        return (cwd / p).resolve() if not p.is_absolute() else p.resolve()

    # Convert mutable set to frozen for hash-consistency across call sites.
    # The mutable set lives in runner.py; each extract_all call gets a
    # stable frozen view that cannot be accidentally mutated.
    _skip_frozen: frozenset[str] | None = (
        frozenset(skip_files) if skip_files else None
    )

    # --- Symbols ---
    symbols: list[Symbol] = []
    seen_usrs: dict[str, bool] = {}  # USR → is_definition (allows decl→def promotion)
    class_cursors: list[cx.Cursor] = []  # class/struct def cursors for inheritance extraction
    # The virtual methods of the walk, also of a class template.
    virtual_methods: list[cx.Cursor] = []

    # Pre-scan: map anonymous struct/union USRs to their field names
    # (e.g. struct { ... } _radio_cmd;  →  anon_usr → "_radio_cmd")
    anon_usr_to_field = _build_anon_usr_to_field(
        tu.cursor, skip_files=_skip_frozen, resolve_fn=_resolve,
    )

    tu_path_str = str(_resolve(tu.spelling))

    # Collect every source file path seen during the symbol walk.
    # Includes ALL files, not just those with _SYMBOL_KINDS cursors —
    # a header with only forward declarations still contributes to the
    # skip set so subsequent TUs can skip its subtree entirely.
    _newly_seen: set[str] = set()

    if _skip_frozen:
        _skipper = _iter_cursor_skip(tu.cursor, _skip_frozen, _resolve)
    else:
        _skipper = tu.cursor.walk_preorder()

    for cursor in _skipper:
        if cursor.location.file:
            fpath = str(_resolve(str(cursor.location.file.name)))
            _newly_seen.add(fpath)

        if cursor.kind == cx.CursorKind.LAMBDA_EXPR:
            _remember_lambda(cursor)
            lambda_symbol = _lambda_symbol(cursor)
            if lambda_symbol is not None and lambda_symbol.usr not in seen_usrs:
                seen_usrs[lambda_symbol.usr] = True
                symbols.append(lambda_symbol)
            continue
        if cursor.kind not in _SYMBOL_KINDS:
            continue
        if cursor.kind in VIRTUAL_METHOD_KINDS and cursor.is_virtual_method():
            virtual_methods.append(cursor)
        _process_one_symbol(cursor, symbols, seen_usrs, class_cursors, anon_usr_to_field, _log)

    # Weakness is a fact of the object file: any declaration of the TU,
    # also one in a header that the walk above skipped, can make it.
    weak = _weak_usrs(tu.cursor)
    for symbol in symbols:
        if symbol.usr in weak:
            symbol.is_weak = True

    _t_symwalk = _time.monotonic() - _t_start - _t_parse  # subtract parse time

    # --- Inheritance: examine base specifiers of collected class cursors ---
    inheritance = _extract_inheritance(class_cursors)
    # --- Overrides: the record of the compiler for each virtual method ---
    overrides = extract_overrides(virtual_methods, _code_usr)

    # --- Macro definitions (#define) ---
    macros = _extract_macros(tu.cursor, _resolve, skip_files=_skip_frozen)

    if not with_refs:
        _log.info("  parse=%.1fs symwalk=%.1fs syms=%d macros=%d", _t_parse, _t_symwalk, len(symbols), len(macros))
        result = ExtractionResult(
            tu=tu if return_tu else None, symbols=symbols, references=[],
            inheritance=inheritance, macros=macros, overrides=overrides, newly_seen_files=_newly_seen,
            parse_errors=parse_errors, parse_fatal=parse_fatal, parse_first_error=parse_first_error,
        )
        return result

    refs, instances, internal_decls = _build_refs(
        tu, tu_path_str, _resolve, anon_usr_to_field, _log,
        skip_files=_skip_frozen,
    )
    from ._slots import collect_slot_facts

    slots = collect_slot_facts(tu, _skip_frozen, _resolve, instances)

    _t_total = _time.monotonic() - _t_start
    _t_refwalk = _t_total - _t_parse - _t_symwalk
    _log.debug(
        "  parse=%.1fs symwalk=%.1fs refwalk=%.1fs syms=%d refs=%d macros=%d",
        _t_parse, _t_symwalk, _t_refwalk, len(symbols), len(refs), len(macros),
    )
    result = ExtractionResult(
        tu=tu if return_tu else None, symbols=symbols, references=refs,
        inheritance=inheritance, macros=macros, overrides=overrides,
        slots=slots, internal_decls=internal_decls, newly_seen_files=_newly_seen,
        parse_errors=parse_errors, parse_fatal=parse_fatal, parse_first_error=parse_first_error,
    )
    return result
