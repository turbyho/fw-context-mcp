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
* **Indirect call sites** — locations where a function-pointer field or
  variable is invoked (``driver.onData(buf, len)``).
* **FnPointerAssignment** — links an assignment site (``obj.onData = &handler``)
  to the assigned function, storing the helper-pointer typedef for Phase 3
  linking of assignments to call sites.
* **PendingDispatch** — deferred dispatch-registration patterns (e.g.
  ``EventQueue::call_every``) for synthetic ``ref_kind='dispatch'`` edges.

**Key design decisions:**

* **Single TU walk** — ``extract_all`` parses once and walks the AST once.
  Symbols and references are extracted in separate sub-walks over the same
  cursor stream, trading memory for speed (no re-parse).

* **Skip-files optimisation** — when a header was already visited by an
  earlier TU, its subtree is skipped during subsequent walks.
  ``_iter_cursor_skip`` uses a manual stack (instead of native
  ``walk_preorder``) to support subtree omission.  The native method is ~3×
  faster and is used when *skip_files* is empty.

* **Three-phase function-pointer resolution** — Phase 1 detects assignments
  (``_emit_fn_ptr_targets``).  Phase 2 records indirect call sites
  (``_handle_indirect_invocations``).  Phase 3 links assignments to call
  sites via the ``fn_ptr_type`` column (performed downstream in the
  database layer).  Arguments to dispatch-registration APIs are additionally
  captured via ``PendingDispatch``.

* **Source-line fallback** — embedded C++ (mbed-os, Zephyr) relies heavily on
  templates.  ``callback(&Class::method, this)`` and ``_timeout.attach(...)``
  often produce ``UNEXPOSED_EXPR`` or empty CALL_EXPR cursors that libclang
  cannot resolve.  ``_run_source_line_fallback`` scans raw source text inside
  known function bodies and emits the missing call edges via regex matching.

* **Conservative disambiguation** — ``_resolve_method_usr`` returns ``None``
  when candidate methods are ambiguous rather than guessing.  A wrong edge
  is worse than a missing edge (previously ``_timeout.attach(...)`` was
  mis-resolved to ``SerialBase::attach``).

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

import clang.cindex as cx

from ._dispatch_bridges import _DISPATCH_ENTRY_POINTS, _DISPATCH_METHOD_NAMES, _TYPE_ERASED_ISR_FUNCTIONS
from .compile_commands import CompilationUnit
from .models import (
    FnPointerAssignment,
    IndirectCallSite,
    InheritanceRecord,
    Macro,
    PendingDispatch,
    Reference,
    Symbol,
)

_log = logging.getLogger(__name__)

_INDEX: cx.Index | None = None
_index_lock = threading.Lock()


