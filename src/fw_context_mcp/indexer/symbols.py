"""Extract symbols, references, and inheritance edges from a CompilationUnit using libclang."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import clang.cindex as cx

from .compile_commands import CompilationUnit

import threading
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

# Kinds where we index declarations even without a definition
_DECL_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
})

# Callable definitions that establish an "enclosing function" for references
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
    cx.CursorKind.TEMPLATE_REF: "template_ref",
}

# Cursor kinds that are valid targets for an indirect call (function pointers)
_INDIRECT_TARGET_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
})


from .models import (
    FnPointerAssignment,
    IndirectCallSite,
    InheritanceRecord,
    Macro,
    Reference,
    Symbol,
)



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


def _build_anon_usr_to_field(tu_cursor: cx.Cursor) -> dict[str, str]:
    """Build a mapping from anonymous struct/union USR → enclosing field name.

    Walks struct/union/class/namespace bodies (skipping function bodies)
    and for each FIELD_DECL whose immediate child is an anonymous struct
    or union, records the anon USR → field name association.

    This is used as a pre-scan before symbol extraction so that anonymous
    structs defined as ``struct { ... } _payload;`` get indexed with the
    field name (``_payload``) instead of ``"(unnamed struct at ...)"``.
    """
    mapping: dict[str, str] = {}

    def _walk(cursor: cx.Cursor) -> None:
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
        except Exception:
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
    except Exception:
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
    except Exception:
        _log.debug("_end_line: extent failed for %s", cursor.spelling)
        pass
    return 0


def _docstring(cursor: cx.Cursor) -> str:
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
    # When the nested callee is itself a known function (not a callback
    # wrapper like mbed::callback), propagate its USR as _skip_usr so its
    # own callee-reference children are not emitted as indirect edges.
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


def _is_fn_ptr_type(t: cx.Type) -> bool:
    """True when *t* is (or resolves via typedef to) a function pointer."""
    try:
        canon = t.get_canonical()
        if canon.kind == cx.TypeKind.POINTER:
            pointee = canon.get_pointee()
            return pointee.kind in (cx.TypeKind.FUNCTIONPROTO, cx.TypeKind.FUNCTIONNOPROTO)
    except Exception:
        _log.debug("_is_fn_ptr_type: get_canonical failed")
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
    except Exception:
        _log.debug("_first_child_unwrapped: get_children failed")
        return None
    if not children:
        return None
    first = children[0]
    while first.kind == cx.CursorKind.UNEXPOSED_EXPR:
        try:
            grandkids = list(first.get_children())
        except Exception:
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
    except Exception:
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
    except Exception:
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
        except Exception:
            _log.debug("base class traversal failed for %s", cls_cursor.spelling)
            continue
    return inheritance


def _extract_macros(tu_cursor: cx.Cursor, resolve_fn) -> list[Macro]:
    """Extract ``#define`` macro definitions from a translation unit cursor."""
    macros: list[Macro] = []
    for child in tu_cursor.get_children():
        if child.kind != cx.CursorKind.MACRO_DEFINITION:
            continue
        loc = child.location
        if not loc.file:
            continue

        try:
            is_fn_like = child.is_macro_function_like()
        except Exception:
            _log.debug("is_macro_function_like failed for %s", child.spelling)
            is_fn_like = False

        value = ""
        try:
            tokens = list(child.get_tokens())
            if len(tokens) > 1:
                value = " ".join(t.spelling for t in tokens[1:])
        except Exception:
            _log.debug("macro token extraction failed for %s", child.spelling)
            value = ""

        macros.append(Macro(
            name=child.spelling,
            value=value,
            line=loc.line,
            is_function_like=is_fn_like,
            file=str(resolve_fn(loc.file.name)),
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


def _resolve_method_usr(
    method_name: str,
    qn_to_usr: dict[str, str],
    field_name: str = "",
) -> str | None:
    """Find USR for *method_name* in *qn_to_usr*, preferring classes matching *field_name*."""
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
    return candidates[0][1]


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
                if lhs_usr and lhs_usr != target_usr:
                    try:
                        _fp_type = child.type.spelling
                    except Exception:
                        _log.debug(
                            "_emit_fn_ptr_targets: type.spelling failed for %s",
                            target.spelling,
                        )
                        _fp_type = ""
                    fp_assignments.append(FnPointerAssignment(
                        from_file=loc.file.name,
                        from_line=loc.line,
                        lhs_usr=lhs_usr,
                        lhs_name=lhs_name,
                        rhs_usr=target_usr,
                        rhs_name=target.spelling,
                        fn_ptr_type=_fp_type,
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
) -> tuple[list[Reference], list[IndirectCallSite], list[FnPointerAssignment]]:
    """Walk the TU AST extracting references, indirect call sites, and function pointer assignments.

    Returns (refs, indirect_call_sites, fp_assignments).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    _callable_kind_strs = frozenset({"function", "method", "constructor", "destructor"})
    qn_to_usr: dict[str, str] = {}
    for s in symbols:
        if s.qualified_name and s.kind in _callable_kind_strs:
            qn_to_usr[s.qualified_name] = s.usr

    refs: list[Reference] = []
    indirect_call_sites: list[IndirectCallSite] = []
    fp_assignments: list[FnPointerAssignment] = []
    seen_ref: set[tuple] = set()
    _fn_spans: list[tuple[str, int, int]] = []

    fn_stack: list[tuple[str, int, str]] = []

    for cursor in tu.cursor.walk_preorder():
        _cl = cursor.location
        _cl_file = str(Path(_cl.file.name).resolve()) if _cl and _cl.file else None
        _cl_line = _cl.line if _cl else -1
        while fn_stack and fn_stack[-1][2] and _cl_file and fn_stack[-1][2] != _cl_file:
            fn_stack.pop()
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
                    if str(resolve_fn(_ext_file)) == tu_path_str:
                        _fn_spans.append((cur_fn or '', _ext.start.line, _ext.end.line))
                        fn_stack.append((cur_fn or '', _ext.end.line, tu_path_str))
                    else:
                        fn_stack.append((cur_fn or '', 0, ""))
                else:
                    fn_stack.append((cur_fn or '', 0, ""))
            except Exception:
                _log.debug(
                    "cursor.extent failed for %s",
                    cursor.spelling,
                )
                fn_stack.append((cur_fn or '', 0, ""))

        _process_ref_cursor(
            cursor, cur_fn, refs, indirect_call_sites, fp_assignments,
            seen_ref, qn_to_usr, tu_path_str, resolve_fn, _log,
        )

    # Source-line fallback for template-obscured method calls
    _run_source_line_fallback(
        tu, refs, seen_ref, _fn_spans, qn_to_usr, _log,
    )

    return refs, indirect_call_sites, fp_assignments


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
        except Exception:
            _log.debug("enum_value failed for %s at %s:%d", cursor.spelling, loc.file.name, loc.line)

    is_virtual = bool(cursor.is_virtual_method()) if cursor.kind in (cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR) else False
    is_pure = bool(cursor.is_pure_virtual_method()) if cursor.kind in (cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR) else False

    sem_parent = cursor.semantic_parent
    kind = _cursor_kind_label(cursor.kind)
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
        if cursor.kind in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL, cx.CursorKind.FUNCTION_DECL):
            try:
                specialized = cursor.specialized_template
                if specialized is not None:
                    template_usr = specialized.get_usr() or ""
            except Exception:
                _log.debug("specialized_template failed for %s", cursor.spelling)

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
    seen_ref: set,
    qn_to_usr: dict[str, str],
    tu_path_str: str,
    resolve_fn,
    _log,
) -> None:
    """Process a single cursor for references and indirect calls — dispatcher."""
    loc = cursor.location
    if not loc.file:
        return

    _handle_direct_refs(cursor, cur_fn, refs, seen_ref, tu_path_str, resolve_fn)
    _handle_indirect_invocations(cursor, cur_fn, refs, indirect_call_sites,
                                 fp_assignments, seen_ref, tu_path_str, resolve_fn, _log)
    _handle_fn_ptr_cases(cursor, cur_fn, refs, fp_assignments, seen_ref, tu_path_str, _log)
    _handle_implicit_constructors(cursor, cur_fn, refs, seen_ref, _log)
    _handle_token_fallbacks(cursor, cur_fn, refs, seen_ref, qn_to_usr, tu_path_str, _log)


def _add_ref(refs, seen_ref, to_usr, from_file, from_line, from_usr, ref_kind):
    """Deduplicated append to refs list."""
    key = (to_usr, from_file, from_line, from_usr, ref_kind)
    if key not in seen_ref:
        seen_ref.add(key)
        refs.append(Reference(to_usr=to_usr, from_file=from_file, from_line=from_line,
                              from_usr=from_usr, ref_kind=ref_kind))
        return True
    return False


def _callee_has_file(referenced) -> bool:
    """Check a referenced cursor has a valid file location."""
    if referenced is None:
        return False
    try:
        rl = referenced.location
        return rl is not None and rl.file is not None
    except Exception:
        return False


def _handle_direct_refs(cursor, cur_fn, refs, seen_ref, tu_path_str, resolve_fn):
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


def _handle_field_call_fallback(cursor, cur_fn, refs, seen_ref, tu_path_str):
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
    child_type, loc_file: str, loc_line: int, cur_fn: str,
    refs: list[Reference], seen_ref: set[tuple],
) -> None:
    if child_type.kind != cx.TypeKind.RECORD:
        return
    try:
        class_cursor = child_type.get_declaration()
    except Exception:
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


def _handle_constructor_fallback(cursor, cur_fn, refs, seen_ref, tu_path_str, resolve_fn):
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
        except Exception:
            _log.debug("get_canonical failed for %s", child_ref.spelling)
            continue
        _collect_ctor_refs_from_type(
            child_type, loc.file.name, loc.line, cur_fn, refs, seen_ref,
        )


def _handle_indirect_invocations(cursor, cur_fn, refs, indirect_call_sites,
                                 fp_assignments, seen_ref, tu_path_str, resolve_fn, _log):
    """Indirect calls: function pointers invoked directly or passed as call arguments."""
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
                except Exception:
                    _log.debug("type.spelling failed for indirect call callee expr")
                    _fn_ptr_spelling = ""
                indirect_call_sites.append(IndirectCallSite(
                    from_file=loc.file.name, from_line=loc.line, from_usr=cur_fn,
                    expr_text=_call_expr_text(cursor), target_usr=target_usr,
                    target_name=target_name, fn_ptr_type=_fn_ptr_spelling,
                ))

    _handle_fn_ptr_as_argument(cursor, cur_fn, refs, fp_assignments, seen_ref, tu_path_str, _log)


def _handle_fn_ptr_as_argument(cursor, cur_fn, refs, fp_assignments, seen_ref, tu_path_str, _log):
    """Function pointers passed as call arguments."""
    loc = cursor.location
    direct_callee = cursor.referenced
    direct_callee_usr = direct_callee.get_usr() if direct_callee else None
    if direct_callee is not None:
        try:
            callee_params = list(direct_callee.get_arguments())
        except Exception:
            _log.debug("get_arguments failed for %s", direct_callee.spelling)
            callee_params = []
        callee_args = list(cursor.get_arguments())
        for i, arg in enumerate(callee_args):
            targets = _find_fn_refs_in_expr(arg, direct_callee_usr)
            if targets and i < len(callee_params):
                param = callee_params[i]
                if _is_fn_ptr_type(param.type):
                    param_usr = param.get_usr()
                    if param_usr:
                        for target in targets:
                            target_usr = target.get_usr()
                            if target_usr and target_usr != direct_callee_usr:
                                try:
                                    _fp_type = param.type.spelling
                                except Exception:
                                    _log.debug("param type.spelling failed for %s", param.spelling)
                                    _fp_type = ""
                                fp_assignments.append(FnPointerAssignment(
                                    from_file=loc.file.name, from_line=loc.line,
                                    lhs_usr=param_usr, lhs_name=param.spelling,
                                    rhs_usr=target_usr, rhs_name=target.spelling,
                                    fn_ptr_type=_fp_type, method="call_arg", from_usr=cur_fn,
                                ))
    _emit_fn_ptr_targets(cursor, cur_fn, seen_ref, refs, fp_assignments, direct_callee_usr)


def _handle_fn_ptr_cases(cursor, cur_fn, refs, fp_assignments, seen_ref, tu_path_str, _log):
    """Function pointers in assignments, variable initializers, and init lists."""

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


def _handle_implicit_constructors(cursor, cur_fn, refs, seen_ref, _log):
    """Implicit constructor calls from global/static objects and member fields."""
    if cursor.kind not in (cx.CursorKind.VAR_DECL, cx.CursorKind.FIELD_DECL):
        return
    loc = cursor.location
    try:
        canon = cursor.type.get_canonical()
    except Exception:
        _log.debug("implicit_construct: get_canonical failed for %s", cursor.spelling)
        return
    if canon is None or canon.kind != cx.TypeKind.RECORD:
        return
    try:
        class_cursor = canon.get_declaration()
    except Exception:
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


def _handle_token_fallbacks(cursor, cur_fn, refs, seen_ref, qn_to_usr, tu_path_str, _log):
    """Token-based fallback detection: UNEXPOSED_EXPR and template-obscured calls."""
    loc = cursor.location

    if cursor.kind == cx.CursorKind.UNEXPOSED_EXPR:
        tokens = list(cursor.get_tokens())
        for i, tok in enumerate(tokens):
            if tok.spelling == "&" and i + 3 < len(tokens):
                t1, t2, t3 = tokens[i + 1], tokens[i + 2], tokens[i + 3]
                if t1.kind.name == "IDENTIFIER" and t2.spelling == "::" and t3.kind.name == "IDENTIFIER":
                    partial = f"{t1.spelling}::{t3.spelling}"
                    target_usr = qn_to_usr.get(partial)
                    if not target_usr:
                        suffix = f"::{partial}"
                        for qn, usr in qn_to_usr.items():
                            if qn.endswith(suffix):
                                target_usr = usr
                                break
                    if target_usr:
                        _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "indirect")

    if cursor.kind == cx.CursorKind.CALL_EXPR:
        tokens = list(cursor.get_tokens())
        for i, tok in enumerate(tokens):
            if (tok.kind.name == "IDENTIFIER"
                    and i + 3 < len(tokens)
                    and tokens[i + 1].spelling == "."
                    and tokens[i + 2].kind.name == "IDENTIFIER"
                    and tokens[i + 3].spelling == "("):
                target_usr = _resolve_method_usr(tokens[i + 2].spelling, qn_to_usr, tok.spelling)
                if target_usr:
                    _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "call")
                else:
                    _log.debug("Token fallback: no USR for %s.%s() at %s:%d",
                               tok.spelling, tokens[i + 2].spelling, loc.file.name, loc.line)
            if (tok.kind.name == "IDENTIFIER"
                    and i + 1 < len(tokens)
                    and tokens[i + 1].spelling == "("
                    and (i == 0 or tokens[i - 1].spelling != ".")):
                target_usr = _resolve_method_usr(tok.spelling, qn_to_usr)
                if target_usr:
                    _add_ref(refs, seen_ref, target_usr, loc.file.name, loc.line, cur_fn, "call")


def _run_source_line_fallback(
    tu: cx.TranslationUnit,
    refs: list[Reference],
    seen_ref: set,
    _fn_spans: list[tuple[str, int, int]],
    qn_to_usr: dict[str, str],
    _log,
) -> None:
    """Post-processing: scan source lines inside known function bodies for
    template-obscured ``obj.method(`` patterns and emit missing references."""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    import re as _re

    _tu_file = tu.spelling
    _lines_with_calls: set[int] = {
        r.from_line for r in refs
        if r.ref_kind in ("call", "indirect") and r.from_file == _tu_file
    }
    try:
        _source_text = Path(_tu_file).read_text(encoding="utf-8", errors="replace")
        _source_lines = _source_text.splitlines()
    except Exception:
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
        for _m in _re.finditer(r'(\w+)\.(\w+)\s*\(', _line):
            _field_name = _m.group(1)
            _method_name = _m.group(2)
            _target_usr = _resolve_method_usr(_method_name, qn_to_usr, _field_name)
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
            else:
                _log.debug(
                    "Source-line fallback: no USR for %s.%s() at %s:%d",
                    _field_name, _method_name, _tu_file, _lineno,
                )


def extract_all(
    unit: CompilationUnit,
    with_refs: bool = False,
    *,
    return_tu: bool = False,
) -> tuple:
    """Parse one translation unit and return extracted symbols, references, and inheritance.

    This is the single-pass entry point for all data extraction from a
    ``CompilationUnit``.  It parses the file with libclang, walks the AST
    once, and produces five outputs simultaneously.

    No filtering is done at the extraction level — all symbols and references
    from all included files are extracted.  Filtering (project vs vendor)
    happens downstream in ``store_symbols_for_unit`` via ``is_project``.

    Args:
        unit: The translation unit to parse (file path + compiler flags).
        with_refs: When True, also extract call, reference, and member-access
            edges and embed a source-line token fallback pass for
            template-obscured expressions.  Inheritance is always extracted
            regardless of this flag.

    Returns:
        A tuple ``(symbols, references, inheritance, indirect_call_sites, fp_assignments, macros)``:
            symbols — all declarations and definitions from all included files.
            references — when ``with_refs`` is True, all call/ref/member/indirect
                edges; empty list otherwise.
            inheritance — C++ base class edges for class/struct definitions.
            indirect_call_sites — when ``with_refs`` is True, call sites
                where a function pointer field or variable is invoked
                (``obj->onData(args)``, ``stored_callback(args)``);
                empty list otherwise.
            fp_assignments — when ``with_refs`` is True, function pointer
                assignments (``field = &fn``, ``cb(&fn)`` param flow,
                ``void (*fp)(...) = &fn``) recording both the left-hand
                field/variable/parameter and the right-hand function;
                enables Phase 3 linking of assignment sites to call sites.
            macros — ``#define`` macro definitions found in the
                translation unit, always extracted.
                ``expanded_value`` is empty at this stage; populated later
                by the ``clang -dM -E`` driver.
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

    @lru_cache(maxsize=4096)
    def _resolve(path: str) -> Path:
        p = Path(path)
        return (cwd / p).resolve() if not p.is_absolute() else p.resolve()

    # --- Symbols ---
    symbols: list[Symbol] = []
    seen_usrs: dict[str, bool] = {}  # USR → is_definition (allows decl→def promotion)
    class_cursors: list[cx.Cursor] = []  # class/struct def cursors for inheritance extraction

    # Pre-scan: map anonymous struct/union USRs to their field names
    # (e.g. struct { ... } _ble_cmd;  →  anon_usr → "_ble_cmd")
    anon_usr_to_field = _build_anon_usr_to_field(tu.cursor)

    tu_path_str = str(_resolve(tu.spelling))

    for cursor in tu.cursor.walk_preorder():
        if cursor.kind not in _SYMBOL_KINDS:
            continue
        _process_one_symbol(cursor, symbols, seen_usrs, class_cursors, anon_usr_to_field, _log)

    _t_symwalk = _time.monotonic() - _t_start - _t_parse  # subtract parse time

    # --- Inheritance: examine base specifiers of collected class cursors ---
    inheritance = _extract_inheritance(class_cursors)

    # --- Macro definitions (#define) ---
    macros = _extract_macros(tu.cursor, _resolve)

    if not with_refs:
        _log.info("  parse=%.1fs symwalk=%.1fs syms=%d macros=%d", _t_parse, _t_symwalk, len(symbols), len(macros))
        return (tu, symbols, [], inheritance, [], [], macros) if return_tu else (symbols, [], inheritance, [], [], macros)

    refs, indirect_call_sites, fp_assignments = _build_refs_and_fp_assignments(
        tu, tu_path_str, symbols, _resolve, anon_usr_to_field, _log,
    )

    _t_total = _time.monotonic() - _t_start
    _t_refwalk = _t_total - _t_parse - _t_symwalk
    _log.debug(
        "  parse=%.1fs symwalk=%.1fs refwalk=%.1fs syms=%d refs=%d macros=%d",
        _t_parse, _t_symwalk, _t_refwalk, len(symbols), len(refs), len(macros),
    )
    return (tu, symbols, refs, inheritance, indirect_call_sites, fp_assignments, macros) if return_tu else (symbols, refs, inheritance, indirect_call_sites, fp_assignments, macros)