def _get_index() -> cx.Index:
    """Return the libclang Index singleton.

    ``cx.Index.create()`` is lightweight and Index objects internally
    serialize concurrent ``parse()`` calls, so a single shared instance
    is sufficient.
    """
    global _INDEX
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
    """True when *cursor* is an anonymous (unnamed) struct or union.

    libclang reports the spelling as ``""`` for some compilers or as
    ``"struct (unnamed at file:line)"`` / ``"union (anonymous at file:line)"``
    on others.  Both are treated as anonymous.
    """
    if cursor.kind not in (cx.CursorKind.STRUCT_DECL, cx.CursorKind.UNION_DECL):
        return False
    spelling = cursor.spelling
    if not spelling:
        return True
    if "(unnamed" in spelling or "(anonymous" in spelling:
        return True
    return False


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
                    child_usr = child.get_usr()
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
    anonymous marker (e.g. ``"_ble_cmd"`` instead of
    ``"struct (unnamed at ...)"``).

    Returns a string like ``"namespace::Class::method"``, or an empty
    string for the translation unit root.
    """
    parts: list[str] = []
    c = cursor
    while c and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        spelling = c.spelling
        usr = c.get_usr() if anon_map else None
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
) -> list[cx.Cursor]:
    """Recursively extract function/method declarations referenced inside an expression.

    Depth-limited to prevent stack overflow on deeply nested C++ expressions.
    """
    _FN_REF_MAX_DEPTH = 500
    if _depth > _FN_REF_MAX_DEPTH:
        _log.warning("_find_fn_refs_in_expr: max depth %d exceeded", _FN_REF_MAX_DEPTH)
        return []

    results: list[cx.Cursor] = []

    # Direct reference to a callable (bare function name, method ref, etc.)
    if cursor.kind in (cx.CursorKind.DECL_REF_EXPR, cx.CursorKind.MEMBER_REF_EXPR):
        ref = cursor.referenced
        if ref is not None and ref.kind in _INDIRECT_TARGET_KINDS:
            # Skip if this is the callee of the enclosing function call
            if _skip_usr and ref.get_usr() == _skip_usr:
                return results
            loc = ref.location
            if loc.file:
                results.append(ref)
        return results

    # Address-of operator (&) — peel and recurse into the operand
    if cursor.kind == cx.CursorKind.UNARY_OPERATOR:
        for child in cursor.get_children():
            results.extend(_find_fn_refs_in_expr(child, _skip_usr, _depth + 1))
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
            nested_callee_usr = nested_callee.get_usr()
            if nested_callee_usr:
                loc = nested_callee.location
                if loc.file:
                    nested_skip = nested_callee_usr
        for child in cursor.get_children():
            results.extend(_find_fn_refs_in_expr(child, nested_skip, _depth + 1))
        return results

    # Default: recurse into all children (handles implicit casts, parentheses, etc.)
    for child in cursor.get_children():
        results.extend(_find_fn_refs_in_expr(child, _skip_usr, _depth + 1))

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


def _call_expr_text(cursor: cx.Cursor) -> str:
    """Extract callee expression text from a CALL_EXPR's token stream.

    Returns everything before the opening ``(``, space-joined.
    ``driver->onData(buf, len)`` → ``"driver->onData"``
    ``stored_callback(42)``      → ``"stored_callback"``
    """
    parts: list[str] = []
    try:
        for tok in cursor.get_tokens():
            if tok.spelling == "(":
                break
            parts.append(tok.spelling)
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("_call_expr_text: get_tokens failed")
        return ""
    return " ".join(parts)


def _extract_lhs_field(
    expr_cursor: cx.Cursor,
) -> tuple[str | None, str]:
    """Extract the USR and name of the field or variable on the LHS.

    ``obj->onData = &handler`` returns ``(USR_FIELD_onData, "onData")``.
    ``global_cb = &handler`` returns ``(USR_VAR_global_cb, "global_cb")``.

    Walks children recursively so it works through ``UNEXPOSED_EXPR``
    wrappers (common in designated initializers like ``.field = &fn``
    inside ``INIT_LIST_EXPR``).

    Returns ``(None, "")`` when the LHS is not a recognizable field
    or variable.
    """
    def _walk(c: cx.Cursor) -> tuple[str | None, str]:
        if c.kind in (cx.CursorKind.MEMBER_REF_EXPR, cx.CursorKind.MEMBER_REF):
            ref = c.referenced
            if ref is not None and ref.kind == cx.CursorKind.FIELD_DECL:
                return (ref.get_usr(), ref.spelling)
        elif c.kind == cx.CursorKind.DECL_REF_EXPR:
            ref = c.referenced
            if ref is not None and ref.kind in (
                cx.CursorKind.VAR_DECL, cx.CursorKind.PARM_DECL,
            ):
                return (ref.get_usr(), ref.spelling)
        # Recurse into UNEXPOSED_EXPR and other wrappers
        for grandchild in c.get_children():
            result = _walk(grandchild)
            if result[0] is not None:
                return result
        return (None, "")

    return _walk(expr_cursor)


def _process_one_base_specifier(
    child: cx.Cursor,
    cls_usr: str,
    cls_spelling: str,
    access_map: dict[cx.AccessSpecifier, str],
) -> InheritanceRecord | None:
    """Build an InheritanceRecord from one CXX_BASE_SPECIFIER child cursor.

    Returns ``None`` for non-base-specifier cursors or when the base class
    cannot be resolved (e.g. forward-declared template base with no
    instantiation visible in this TU).

    Calls ``clang_isVirtualBase`` directly through the C-API to detect
    virtual inheritance — libclang does not expose a Python wrapper for
    this query.
    """
    if child.kind != cx.CursorKind.CXX_BASE_SPECIFIER:
        return None
    base_ref = child.referenced
    if base_ref is None:
        return None
    base_usr = base_ref.get_usr()
    if not base_usr:
        return None
    base_loc = base_ref.location
    if not base_loc.file:
        return None
    access = access_map.get(child.access_specifier, "public")
    try:
        from clang.cindex import conf as _ciconf
        is_virt = bool(_ciconf.lib.clang_isVirtualBase(child))
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("clang_isVirtualBase failed for %s", cls_spelling)
        is_virt = False
    return InheritanceRecord(
        derived_usr=cls_usr,
        base_usr=base_usr,
        access=access,
        is_virtual=is_virt,
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
        cls_usr = cls_cursor.get_usr()
        if not cls_usr:
            continue
        try:
            for child in cls_cursor.get_children():
                rec = _process_one_base_specifier(child, cls_usr, cls_cursor.spelling, _access_map)
                if rec is not None:
                    inheritance.append(rec)
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("base class traversal failed for %s", cls_cursor.spelling)
            continue
    return inheritance


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

        try:
            is_fn_like = child.is_macro_function_like()
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("is_macro_function_like failed for %s", child.spelling)
            is_fn_like = False

        value = ""
        try:
            tokens = list(child.get_tokens())
            if len(tokens) > 1:
                value = " ".join(t.spelling for t in tokens[1:])
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("macro token extraction failed for %s", child.spelling)
            value = ""

        macros.append(Macro(
            name=child.spelling,
            value=value,
            line=loc.line,
            is_function_like=is_fn_like,
            file=fpath,
        ))
    return macros


def _class_match_score(hint: str, class_part: str) -> int:
    """Score how well a field-name hint matches a class name.

    Multi-level matching designed for the fallback path where field
    names use snake_case (``_ble_msg_manager``) but class names use
    CamelCase (``BleMsgManager``), or where the field name is a
    superset of the class name (``_uart_driver`` → ``UART_DRIVER``).

    Returns 0 = no match, 1 = weak (token-level), 2 = good.
    """
    if not hint or not class_part:
        return 0
    class_lower = class_part.lower()

    if hint in class_lower:
        return 2
    if class_lower in hint:
        return 1

    hint_flat = hint.replace("_", "")
    if hint_flat and hint_flat in class_lower:
        return 2
    if hint_flat and class_lower in hint_flat:
        return 1

    hint_tokens = [t for t in hint.split("_") if t]
    if not hint_tokens:
        return 0
    match_count = sum(1 for t in hint_tokens if t in class_lower)
    if match_count == len(hint_tokens):
        return 2
    if match_count > 0:
        return 1

    return 0


def _resolve_partial_qn(partial: str, qn_to_usr: dict[str, str]) -> str | None:
    """Resolve a possibly-partial qualified name (e.g. ``WDT::_timeout_interrupt``)
    against *qn_to_usr* by exact match, then by ``::<partial>`` suffix match."""
    usr = qn_to_usr.get(partial)
    if usr:
        return usr
    suffix = f"::{partial}"
    for qn, u in qn_to_usr.items():
        if qn.endswith(suffix):
            return u
    return None


def _resolve_method_usr(
    method_name: str,
    qn_to_usr: dict[str, str],
    field_name: str = "",
    caller_qn: str = "",
) -> str | None:
    """Find USR for *method_name* in *qn_to_usr*.

    Disambiguation order (mirrors C++ scoping rules):

    1. Exact qualified name match.
    2. Single ``::method_name`` suffix candidate.
    3. Receiver-class hint from *field_name* — for ``obj.method()`` the
       receiver's type is authoritative, so a field-name match wins over
       the caller's class.
    4. Caller-class match — bare ``method()`` calls inside a class method
       resolve to sibling methods of the enclosing class (fixes
       ``zbox_reset()`` inside ``WDT::swdt_check``).
    5. Ambiguous → ``None``.  Never emit a possibly-wrong edge: previously
       an arbitrary first candidate was returned, which mis-resolved
       ``_timeout.attach(...)`` to ``mbed::SerialBase::attach``.
    """
    usr = qn_to_usr.get(method_name)
    if usr:
        return usr
    suffix = f"::{method_name}"
    candidates: list[tuple[str, str]] = [
        (qn, u) for qn, u in qn_to_usr.items() if qn.endswith(suffix)
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]
    if field_name:
        _hint = field_name.lstrip("_").lower()
        _scored: list[tuple[int, str, str]] = []
        for qn, u in candidates:
            _class_part = qn.rsplit("::", 2)[-2] if "::" in qn else ""
            _score = _class_match_score(_hint, _class_part)
            _scored.append((_score, qn, u))
        _scored.sort(key=lambda x: -x[0])
        if _scored[0][0] > 0:
            return _scored[0][2]
    if caller_qn:
        _caller_class = caller_qn.rsplit("::", 1)[0]
        if _caller_class:
            for qn, u in candidates:
                if qn.rsplit("::", 1)[0] == _caller_class:
                    return u
    return None


def _emit_fn_ptr_targets(
    expr_cursor: cx.Cursor,
    caller_usr: str | None,
    seen_ref: set,
    refs: list[Reference],
    fp_assignments: list[FnPointerAssignment],
    skip_usr: str | None = None,
    lhs_usr: str | None = None,
    lhs_name: str = "",
    method: str = "assignment",
    qn_to_usr: dict[str, str] | None = None,
) -> None:
    """Emit indirect refs and FnPointerAssignment records for function pointer assignments.

    Walks the children of *expr_cursor* looking for function declarations
    (including those nested inside unary operators and casts), then records:
    - An indirect reference via one of the *refs* / *seen_ref* lists.
    - An ``FnPointerAssignment`` when *lhs_usr* is provided (Phase 3 linking).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    loc = expr_cursor.location
    if not loc.file:
        return

    # Pre-scan: for assignment/init_list, extract fn_ptr_type from the
    # LHS field/var (the storage location), not the RHS function.
    # This ensures fp_assignments.fn_ptr_type uses the function-pointer
    # typedef (e.g., nrfx_power_usb_event_handler_t), matching the type
    # stored in indirect_call_sites and enabling the Phase 3 USR join.
    lhs_fp_type = ""
    if method in ("assignment", "init_list") and lhs_name:
        for _c in expr_cursor.get_children():
            _lhs_usr, _lhs_name = _extract_lhs_field(_c)
            if _lhs_name and _lhs_name == lhs_name:
                try:
                    lhs_fp_type = _c.type.spelling
                except (ValueError, TypeError, RuntimeError, AttributeError):
                    pass
                break

    for child in expr_cursor.get_children():
        targets = _find_fn_refs_in_expr(child, skip_usr)
        if not targets:
            continue
        for target in targets:
            target_usr = target.get_usr()
            if target_usr == skip_usr:
                continue
            target_loc = target.location
            if target_loc.file:
                key = (target_usr, loc.file.name, loc.line, caller_usr, "indirect")
                if key not in seen_ref:
                    seen_ref.add(key)
                    refs.append(Reference(
                        to_usr=target_usr,
                        from_file=loc.file.name,
                        from_line=loc.line,
                        from_usr=caller_usr,
                        ref_kind="indirect",
                    ))
                if (lhs_usr and lhs_usr != target_usr) or lhs_name:
                    try:
                        _fp_type = lhs_fp_type or child.type.spelling
                    except (ValueError, TypeError, RuntimeError, AttributeError):
                        _log.debug(
                            "_emit_fn_ptr_targets: type.spelling failed for %s",
                            target.spelling,
                        )
                        _fp_type = ""
                    fp_assignments.append(FnPointerAssignment(
                        from_file=loc.file.name,
                        from_line=loc.line,
                        lhs_usr=lhs_usr or "",
                        lhs_name=lhs_name,
                        rhs_usr=target_usr,
                        rhs_name=target.spelling,
                        fn_ptr_type=_fp_type,
                        method=method,
                        from_usr=caller_usr,
                    ))

    # Token fallback: when _find_fn_refs_in_expr found no targets in any child
    # (e.g. UNEXPOSED_EXPR wrapping a member function pointer inside a CALL_EXPR
    # like callback(&Class::method)), try token-based extraction on the whole expr.
    # This is the last-resort fallback — the libclang AST is opaque but the raw
    # token stream still contains the ``&Class::method`` pattern.  We scan for
    # it via _extract_fn_refs_from_unexposed which does not depend on AST
    # structure, only on a linear token sequence.
    if qn_to_usr is not None:
        any_found = False
        for child in expr_cursor.get_children():
            if _find_fn_refs_in_expr(child, skip_usr):
                any_found = True
                break
        if not any_found:
            pairs = _extract_fn_refs_from_unexposed(expr_cursor, qn_to_usr)
            for target_usr, rhs_name in pairs:
                if target_usr == skip_usr:
                    continue
                key = (target_usr, loc.file.name, loc.line, caller_usr, "indirect")
                if key not in seen_ref:
                    seen_ref.add(key)
                    refs.append(Reference(
                        to_usr=target_usr,
                        from_file=loc.file.name,
                        from_line=loc.line,
                        from_usr=caller_usr,
                        ref_kind="indirect",
                    ))
                if (lhs_usr and lhs_usr != target_usr) or lhs_name:
                    fp_assignments.append(FnPointerAssignment(
                        from_file=loc.file.name,
                        from_line=loc.line,
                        lhs_usr=lhs_usr or "",
                        lhs_name=lhs_name,
                        rhs_usr=target_usr,
                        rhs_name=rhs_name,
                        fn_ptr_type="",
                        method=method,
                        from_usr=caller_usr,
                    ))
def _build_refs_and_fp_assignments(
    tu: cx.TranslationUnit,
    tu_path_str: str,
    symbols: list[Symbol],
    resolve_fn,
    anon_usr_to_field: dict[str, str],
    _log,
    skip_files: frozenset[str] | None = None,
) -> tuple[list[Reference], list[IndirectCallSite], list[FnPointerAssignment], list[PendingDispatch]]:
    """Walk the TU AST extracting references, indirect call sites, and function pointer assignments.

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

    **Phase 2 — source-line fallback**: after the AST walk,
    :func:`_run_source_line_fallback` scans raw source text inside known
    function spans and emits call/indirect edges via regex matching.
    This catches ``obj.method()`` and ``&Class::method`` patterns that
    template-obscured AST cursors cannot resolve.

    Returns (refs, indirect_call_sites, fp_assignments, pending_dispatches).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    _callable_kind_strs = frozenset({"function", "method", "constructor", "destructor"})
    qn_to_usr: dict[str, str] = {}
    for s in symbols:
        if s.qualified_name and s.kind in _callable_kind_strs:
            qn_to_usr[s.qualified_name] = s.usr
    usr_to_qn: dict[str, str] = {u: q for q, u in qn_to_usr.items()}

    refs: list[Reference] = []
    indirect_call_sites: list[IndirectCallSite] = []
    fp_assignments: list[FnPointerAssignment] = []
    pending_dispatches: list[PendingDispatch] = []
    seen_ref: set[tuple] = set()
    _fn_spans: list[tuple[str, int, int]] = []

    fn_stack: list[tuple[str, int, str]] = []

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

        if cursor.kind in _CALLABLE_KINDS and cursor.is_definition():
            own_usr = cursor.get_usr()
            if own_usr:
                cur_fn = own_usr
            try:
                _ext = cursor.extent
                if _ext.start.file:
                    _ext_file = _ext.start.file.name
                    _resolved_file = str(resolve_fn(_ext_file))
                    if _resolved_file == tu_path_str:
                        _fn_spans.append((cur_fn or '', _ext.start.line, _ext.end.line))
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

        _process_ref_cursor(
            cursor, cur_fn, refs, indirect_call_sites, fp_assignments,
            pending_dispatches, seen_ref, qn_to_usr, usr_to_qn, tu_path_str, resolve_fn, _log,
        )

    # Source-line fallback for template-obscured method calls
    _run_source_line_fallback(
        tu, refs, fp_assignments, pending_dispatches, seen_ref, _fn_spans, qn_to_usr, usr_to_qn, _log,
    )

    return refs, indirect_call_sites, fp_assignments, pending_dispatches


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

    is_def = cursor.is_definition()
    if not is_def and cursor.kind not in _DECL_KINDS:
        return False

    usr = cursor.get_usr()
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
        parent_usr = sem_parent.get_usr() or ""

    if not parent_usr and kind in ("varlocal", "variable"):
        if sem_parent and sem_parent.kind in _CALLABLE_KINDS:
            parent_usr = sem_parent.get_usr() or ""

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
        if cursor.kind in _template_check_kinds:
            try:
                specialized = cursor.specialized_template
                if specialized is not None:
                    template_usr = specialized.get_usr() or ""
                if not template_usr:
                    parent = cursor.semantic_parent
                    if parent is not None:
                        parent_spec = parent.specialized_template
                        if parent_spec is not None:
                            template_usr = parent_spec.get_usr() or ""
                if not template_usr:
                    decl = cursor.type.get_declaration()
                    if decl is not None and decl != cursor:
                        decl_spec = decl.specialized_template
                        if decl_spec is not None:
                            template_usr = decl_spec.get_usr() or ""
            except (ValueError, TypeError, RuntimeError, AttributeError):
                _log.debug("specialized_template failed for %s", cursor.spelling)

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
    ))

    if cursor.kind in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL) and is_def:
        class_cursors.append(cursor)

    return True


def _process_ref_cursor(
    cursor: cx.Cursor,
    cur_fn: str | None,
    refs: list[Reference],
    indirect_call_sites: list[IndirectCallSite],
    fp_assignments: list[FnPointerAssignment],
    pending_dispatches: list[PendingDispatch],
    seen_ref: set[tuple],
    qn_to_usr: dict[str, str],
    usr_to_qn: dict[str, str],
    tu_path_str: str,
    resolve_fn: Callable[..., object],
    _log: logging.Logger,
) -> None:
    """Process a single cursor for references and indirect calls — dispatcher."""
    loc = cursor.location
    if not loc.file:
        return

    _handle_direct_refs(cursor, cur_fn, refs, seen_ref, tu_path_str, resolve_fn)
    _handle_indirect_invocations(cursor, cur_fn, refs, indirect_call_sites,
                                 fp_assignments, pending_dispatches, seen_ref, tu_path_str, resolve_fn,
                                 qn_to_usr, _log)
    _handle_fn_ptr_cases(cursor, cur_fn, refs, fp_assignments, seen_ref, tu_path_str, _log)
    _handle_implicit_constructors(cursor, cur_fn, refs, seen_ref, _log)
    _handle_token_fallbacks(cursor, cur_fn, refs, fp_assignments, seen_ref, qn_to_usr, usr_to_qn, tu_path_str, _log)


def _add_ref(refs: list[Reference], seen_ref: set[tuple], to_usr: str,
             from_file: str, from_line: int, from_usr: str | None,
             ref_kind: str) -> bool:
    """Deduplicated append to refs list."""
    key = (to_usr, from_file, from_line, from_usr, ref_kind)
    if key not in seen_ref:
        seen_ref.add(key)
        refs.append(Reference(to_usr=to_usr, from_file=from_file, from_line=from_line,
                              from_usr=from_usr, ref_kind=ref_kind))
        return True
    return False


def _callee_has_file(referenced: cx.Cursor | None) -> bool:
    """Check a referenced cursor has a valid file location."""
    if referenced is None:
        return False
    try:
        rl = referenced.location
        return rl is not None and rl.file is not None
    except (ValueError, TypeError, RuntimeError, AttributeError):
        return False


def _handle_direct_refs(cursor: cx.Cursor, cur_fn: str | None, refs: list[Reference],
                       seen_ref: set[tuple], tu_path_str: str,
                       resolve_fn: Callable[..., object]) -> None:
    """Direct call/ref/member references + field-access fallback + constructor fallback."""
    loc = cursor.location
    ref_kind = _REF_KINDS.get(cursor.kind)
    if ref_kind is not None:
        referenced = cursor.referenced
        if referenced is not None:
            to_usr = referenced.get_usr()
            ref_loc = referenced.location
            if to_usr and ref_loc.file:
                _add_ref(refs, seen_ref, to_usr, loc.file.name, loc.line, cur_fn, ref_kind)

        if cursor.kind == cx.CursorKind.CALL_EXPR:
            _handle_field_call_fallback(cursor, cur_fn, refs, seen_ref, tu_path_str)
            _handle_constructor_fallback(cursor, cur_fn, refs, seen_ref, tu_path_str, resolve_fn)


def _handle_field_call_fallback(cursor: cx.Cursor, cur_fn: str | None,
                                refs: list[Reference], seen_ref: set[tuple],
                                tu_path_str: str) -> None:
    """Field-access call fallback: obj.method() where method is not directly resolved."""
    loc = cursor.location
    if _callee_has_file(cursor.referenced):
        return
    for child in cursor.get_children():
        if child.kind == cx.CursorKind.MEMBER_REF_EXPR:
            child_ref = child.referenced
            if child_ref is not None and child_ref.kind in _CALLABLE_KINDS:
                child_usr = child_ref.get_usr()
                child_loc = child_ref.location
                if child_usr and child_loc.file:
                    _add_ref(refs, seen_ref, child_usr, loc.file.name, loc.line, cur_fn, "call")
                    return


def _collect_ctor_refs_from_type(
    child_type: cx.Type, loc_file: str, loc_line: int, cur_fn: str | None,
    refs: list[Reference], seen_ref: set[tuple],
) -> None:
    """Emit a ``call`` reference for every constructor of a RECORD type.

    Called from ``_handle_constructor_fallback`` when a variable or field
    declaration has a class type whose constructor call was not resolved
    by libclang (common with implicit/default constructors).

    Iterates all child constructors of the class — the specific overload
    is not distinguished; each constructor gets a call reference at the
    declaration site.
    """
    if child_type.kind != cx.TypeKind.RECORD:
        return
    try:
        class_cursor = child_type.get_declaration()
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("get_declaration failed for record type")
        return
    if class_cursor is None:
        return
    for ctor in class_cursor.get_children():
        if ctor.kind != cx.CursorKind.CONSTRUCTOR:
            continue
        ctor_usr = ctor.get_usr()
        if not ctor_usr:
            continue
        ctor_loc = ctor.location
        if not ctor_loc.file:
            continue
        _add_ref(refs, seen_ref, ctor_usr, loc_file, loc_line, cur_fn, "call")


def _handle_constructor_fallback(cursor: cx.Cursor, cur_fn: str | None,
                                 refs: list[Reference], seen_ref: set[tuple],
                                 tu_path_str: str,
                                 resolve_fn: Callable[..., object]) -> None:
    """Constructor call fallback: detecting implicit constructor invocations."""
    loc = cursor.location
    if _callee_has_file(cursor.referenced):
        return
    for child in cursor.get_children():
        if child.kind != cx.CursorKind.DECL_REF_EXPR:
            continue
        child_ref = child.referenced
        if child_ref is None:
            continue
        if child_ref.kind not in (cx.CursorKind.FIELD_DECL, cx.CursorKind.VAR_DECL):
            continue
        try:
            child_type = child_ref.type.get_canonical()
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("get_canonical failed for %s", child_ref.spelling)
            continue
        _collect_ctor_refs_from_type(
            child_type, loc.file.name, loc.line, cur_fn, refs, seen_ref,
        )


def _handle_indirect_invocations(cursor: cx.Cursor, cur_fn: str | None,
                                 refs: list[Reference],
                                 indirect_call_sites: list[IndirectCallSite],
                                 fp_assignments: list[FnPointerAssignment],
                                 pending_dispatches: list[PendingDispatch],
                                 seen_ref: set[tuple], tu_path_str: str,
                                 resolve_fn: Callable[..., object],
                                 qn_to_usr: dict[str, str],
                                 _log: logging.Logger) -> None:
    """Indirect calls: function pointers invoked directly or passed as call arguments.

    Two resolution strategies for indirect invocations:

    1. **Direct callee** — ``cursor.referenced`` resolves to a FIELD_DECL,
       VAR_DECL, or PARM_DECL whose type is a function pointer.  This
       covers the common case ``driver.onData(buf, len)`` where libclang
       resolves ``onData`` directly.  An IndirectCallSite is recorded.

    2. **Callee-expression fallback** — when ``cursor.referenced`` is
       ``None`` or does not match, the first child of the CALL_EXPR is
       unwrapped (through ``UNEXPOSED_EXPR`` layers) and its type is
       checked.  This covers ``stored_callback(42)`` where the callee is
       a variable rather than a field, and template-wrapped cases.
       ``_extract_lhs_field`` walks the callee expression to extract the
       target USR and name.

    After recording the call site, ``_handle_fn_ptr_as_argument`` scans
    the call arguments for function-pointer targets passed as parameters.
    """
    if cursor.kind != cx.CursorKind.CALL_EXPR:
        return
    loc = cursor.location

    callee = cursor.referenced
    if (callee is not None
            and callee.kind in (cx.CursorKind.FIELD_DECL, cx.CursorKind.VAR_DECL, cx.CursorKind.PARM_DECL)
            and _is_fn_ptr_type(callee.type)):
        target_usr = callee.get_usr()
        if target_usr:
            indirect_call_sites.append(IndirectCallSite(
                from_file=loc.file.name, from_line=loc.line, from_usr=cur_fn,
                expr_text=_call_expr_text(cursor), target_usr=target_usr,
                target_name=callee.spelling, fn_ptr_type=callee.type.spelling,
            ))
    else:
        callee_expr = _first_child_unwrapped(cursor)
        if callee_expr is not None and _is_fn_ptr_type(callee_expr.type):
            target_usr, target_name = _extract_lhs_field(callee_expr)
            if target_usr:
                try:
                    _fn_ptr_spelling = callee_expr.type.spelling
                except (ValueError, TypeError, RuntimeError, AttributeError):
                    _log.debug("type.spelling failed for indirect call callee expr")
                    _fn_ptr_spelling = ""
                indirect_call_sites.append(IndirectCallSite(
                    from_file=loc.file.name, from_line=loc.line, from_usr=cur_fn,
                    expr_text=_call_expr_text(cursor), target_usr=target_usr,
                    target_name=target_name, fn_ptr_type=_fn_ptr_spelling,
                ))

    _handle_fn_ptr_as_argument(cursor, cur_fn, refs, fp_assignments, pending_dispatches,
                               seen_ref, tu_path_str, qn_to_usr, _log)



def _extract_fn_refs_from_unexposed(
    cursor: cx.Cursor,
    qn_to_usr: dict[str, str],
) -> list[tuple[str, str]]:
    """Extract ``(USR, rhs_name)`` pairs from UNEXPOSED_EXPR tokens.

    Parses token stream for ``&Class::method`` patterns and resolves
    them against *qn_to_usr*.  Returns ``[(usr, name), ...]`` tuples
    suitable for direct ``FnPointerAssignment`` creation.
    """
    results: list[tuple[str, str]] = []
    try:
        tokens = list(cursor.get_tokens())
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("_extract_fn_refs_from_unexposed: get_tokens failed")
        return []
    for i, tok in enumerate(tokens):
        if tok.spelling == "&" and i + 3 < len(tokens):
            t1, t2, t3 = tokens[i + 1], tokens[i + 2], tokens[i + 3]
            if (t1.kind.name == "IDENTIFIER" and t2.spelling == "::"
                    and t3.kind.name == "IDENTIFIER"):
                partial = f"{t1.spelling}::{t3.spelling}"
                target_usr = qn_to_usr.get(partial)
                if not target_usr:
                    suffix = f"::{partial}"
                    for qn, usr in qn_to_usr.items():
                        if qn.endswith(suffix):
                            target_usr = usr
                            break
                if target_usr:
                    results.append((target_usr, t3.spelling))
    return results


def _handle_fn_ptr_as_argument(cursor: cx.Cursor, cur_fn: str | None,
                               refs: list[Reference],
                               fp_assignments: list[FnPointerAssignment],
                               pending_dispatches: list[PendingDispatch],
                               seen_ref: set[tuple], tu_path_str: str,
                               qn_to_usr: dict[str, str],
                               _log: logging.Logger) -> None:
    """Function pointers passed as call arguments.

    Emits an ``indirect`` reference for every callable target found in the
    arguments.  When the callee is directly resolvable AND its parameter at
    the matching index is a function-pointer type, an ``FnPointerAssignment``
    is also recorded (Phase 3 linking).  The indirect reference is emitted
    even when the callee cannot be resolved (e.g. ``_timeout.attach(
    callback(&WDT::_timeout_interrupt), delay)`` where ``attach`` is
    template-obscured) — previously an unresolved callee meant ``callee_params``
    was empty and the callback target was silently dropped.

    Additionally detects dispatch-registration patterns (e.g. EventQueue::
    call_every with a callback target) and records ``PendingDispatch`` records
    for deferred resolution into ``ref_kind='dispatch'`` synthetic edges.
    """
    loc = cursor.location
    direct_callee = cursor.referenced
    direct_callee_usr = direct_callee.get_usr() if direct_callee else None

    # ── Fallback callee resolution via callee expression ────────────────
    # When cursor.referenced is None (template-obscured), try to resolve
    # the callee through the first child of the CALL_EXPR (the callee expression).
    # E.g. _timeout.attach(...) where attach is a template — the CALL_EXPR
    # doesn't resolve it, but the MEMBER_REF_EXPR child might.
    callee_expr_text: str = ""
    if direct_callee is None:
        callee_expr = _first_child_unwrapped(cursor)
        callee_expr_text = _call_expr_text(cursor) if callee_expr else ""
        if callee_expr is not None:
            if callee_expr.kind == cx.CursorKind.MEMBER_REF_EXPR:
                for child in callee_expr.get_children():
                    if child.kind in _CALLABLE_KINDS:
                        direct_callee = child
                        break
            elif callee_expr.referenced is not None:
                direct_callee = callee_expr.referenced

    # ── Dispatch API detection ──────────────────────────────────────────
    _callee_dispatch_entry_qn: str | None = None
    if direct_callee is not None:
        _callee_qn = _qualified_name(direct_callee)
        if _callee_qn in _DISPATCH_ENTRY_POINTS:
            _callee_dispatch_entry_qn = _DISPATCH_ENTRY_POINTS[_callee_qn]
    # ── End dispatch API detection ──────────────────────────────────────
    callee_params: list = []
    if direct_callee is not None:
        try:
            callee_params = list(direct_callee.get_arguments())
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("get_arguments failed for %s", direct_callee.spelling)
            callee_params = []
    callee_args = list(cursor.get_arguments())

    def _param_info(i: int, arg: cx.Cursor | None = None) -> tuple[str | None, str]:
        """Return (param_usr, fn_ptr_type_spelling) for argument index *i*.

        When the callee parameter is a template type (e.g. ``F &&func``),
        falls back to the argument expression's type — so factory wrappers
        like ``callback(&func)`` are detected via their return type
        (e.g. ``Callback<void()>``).
        """
        if i >= len(callee_params):
            return None, ""
        try:
            param = callee_params[i]
        except (ValueError, TypeError, RuntimeError, AttributeError):
            return None, ""
        param_usr: str | None = None
        _fp_type: str = ""
        if _is_fn_ptr_type(param.type):
            param_usr = param.get_usr()
            if param_usr:
                try:
                    _fp_type = param.type.spelling
                except (ValueError, TypeError, RuntimeError, AttributeError):
                    _fp_type = ""
                return param_usr, _fp_type
        if arg is not None:
            try:
                arg_type = arg.type.get_canonical()
                if _is_fn_ptr_type(arg_type):
                    _fp_type = arg_type.spelling
                    return None, _fp_type
            except (ValueError, TypeError, RuntimeError, AttributeError):
                pass
        return None, ""

    for i, arg in enumerate(callee_args):
        targets = _find_fn_refs_in_expr(arg, direct_callee_usr)
        # Fallback: token-based extraction for opaque args
        # (UNEXPOSED_EXPR, unresolved CALL_EXPR like callback(...), etc.)
        if not targets:
            unexposed_pairs = _extract_fn_refs_from_unexposed(arg, qn_to_usr)
            if unexposed_pairs:
                param_usr, _fp_type = _param_info(i, arg)
                for target_usr, rhs_name in unexposed_pairs:
                    if target_usr != direct_callee_usr:
                        _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "indirect")
                        fp_assignments.append(FnPointerAssignment(
                            from_file=loc.file.name, from_line=loc.line,
                            lhs_usr=param_usr or "",
                            lhs_name=callee_expr_text or rhs_name,
                            rhs_usr=target_usr, rhs_name=rhs_name,
                            fn_ptr_type=_fp_type, method="call_arg",
                            from_usr=cur_fn,
                        ))
                        if _callee_dispatch_entry_qn is not None:
                            pending_dispatches.append(PendingDispatch(
                                callee_qn=_callee_qn,
                                target_qn_partial="",
                                target_name=rhs_name,
                                target_usr=target_usr,
                                file=tu_path_str,
                                line=loc.line,
                                caller_usr=cur_fn,
                            ))
        if targets:
            param_usr, _fp_type = _param_info(i, arg)
            for target in targets:
                target_usr = target.get_usr()
                if target_usr and target_usr != direct_callee_usr:
                    _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "indirect")
                    _lhs_name = callee_expr_text or target.spelling
                    fp_assignments.append(FnPointerAssignment(
                        from_file=loc.file.name, from_line=loc.line,
                        lhs_usr=param_usr or "", lhs_name=_lhs_name,
                        rhs_usr=target_usr, rhs_name=target.spelling,
                        fn_ptr_type=_fp_type, method="call_arg", from_usr=cur_fn,
                    ))
                    if _callee_dispatch_entry_qn is not None:
                        pending_dispatches.append(PendingDispatch(
                            callee_qn=_callee_qn,
                            target_qn_partial=_qualified_name(target),
                            target_name=target.spelling,
                            target_usr=target_usr,
                            file=tu_path_str,
                            line=loc.line,
                            caller_usr=cur_fn,
                        ))
    _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs, fp_assignments, direct_callee_usr, qn_to_usr=qn_to_usr)


def _handle_fn_ptr_cases(cursor: cx.Cursor, cur_fn: str | None,
                        refs: list[Reference],
                        fp_assignments: list[FnPointerAssignment],
                        seen_ref: set[tuple], tu_path_str: str,
                        _log: logging.Logger) -> None:
    """Function pointers in assignments, variable initializers, and init lists.

    Three recognised patterns for function-pointer assignments:

    * **Binary assignment** — ``obj.onData = &handler`` or ``global_cb = &fn``.
      The LHS field/variable is extracted via ``_extract_lhs_field`` and
      passed to ``_emit_fn_ptr_targets`` for ``FnPointerAssignment`` creation.

    * **Variable initializer** — ``static void (*fp)(int) = &handler``.
      The variable declaration itself is the LHS; its USR and spelling
      become the assignment target for Phase 3 linking.

    * **Init-list member** — ``{.on_data = &handler, .cb = &fallback}``.
      Each init-list child that assigns a function pointer is processed
      individually, with the designated field name used as the LHS.

    Does NOT check return value of ``_emit_fn_ptr_targets`` — indirect
    references are always emitted even when no ``FnPointerAssignment`` is
    created (i.e. the assigned function has no call site yet).
    """

    if cursor.kind == cx.CursorKind.BINARY_OPERATOR:
        lhs_usr, lhs_name = _extract_lhs_field(cursor)
        _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs, fp_assignments,
                             lhs_usr=lhs_usr, lhs_name=lhs_name, method="assignment")
    elif cursor.kind == cx.CursorKind.VAR_DECL and _is_fn_ptr_type(cursor.type):
        _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs, fp_assignments,
                             lhs_usr=cursor.get_usr(), lhs_name=cursor.spelling, method="var_init")
    elif cursor.kind == cx.CursorKind.INIT_LIST_EXPR:
        for child in cursor.get_children():
            child_usr, child_name = _extract_lhs_field(child)
            _emit_fn_ptr_targets(child, cur_fn, seen_ref, refs, fp_assignments,
                                 lhs_usr=child_usr, lhs_name=child_name, method="init_list")


def _handle_implicit_constructors(cursor: cx.Cursor, cur_fn: str | None,
                                  refs: list[Reference], seen_ref: set[tuple],
                                  _log: logging.Logger) -> None:
    """Implicit constructor calls from global/static objects and member fields.

    Detects ``ref_kind='implicit_construct'`` edges: when a VAR_DECL or
    FIELD_DECL has a RECORD type (class/struct), iterates all constructors
    of that class and emits a call reference at the declaration site.
    This is necessary because libclang does not emit CALL_EXPR for implicit
    or default constructor invocations (e.g. ``Foo obj;`` or
    ``static Bar _bar;`` at file scope).

    The specific constructor overload is NOT distinguished — every
    constructor of the class gets a reference.  This is sufficient for
    call-graph dead-code detection and hotspot analysis, where the
    caller count should reflect all construction sites.
    """
    if cursor.kind not in (cx.CursorKind.VAR_DECL, cx.CursorKind.FIELD_DECL):
        return
    loc = cursor.location
    try:
        canon = cursor.type.get_canonical()
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("implicit_construct: get_canonical failed for %s", cursor.spelling)
        return
    if canon is None or canon.kind != cx.TypeKind.RECORD:
        return
    try:
        class_cursor = canon.get_declaration()
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _log.debug("implicit_construct: get_declaration failed")
        return
    if class_cursor is None:
        return
    for child in class_cursor.get_children():
        if child.kind != cx.CursorKind.CONSTRUCTOR:
            continue
        ctor_usr = child.get_usr()
        if not ctor_usr:
            continue
        ctor_loc = child.location
        if not ctor_loc.file:
            continue
        _add_ref(refs, seen_ref, ctor_usr, loc.file.name, loc.line, cur_fn, "implicit_construct")


def _handle_token_fallbacks(cursor: cx.Cursor, cur_fn: str | None,
                            refs: list[Reference],
                            fp_assignments: list[FnPointerAssignment],
                            seen_ref: set[tuple], qn_to_usr: dict[str, str],
                            usr_to_qn: dict[str, str], tu_path_str: str,
                            _log: logging.Logger) -> None:
    """Token-based fallback detection: UNEXPOSED_EXPR and template-obscured calls.

    Operates on the raw token stream, not the libclang AST — necessary when
    template expansion produces ``UNEXPOSED_EXPR`` wrappers that hide the
    actual call/ref structure.

    Three detection patterns:

    1. **UNEXPOSED_EXPR** → extracts ``&Class::method`` via
       ``_extract_fn_refs_from_unexposed`` from any opaque expression node.

    2. **Token-level obj.method()** — scans CALL_EXPR tokens for
       ``IDENTIFIER DOT/ARROW IDENTIFIER LPAREN`` sequences and resolves
       via ``_resolve_method_usr`` with the field name as hint.

    3. **Token-level bare call** — matches ``IDENTIFIER LPAREN`` where the
       identifier is not preceded by ``.`` or ``->`` (avoids double-counting
       with pattern 2).  Resolves via ``_resolve_method_usr`` with the
       enclosing class as hint.

    4. **Callback-style indirect targets** — ``&Class::method`` inside a
       CALL_EXPR argument list.  Emits ``ref_kind='indirect'`` and an
       ``FnPointerAssignment`` with ``method='call_arg'``.
    """
    loc = cursor.location
    caller_qn = usr_to_qn.get(cur_fn or "") or ""

    if cursor.kind == cx.CursorKind.UNEXPOSED_EXPR:
        pairs = _extract_fn_refs_from_unexposed(cursor, qn_to_usr)
        for _target_usr, _rhs_name in pairs:
            _add_ref(refs, seen_ref, _target_usr, loc.file.name, loc.line, cur_fn, "indirect")

    if cursor.kind == cx.CursorKind.CALL_EXPR:
        try:
            tokens = list(cursor.get_tokens())
        except (ValueError, TypeError, RuntimeError, AttributeError):
            _log.debug("_handle_token_fallbacks: get_tokens failed")
            return
        for i, tok in enumerate(tokens):
            if (tok.kind.name == "IDENTIFIER"
                    and i + 3 < len(tokens)
                    and tokens[i + 1].spelling in (".", "->")
                    and tokens[i + 2].kind.name == "IDENTIFIER"
                    and tokens[i + 3].spelling == "("):
                target_usr = _resolve_method_usr(
                    tokens[i + 2].spelling, qn_to_usr, tok.spelling, caller_qn,
                )
                if target_usr:
                    _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "call")
                else:
                    _log.debug("Token fallback: no USR for %s.%s() at %s:%d",
                               tok.spelling, tokens[i + 2].spelling, loc.file.name, loc.line)
            if (tok.kind.name == "IDENTIFIER"
                    and i + 1 < len(tokens)
                    and tokens[i + 1].spelling == "("
                    and (i == 0 or tokens[i - 1].spelling not in (".", "->"))):
                target_usr = _resolve_method_usr(tok.spelling, qn_to_usr, caller_qn=caller_qn)
                if target_usr:
                    _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "call")
        # Callback-style indirect targets: `&Class::method` passed as a call
        # argument (e.g. `_timeout.attach(callback(&WDT::_timeout_interrupt), delay)`).
        # libclang often resolves the outer call (attach) but leaves the
        # nested `&Class::method` opaque — emit the target as an indirect ref
        # so find_references / find_all_callers_recursive can see it.
        for i, tok in enumerate(tokens):
            if (tok.spelling == "&" and i + 3 < len(tokens)
                    and tokens[i + 1].kind.name == "IDENTIFIER"
                    and tokens[i + 2].spelling == "::"
                    and tokens[i + 3].kind.name == "IDENTIFIER"):
                partial = f"{tokens[i + 1].spelling}::{tokens[i + 3].spelling}"
                target_usr = _resolve_partial_qn(partial, qn_to_usr)
                if target_usr:
                    _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "indirect")
                    fp_assignments.append(FnPointerAssignment(
                        from_file=loc.file.name, from_line=loc.line,
                        lhs_usr="", lhs_name=tokens[i + 3].spelling,
                        rhs_usr=target_usr, rhs_name=tokens[i + 3].spelling,
                        fn_ptr_type="", method="call_arg",
                        from_usr=cur_fn,
                    ))

# Keywords and built-in operators that the bare-call regex in
# _run_source_line_fallback must not emit as call edges.
# A frozenset gives O(1) containment check and prevents accidental
# mutation across the module's lifetime.
_BARE_CALL_KEYWORD_DENYLIST: frozenset[str] = frozenset({
    'if', 'while', 'for', 'return', 'switch', 'catch',
    'sizeof', 'decltype', 'typeof', 'alignof', 'noexcept',
    'static_cast', 'dynamic_cast', 'const_cast', 'reinterpret_cast',
})

def _run_source_line_fallback(
    tu: cx.TranslationUnit,
    refs: list[Reference],
    fp_assignments: list[FnPointerAssignment],
    pending_dispatches: list[PendingDispatch],
    seen_ref: set,
    _fn_spans: list[tuple[str, int, int]],
    qn_to_usr: dict[str, str],
    usr_to_qn: dict[str, str],
    _log,
) -> None:
    """Post-processing: scan source lines inside known function bodies for
    template-obscured patterns and emit missing references.

    Operates in four sequential phases per source line:

    **Phase 1 — obj.method() fallback**: regex-matches ``obj.method(`` patterns
    on lines NOT already covered by AST-extracted call/indirect edges.  Uses
    ``_resolve_method_usr`` with the field name as a disambiguation hint.
    When the matched method name is in ``_DISPATCH_METHOD_NAMES``, also
    scans for ``&Class::callback`` arguments and records ``PendingDispatch``.

    **Phase 2 — bare call fallback**: matches ``function(args)`` and
    ``function<T>(args)`` patterns (excluding language keywords in
    ``_BARE_CALL_KEYWORD_DENYLIST``).  Suppresses self-references on the
    function's own definition line — the regex would otherwise match the
    declaration signature as a self-call.

    **Phase 3 — type-erased ISR registration**: detects patterns like
    ``NVIC_SetVector(IRQn, (uint32_t)handler)`` where the integer cast
    obliterates the function-pointer type.  Uses ``_TYPE_ERASED_ISR_FUNCTIONS``
    (a registry of known ISR-registration function names mapped to their
    handler-argument position) and attempts to resolve the handler name
    through both ``&Class::method`` regex and bare-name lookup.

    **Phase 4 — &Class::method on source text**: a final blanket scan for
    ``&Class::method`` patterns.  This runs on raw source text, not AST
    cursors, so it fires even when the TU is too degraded for libclang to
    produce ``CALL_EXPR`` cursors (common in embedded mbed-os builds where
    the AST-based ``_handle_token_fallbacks`` cannot see empty bodies).

    Each phase uses ``seen_ref`` for deduplication — a reference already
    found by an earlier phase or the AST walk is never re-emitted.
    """
    import re as _re

    _tu_file = tu.spelling
    _lines_with_calls: set[int] = {
        r.from_line for r in refs
        if r.ref_kind in ("call", "indirect") and r.from_file == _tu_file
    }
    # Map each function's definition start line → its USR.  Used to suppress
    # the false self-reference created when the bare-call regex matches the
    # function's OWN name in its definition signature (e.g. ``void WDT::
    # zbox_reset(`` → a self-caller edge at the definition line).
    _fn_def_lines: dict[int, str] = {
        _fn_start: _fn_usr for _fn_usr, _fn_start, _fn_end in _fn_spans
    }
    try:
        _source_text = Path(_tu_file).read_text(encoding="utf-8", errors="replace")
        _source_lines = _source_text.splitlines()
    except (ValueError, TypeError, RuntimeError, AttributeError):
        _source_lines = []
    for _lineno_0, _line in enumerate(_source_lines):
        _lineno = _lineno_0 + 1
        if _lineno in _lines_with_calls:
            continue
        _line_fn = None
        for _fn_usr, _fn_start, _fn_end in _fn_spans:
            if _fn_start <= _lineno <= _fn_end:
                _line_fn = _fn_usr
                break
        if _line_fn is None:
            continue
        _line_qn = usr_to_qn.get(_line_fn or "") or ""
        # ── Phase 1: obj.method() fallback ─────────────────────────────
        # Matches ``field.method(...)`` or ``field->method(...)`` patterns.
        # Runs only on lines NOT already covered by AST-extracted call/indirect
        # edges (checked via _lines_with_calls fast-path above).
        # _resolve_method_usr uses the field name hint for receiver-class
        # disambiguation — e.g. ``_timeout.attach(...)`` resolves to
        # ``Timeout::attach`` because the field is named ``_timeout``.
        for _m in _re.finditer(r'(\w+)(?:\.|->)(\w+)\s*\(', _line):
            _field_name = _m.group(1)
            _method_name = _m.group(2)
            _target_usr = _resolve_method_usr(_method_name, qn_to_usr, _field_name, _line_qn)
            if _target_usr:
                _key = (_target_usr, _tu_file, _lineno, _line_fn, "call")
                if _key not in seen_ref:
                    seen_ref.add(_key)
                    refs.append(Reference(
                        to_usr=_target_usr,
                        from_file=_tu_file,
                        from_line=_lineno,
                        from_usr=_line_fn,
                        ref_kind="call",
                    ))
                # ── Dispatch API detection ──────────────────────────────
                if _method_name in _DISPATCH_METHOD_NAMES:
                    # Look up the matched method's QN to check against
                    # _DISPATCH_ENTRY_POINTS (which maps callee QN → entry QN)
                    _dispatch_callee_qn = ""
                    for _dq in _DISPATCH_ENTRY_POINTS:
                        if qn_to_usr.get(_dq) == _target_usr:
                            _dispatch_callee_qn = _dq
                            break
                    if _dispatch_callee_qn:
                        # Find callback targets in &Class::method arguments
                        for _cm in _re.finditer(r'&(\w+)::(\w+)', _line):
                            _partial = f"{_cm.group(1)}::{_cm.group(2)}"
                            _cb_usr = _resolve_partial_qn(_partial, qn_to_usr)
                            if _cb_usr:
                                pending_dispatches.append(PendingDispatch(
                                    callee_qn=_dispatch_callee_qn,
                                    target_qn_partial=_partial,
                                    target_name=_cm.group(2),
                                    target_usr=_cb_usr,
                                    file=_tu_file,
                                    line=_lineno,
                                    caller_usr=_line_fn,
                                ))
            else:
                _log.debug(
                    "Source-line fallback: no USR for %s.%s() at %s:%d",
                    _field_name, _method_name, _tu_file, _lineno,
                )
        # ── Phase 2: bare call fallback ────────────────────────────────
        # Matches ``func(args)`` or ``func<T>(args)`` — direct function
        # calls without a receiver object.  Filters out C/C++ keywords via
        # _BARE_CALL_KEYWORD_DENYLIST (e.g. ``if(x)`` is not a call to
        # function ``if``).  Suppresses the self-reference false-positive:
        # a bare match of the enclosing function's own name on its
        # definition line is the declaration signature, not a recursive
        # call (e.g. ``void zbox_reset(void)`` matches ``zbox_reset(``).
        # Bare call fallback: function(args) or function<T>(args)
        for _m in _re.finditer(r'(?<![.\w])(\w+)(<[^>]+>)?\s*\(', _line):
            _method_name = _m.group(1)
            if _method_name in _BARE_CALL_KEYWORD_DENYLIST:
                continue
            _target_usr = _resolve_method_usr(_method_name, qn_to_usr, caller_qn=_line_qn)
            if not _target_usr:
                continue
            # Suppress the false self-reference: a bare match of the enclosing
            # function's own name on its definition start line is the
            # declaration signature, not a call to itself.
            if (_target_usr == _line_fn and _fn_def_lines.get(_lineno) == _line_fn):
                continue
            _key = (_target_usr, _tu_file, _lineno, _line_fn, "call")
            if _key not in seen_ref:
                seen_ref.add(_key)
                refs.append(Reference(
                    to_usr=_target_usr, from_file=_tu_file,
                    from_line=_lineno, from_usr=_line_fn, ref_kind="call",
                ))
        # ── Phase 3: Type-erased ISR registration detection ────────────
        # APIs like NVIC_SetVector(IRQn, (uint32_t)handler) where
        # libclang cannot see the fn-ptr because of the integer cast.
        # _TYPE_ERASED_ISR_FUNCTIONS maps known ISR-registration function
        # names to their handler-argument position (currently unused in
        # the detection itself — the handler is found by regex, not by
        # argument indexing).  Two sub-strategies for handler resolution:
        #   a) ``&Class::method`` pattern in the argument text.
        #   b) Bare-name lookup of the last identifier in the arg list
        #      (after stripping parenthesized sub-expressions).
        for _isr_fn, _arg_pos in _TYPE_ERASED_ISR_FUNCTIONS.items():
            for _m in _re.finditer(rf'\b{_isr_fn}\s*\(', _line):
                _call_start = _m.end()
                _depth = 0
                _call_end = _call_start
                for _i in range(_call_start, len(_line)):
                    if _line[_i] == '(':
                        _depth += 1
                    elif _line[_i] == ')':
                        if _depth == 0:
                            _call_end = _i
                            break
                        _depth -= 1
                _args_text = _line[_call_start:_call_end]
                _isr_cm = _re.search(r'&(\w+)::(\w+)', _args_text)
                if _isr_cm:
                    _partial = f"{_isr_cm.group(1)}::{_isr_cm.group(2)}"
                    _handler_usr = _resolve_partial_qn(_partial, qn_to_usr)
                    _handler_name = _isr_cm.group(2)
                else:
                    _cleaned = _re.sub(r'\([^)]*\)', ' ', _args_text)
                    _ids = _re.findall(r'\b([A-Za-z_]\w*)\b', _cleaned)
                    if len(_ids) < 2:
                        continue
                    _handler_name = _ids[-1]
                    _handler_usr = _resolve_method_usr(
                        _handler_name, qn_to_usr, caller_qn=_line_qn,
                    )
                if not _handler_usr:
                    continue
                _key = (_handler_usr, _tu_file, _lineno, _line_fn, "indirect")
                if _key not in seen_ref:
                    seen_ref.add(_key)
                    refs.append(Reference(
                        to_usr=_handler_usr, from_file=_tu_file,
                        from_line=_lineno, from_usr=_line_fn,
                        ref_kind="indirect",
                    ))
                    fp_assignments.append(FnPointerAssignment(
                        from_file=_tu_file, from_line=_lineno,
                        lhs_usr="", lhs_name=_isr_fn,
                        rhs_usr=_handler_usr, rhs_name=_handler_name,
                        fn_ptr_type="", method="isr_vec",
                        from_usr=_line_fn,
                    ))
        # ── Phase 4: &Class::method on source text ─────────────────────
        # Blanket scan for ``&Class::method`` patterns on every source
        # line inside a known function body.  This runs on raw text, not
        # AST cursors — when a TU is too degraded for libclang to produce
        # CALL_EXPR cursors (embedded mbed-os builds with unrecoverable
        # template errors), the AST-based _handle_token_fallbacks cannot
        # see the empty body, but the source text still has the patterns.
        for _m in _re.finditer(r'&(\w+)::(\w+)', _line):
            _partial = f"{_m.group(1)}::{_m.group(2)}"
            _target_usr = _resolve_partial_qn(_partial, qn_to_usr)
            if not _target_usr:
                continue
            _key = (_target_usr, _tu_file, _lineno, _line_fn, "indirect")
            if _key not in seen_ref:
                seen_ref.add(_key)
                refs.append(Reference(
                    to_usr=_target_usr, from_file=_tu_file,
                    from_line=_lineno, from_usr=_line_fn, ref_kind="indirect",
                ))
                fp_assignments.append(FnPointerAssignment(
                    from_file=_tu_file, from_line=_lineno,
                    lhs_usr="", lhs_name=_m.group(2),
                    rhs_usr=_target_usr, rhs_name=_m.group(2),
                    fn_ptr_type="", method="call_arg",
                    from_usr=_line_fn,
                ))





@dataclass
class ExtractionResult:
    """Result of a single extract_all() call on one translation unit."""
    tu: cx.TranslationUnit | None = None
    symbols: list = field(default_factory=list)
    references: list = field(default_factory=list)
    inheritance: list = field(default_factory=list)
    indirect_call_sites: list = field(default_factory=list)
    fp_assignments: list = field(default_factory=list)
    macros: list = field(default_factory=list)
    pending_dispatches: list = field(default_factory=list)
    newly_seen_files: set[str] = field(default_factory=set)

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
            edges and embed a source-line token fallback pass for
            template-obscured expressions.  Inheritance is always extracted
            regardless of this flag.
        skip_files: Optional set of resolved file paths whose subtrees
            should be skipped.  Passed through to all internal walkers.
            When ``None`` or empty, the native ``walk_preorder`` is used
            (faster for the first TU with an empty skip set).

    Returns:
        An ``ExtractionResult`` with symbols, references, inheritance,
        indirect_call_sites, fp_assignments, macros, pending_dispatches,
        and newly_seen_files (the set of all source file paths encountered
        during the symbol walk — used to accumulate the skip set for
        subsequent translation units).
    """

    import logging
    import time as _time
    _log = logging.getLogger(__name__)
    _t_start = _time.monotonic()
    tu = _get_index().parse(
        str(unit.file),
        args=unit.clang_args,
        options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    _t_parse = _time.monotonic() - _t_start

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

    # Pre-scan: map anonymous struct/union USRs to their field names
    # (e.g. struct { ... } _ble_cmd;  →  anon_usr → "_ble_cmd")
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

        if cursor.kind not in _SYMBOL_KINDS:
            continue
        _process_one_symbol(cursor, symbols, seen_usrs, class_cursors, anon_usr_to_field, _log)

    _t_symwalk = _time.monotonic() - _t_start - _t_parse  # subtract parse time

    # --- Inheritance: examine base specifiers of collected class cursors ---
    inheritance = _extract_inheritance(class_cursors)

    # --- Macro definitions (#define) ---
    macros = _extract_macros(tu.cursor, _resolve, skip_files=_skip_frozen)

    if not with_refs:
        _log.info("  parse=%.1fs symwalk=%.1fs syms=%d macros=%d", _t_parse, _t_symwalk, len(symbols), len(macros))
        result = ExtractionResult(
            tu=tu if return_tu else None, symbols=symbols, references=[],
            inheritance=inheritance, indirect_call_sites=[], fp_assignments=[],
            macros=macros, newly_seen_files=_newly_seen,
        )
        return result

    refs, indirect_call_sites, fp_assignments, pending_dispatches = _build_refs_and_fp_assignments(
        tu, tu_path_str, symbols, _resolve, anon_usr_to_field, _log,
        skip_files=_skip_frozen,
    )

    _t_total = _time.monotonic() - _t_start
    _t_refwalk = _t_total - _t_parse - _t_symwalk
    _log.debug(
        "  parse=%.1fs symwalk=%.1fs refwalk=%.1fs syms=%d refs=%d macros=%d",
        _t_parse, _t_symwalk, _t_refwalk, len(symbols), len(refs), len(macros),
    )
    result = ExtractionResult(
        tu=tu if return_tu else None, symbols=symbols, references=refs,
        inheritance=inheritance, indirect_call_sites=indirect_call_sites,
        fp_assignments=fp_assignments, macros=macros,
        pending_dispatches=pending_dispatches, newly_seen_files=_newly_seen,
    )
    return result
