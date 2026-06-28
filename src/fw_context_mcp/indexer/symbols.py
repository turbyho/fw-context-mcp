"""Extract symbols, references, and inheritance edges from a CompilationUnit using libclang."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import clang.cindex as cx

from .compile_commands import CompilationUnit

_INDEX: cx.Index | None = None
_index_lock = None


def _get_index() -> cx.Index:
    """Return the libclang Index singleton.

    ``cx.Index.create()`` is lightweight and Index objects internally
    serialize concurrent ``parse()`` calls, so a single shared instance
    is sufficient.
    """
    global _INDEX, _index_lock
    import threading
    if _index_lock is None:
        _index_lock = threading.Lock()
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


@dataclass
class Reference:
    """An edge from a call or reference site to the symbol it refers to.

    Attributes:
        to_usr: USR of the referenced definition (links to ``Symbol.usr``).
        from_file: Absolute path of the file containing the reference.
        from_line: Source line of the reference expression.
        from_usr: USR of the enclosing function or method (the caller), or
            None when the reference appears at file scope.
        ref_kind: Classification of the reference — ``"call"`` for direct
            function calls, ``"ref"`` for variable/enum reads, ``"member"``
            for member accesses, ``"indirect"`` for function pointers found
            in call arguments, assignments, variable initializers, or
            struct/array init lists.
    """
    to_usr: str        # USR of the referenced definition (links to Symbol.usr)
    from_file: str     # file containing the reference (absolute, as clang reports)
    from_line: int
    from_usr: str | None   # USR of the enclosing function/method (caller), or None
    ref_kind: str      # "call" | "ref" | "member" | "indirect"


@dataclass
class InheritanceRecord:
    """A C++ inheritance edge: ``class Derived : public Base { ... }``."""
    derived_usr: str   # USR of the derived class (child)
    base_usr: str      # USR of the base class (parent)
    access: str        # "public", "protected", or "private"
    is_virtual: bool   # True for virtual inheritance


@dataclass
class IndirectCallSite:
    """A call site where a function pointer is invoked through a field or variable.

    Unlike ``Reference`` (which points to a resolved FUNCTION_DECL), this
    records the FIELD_DECL or VAR_DECL that holds the function pointer —
    the actual function called depends on runtime state.

    Attributes:
        from_file: Absolute path of the file containing the call.
        from_line: Source line of the call expression.
        from_usr: USR of the enclosing function or method, or None.
        expr_text: Callee expression text, e.g. ``"driver.onData"`` or
            ``"stored_callback"``.
        target_usr: USR of the function pointer field or variable being
            called (the FIELD_DECL / VAR_DECL, not the target function).
        target_name: Display name of the field or variable, e.g. ``"onData"``.
        fn_ptr_type: Function pointer type signature string,
            e.g. ``"void (*)(uint8_t *, size_t)"``.
    """
    from_file: str
    from_line: int
    from_usr: str | None
    expr_text: str
    target_usr: str
    target_name: str
    fn_ptr_type: str


@dataclass
class FnPointerAssignment:
    """A function assigned to a function pointer field, variable, or parameter.

    Records both sides of ``field = &function`` and ``register(&function)``
    patterns.  The *lhs_usr* links to the FIELD_DECL, VAR_DECL, or PARM_DECL
    that receives the function pointer; *rhs_usr* links to the FUNCTION_DECL
    that is assigned.

    Together with ``IndirectCallSite``, this enables Phase 3 linking:
    ``fp_assignments.lhs_usr = indirect_call_sites.target_usr`` answers
    "which functions can be called through this field?"

    Attributes:
        from_file: Absolute path of the file containing the assignment.
        from_line: Source line of the assignment expression.
        lhs_usr: USR of the field, variable, or parameter that receives
            the function pointer.
        lhs_name: Display name of the left-hand side, e.g. ``"onData"``
            or ``"cb"``.
        rhs_usr: USR of the function being assigned.
        rhs_name: Display name of the assigned function, e.g. ``"handler"``.
        fn_ptr_type: Function pointer type signature string,
            e.g. ``"void (*)(uint8_t *, size_t)"``.
        method: How the assignment was detected — ``"assignment"``
            (BINARY_OPERATOR), ``"call_arg"`` (CALL_EXPR argument),
            ``"var_init"`` (VAR_DECL initializer), or ``"init_list"``
            (INIT_LIST_EXPR struct/array init).
        from_usr: USR of the enclosing function or method, or None.
    """
    from_file: str
    from_line: int
    lhs_usr: str
    lhs_name: str
    rhs_usr: str
    rhs_name: str
    fn_ptr_type: str
    method: str
    from_usr: str | None


@dataclass
class Symbol:
    """A parsed C/C++ symbol extracted from a translation unit.

    Represents a single declaration or definition encountered during
    libclang AST traversal.  Every symbol carries a ``usr`` that uniquely
    identifies it across translation units, a ``qualified_name`` built
    from semantic parent traversal, and metadata specific to its kind
    (signature for callables, enum values for constants, virtual flags
    for methods, and template relationships for specializations).

    Attributes:
        name: Unqualified symbol name (e.g. ``uart_init``).
        qualified_name: Fully qualified name with ``::`` separators,
            built by traversing semantic parents
            (e.g. ``namespace::Class::method``).
        kind: Symbol kind string — one of ``"function"``, ``"method"``,
            ``"constructor"``, ``"destructor"``, ``"class"``, ``"struct"``,
            ``"enum"``, ``"enum_constant"``, ``"typedef"``, ``"variable"``,
            ``"field"``, or ``"namespace"``.
        file: Absolute path to the source file containing this symbol.
        line: Start line of the declaration or definition (1-based).
        column: Start column of the declaration or definition (0-based).
        is_definition: True when the cursor is a definition; for
            ``_DECL_KINDS`` (function, function template, method) the
            declaration is indexed even without a definition.
        signature: Human-readable signature for callables — combines
            return type, name, and parameter list.  Empty string for
            non-callable symbols.
        docstring: Raw comment text from above the symbol, with comment
            markers (``/**``, ``//``, ``*``) stripped and collapsed into
            a single line.
        usr: libclang Unified Symbol Resolution — a cross-translation-unit
            identifier that links declarations and definitions of the
            same symbol.
        end_line: Last source line of the definition extent, or 0 when
            the extent is unavailable (e.g. the end lies in a different
            file due to macro expansion).
        enum_value: Integer value for ``enum_constant`` symbols.
            ``None`` for all other symbol kinds.
        is_virtual: True for virtual ``CXX_METHOD`` and destructor
            declarations.
        is_pure_virtual: True for pure virtual methods (marked ``= 0``).
        parent_usr: USR of the enclosing class, struct, or template.
            Empty string for free functions and file-scope symbols.
        is_template: True when this is a ``CLASS_TEMPLATE``,
            ``FUNCTION_TEMPLATE``, or partial specialization declaration.
        template_usr: USR of the primary template.  Non-empty only when
            this symbol is an instantiation of a template (e.g. a
            ``CLASS_DECL`` that was generated from a ``CLASS_TEMPLATE``).
    """
    name: str
    qualified_name: str      # namespace::Class::method
    kind: str                # "function", "class", "struct", "enum", etc.
    file: str                # absolute path
    line: int
    column: int
    is_definition: bool
    signature: str           # return type + params for callables
    docstring: str           # raw comment above the symbol
    usr: str                 # libclang Unified Symbol Resolution
    end_line: int = 0        # last line of the definition extent (0 if unknown)
    enum_value: int | None = None  # value of enum constant (ENUM_CONSTANT_DECL only)
    is_virtual: bool = False          # True for virtual CXX_METHOD
    is_pure_virtual: bool = False     # True for pure virtual (= 0) CXX_METHOD
    parent_usr: str = ""    # USR of enclosing class/struct (empty for free functions)
    is_template: bool = False  # True for CLASS_TEMPLATE, FUNCTION_TEMPLATE declarations
    template_usr: str = ""   # USR of the primary template (non-empty = this is an instantiation)


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
        cx.CursorKind.ENUM_DECL: "enum",
        cx.CursorKind.ENUM_CONSTANT_DECL: "enum_constant",
        cx.CursorKind.TYPEDEF_DECL: "typedef",
        cx.CursorKind.TYPE_ALIAS_DECL: "typedef",
        cx.CursorKind.VAR_DECL: "variable",
        cx.CursorKind.FIELD_DECL: "field",
        cx.CursorKind.NAMESPACE: "namespace",
    }
    return mapping.get(kind, kind.name.lower())


def _qualified_name(cursor: cx.Cursor) -> str:
    """Build the fully qualified name of a cursor via semantic parent traversal.

    Walks up the semantic parent chain (skipping the translation unit root)
    and joins each ancestor's spelling with ``::``.  Anonymous namespaces
    and unnamed entities produce empty segments which are collapsed by
    the join.

    Returns a string like ``"namespace::Class::method"``, or an empty
    string for the translation unit root.
    """
    parts: list[str] = []
    c = cursor
    while c and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        if c.spelling:
            parts.append(c.spelling)
        c = c.semantic_parent
    parts.reverse()
    return "::".join(parts)


def _signature(cursor: cx.Cursor) -> str:
    """Build a human-readable signature for callables.

    Returns an empty string for non-callable cursors (classes, enums,
    variables, etc.) so callers can unconditionally assign it to
    ``Symbol.signature``.
    """
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
    return " ".join(cleaned)


def _find_fn_refs_in_expr(
    cursor: cx.Cursor,
    in_roots_fn,
    not_excluded_fn,
    _skip_usr: str | None = None,
) -> list[cx.Cursor]:
    """Recursively extract function/method declarations referenced inside an expression.

    Walks into UNARY_OPERATOR (address-of ``&``), nested CALL_EXPR (callback
    wrappers like ``mbed::callback(...)``), and other intermediate nodes to find
    function pointer targets that libclang can resolve.

    Used both for function pointers passed as call arguments (via
    ``_emit_fn_ptr_targets`` on CALL_EXPR) and for function pointers in
    assignments, variable initializers, and struct/array init lists
    (via ``_emit_fn_ptr_targets`` on BINARY_OPERATOR, VAR_DECL, and
    INIT_LIST_EXPR).

    ``_skip_usr`` is the callee USR of the nearest enclosing CALL_EXPR whose
    callee is a project function; its children (the callee's own DECL_REF_EXPR
    wrappers) are skipped to avoid re-emitting direct calls as indirect edges.

    Returns a list of resolved ``FUNCTION_DECL`` / ``CXX_METHOD`` cursors whose
    definition location passes ``in_roots_fn`` and ``not_excluded_fn``.
    """
    results: list[cx.Cursor] = []

    # Direct reference to a callable (bare function name, method ref, etc.)
    if cursor.kind in (cx.CursorKind.DECL_REF_EXPR, cx.CursorKind.MEMBER_REF_EXPR):
        ref = cursor.referenced
        if ref is not None and ref.kind in _INDIRECT_TARGET_KINDS:
            # Skip if this is the callee of the enclosing project-function call
            if _skip_usr and ref.get_usr() == _skip_usr:
                return results
            loc = ref.location
            if loc.file and in_roots_fn(loc.file.name) and not_excluded_fn(loc.file.name):
                results.append(ref)
        return results

    # Address-of operator (&) — peel and recurse into the operand
    if cursor.kind == cx.CursorKind.UNARY_OPERATOR:
        for child in cursor.get_children():
            results.extend(_find_fn_refs_in_expr(child, in_roots_fn, not_excluded_fn, _skip_usr))
        return results

    # Nested call expression — e.g. callback(&Class::method, this).
    # Always recurse into arguments to find function pointer targets.
    # When the nested callee is itself a project function (not a callback
    # wrapper like mbed::callback), propagate its USR as _skip_usr so its
    # own callee-reference children are not emitted as indirect edges.
    if cursor.kind == cx.CursorKind.CALL_EXPR:
        nested_callee = cursor.referenced
        nested_skip = _skip_usr
        if nested_callee is not None:
            nested_callee_usr = nested_callee.get_usr()
            if nested_callee_usr:
                loc = nested_callee.location
                if loc.file and in_roots_fn(loc.file.name) and not_excluded_fn(loc.file.name):
                    nested_skip = nested_callee_usr
        for child in cursor.get_children():
            results.extend(_find_fn_refs_in_expr(child, in_roots_fn, not_excluded_fn, nested_skip))
        return results

    # Default: recurse into all children (handles implicit casts, parentheses, etc.)
    for child in cursor.get_children():
        results.extend(_find_fn_refs_in_expr(child, in_roots_fn, not_excluded_fn, _skip_usr))

    return results


def extract(
    unit: CompilationUnit,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
) -> Iterator[Symbol]:
    """Parse unit and yield Symbol records for definitions in source_roots.

    Thin backward-compatible wrapper over ``extract_all`` (symbols only).
    """
    symbols, _, _, _, _ = extract_all(unit, source_roots, exclude_paths, with_refs=False)
    return iter(symbols)


def extract_all(
    unit: CompilationUnit,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    with_refs: bool = False,
) -> tuple[list[Symbol], list[Reference], list[InheritanceRecord], list[IndirectCallSite], list[FnPointerAssignment]]:
    """Parse one translation unit and return extracted symbols, references, and inheritance.

    This is the single-pass entry point for all data extraction from a
    ``CompilationUnit``.  It parses the file with libclang, walks the AST
    once, and produces four outputs simultaneously.

    Args:
        unit: The translation unit to parse (file path + compiler flags).
        source_roots: Only emit symbols and references whose file is under
            one of these directories.  Defaults to ``[unit.file.parent]``.
        exclude_paths: Skip any file that falls under one of these paths
            (applied after ``source_roots`` filtering).
        with_refs: When True, also extract call, reference, and member-access
            edges and embed a source-line token fallback pass for
            template-obscured expressions.  Inheritance is always extracted
            regardless of this flag.

    Returns:
        A tuple ``(symbols, references, inheritance, indirect_call_sites, fp_assignments)``:
            symbols — all matching declarations and definitions.
            references — when ``with_refs`` is True, project-internal
                call/ref/member/indirect edges whose both ends are under
                ``source_roots``; empty list otherwise.
            inheritance — C++ base class edges for class/struct
                definitions under ``source_roots``, always extracted.
            indirect_call_sites — when ``with_refs`` is True, call sites
                where a function pointer field or variable is invoked
                (``obj->onData(args)``, ``stored_callback(args)``);
                empty list otherwise.
            fp_assignments — when ``with_refs`` is True, function pointer
                assignments (``field = &fn``, ``cb(&fn)`` param flow,
                ``void (*fp)(...) = &fn``) recording both the left-hand
                field/variable/parameter and the right-hand function;
                enables Phase 3 linking of assignment sites to call sites.
    """
    if not source_roots:
        source_roots = [unit.file.parent]
    if exclude_paths is None:
        exclude_paths = []

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

    def _resolve(path: str) -> Path:
        p = Path(path)
        return (cwd / p).resolve() if not p.is_absolute() else p.resolve()

    def _in_roots(path: str) -> bool:
        p = _resolve(path)
        return any(p == r or p.is_relative_to(r) for r in source_roots)

    def _not_excluded(path: str) -> bool:
        p = _resolve(path)
        return not any(p == e or p.is_relative_to(e) for e in exclude_paths)

    # --- Symbols ---
    symbols: list[Symbol] = []
    seen_usrs: dict[str, bool] = {}  # USR → is_definition (allows decl→def promotion)
    class_cursors: list[cx.Cursor] = []  # class/struct def cursors for inheritance extraction

    for cursor in tu.cursor.walk_preorder():
        if cursor.kind not in _SYMBOL_KINDS:
            continue
        loc = cursor.location
        if not loc.file:
            continue
        if not _in_roots(loc.file.name):
            continue
        if not _not_excluded(loc.file.name):
            continue

        is_def = cursor.is_definition()
        if not is_def and cursor.kind not in _DECL_KINDS:
            continue

        usr = cursor.get_usr()
        if not usr:
            continue

        # Extract enum constant value (signed int, works for negative values too)
        enum_val: int | None = None
        if cursor.kind == cx.CursorKind.ENUM_CONSTANT_DECL:
            try:
                enum_val = cursor.enum_value
            except Exception:
                enum_val = None

        # Virtual method flags (CXX_METHOD and DESTRUCTOR; destructors can be virtual too)
        is_virtual = bool(cursor.is_virtual_method()) if cursor.kind in (cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR) else False
        is_pure = bool(cursor.is_pure_virtual_method()) if cursor.kind in (cx.CursorKind.CXX_METHOD, cx.CursorKind.DESTRUCTOR) else False

        # Parent class/struct USR — set for methods, fields, and nested types
        parent_usr = ""
        sem_parent = cursor.semantic_parent
        if sem_parent and sem_parent.kind in (
            cx.CursorKind.CLASS_DECL,
            cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.CLASS_TEMPLATE,
            cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        ):
            parent_usr = sem_parent.get_usr() or ""

        # Template tracking: detect template declarations and instantiations
        is_template = cursor.kind in (
            cx.CursorKind.CLASS_TEMPLATE,
            cx.CursorKind.FUNCTION_TEMPLATE,
            cx.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION,
        )
        template_usr = ""
        if not is_template:
            # Check if this is an instantiation of a template.  Only resolve
            # for class/struct/function-level symbols — methods of template
            # classes are excluded (the template_usr would point to the
            # template method, not the template class, which is misleading).
            _TEMPLATE_INSTANCE_KINDS = frozenset({
                cx.CursorKind.CLASS_DECL,
                cx.CursorKind.STRUCT_DECL,
                cx.CursorKind.FUNCTION_DECL,
            })
            if cursor.kind in _TEMPLATE_INSTANCE_KINDS:
                try:
                    specialized = cursor.specialized_template
                    if specialized is not None:
                        template_usr = specialized.get_usr() or ""
                except Exception:
                    pass  # specialized_template may fail for some cursor kinds

        prev = seen_usrs.get(usr)
        if prev is not None:
            if is_def and not prev:
                seen_usrs[usr] = True
            else:
                continue
        else:
            seen_usrs[usr] = is_def

        symbols.append(Symbol(
            name=cursor.spelling,
            qualified_name=_qualified_name(cursor),
            kind=_cursor_kind_label(cursor.kind),
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

        # Collect class/struct definition cursors for inheritance extraction
        if cursor.kind in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL) and is_def:
            class_cursors.append(cursor)

    _t_symwalk = _time.monotonic() - _t_start - _t_parse  # subtract parse time

    # --- Inheritance: examine base specifiers of collected class cursors ---
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
                if child.kind == cx.CursorKind.CXX_BASE_SPECIFIER:
                    base_ref = child.referenced
                    if base_ref is None:
                        continue
                    base_usr = base_ref.get_usr()
                    if not base_usr:
                        continue
                    # Only record edges where the base class is in project sources
                    base_loc = base_ref.location
                    if base_loc.file and (not _in_roots(base_loc.file.name) or not _not_excluded(base_loc.file.name)):
                        continue
                    # Access specifier
                    access = _access_map.get(child.access_specifier, "public")
                    # Virtual inheritance (uses C API via ctypes — not exposed in Python bindings)
                    try:
                        from clang.cindex import conf
                        is_virt = bool(conf.lib.clang_isVirtualBase(child))
                    except Exception:
                        is_virt = False
                    inheritance.append(InheritanceRecord(
                        derived_usr=cls_usr,
                        base_usr=base_usr,
                        access=access,
                        is_virtual=is_virt,
                    ))
        except Exception:
            continue  # skip malformed cursors

    if not with_refs:
        _log.debug("  parse=%.1fs symwalk=%.1fs syms=%d", _t_parse, _t_symwalk, len(symbols))
        return symbols, [], inheritance, [], []

    # Build qualified-name → USR lookup for token-based fallback
    # (UNEXPOSED_EXPR nodes hide template expansions like mbed::callback(...)
    #  so we fall back to scanning raw tokens for &Class::method patterns)
    # Only include callable symbols — token-based fallbacks resolve
    # method/function names, so including fields/variables/enum_constants
    # would create spurious "call"/"indirect" references to non-callable
    # targets, polluting call-graph queries like find_hotspots.
    _callable_kind_strs = frozenset({"function", "method", "constructor", "destructor"})
    # Build qualified-name → USR map for template-obscured call resolution.
    # Limitation: overloaded functions share the same qualified_name (e.g. two
    # void send(int) / void send(char) specializations); the last one wins.
    # In practice, overloaded function templates are rare in embedded C/C++.
    _qn_to_usr: dict[str, str] = {}
    for s in symbols:
        if s.qualified_name and s.kind in _callable_kind_strs:
            _qn_to_usr[s.qualified_name] = s.usr

    # --- References (explicit stack DFS to track the enclosing function) ---
    refs: list[Reference] = []
    indirect_call_sites: list[IndirectCallSite] = []
    fp_assignments: list[FnPointerAssignment] = []
    seen_ref: set[tuple] = set()
    # Track function spans: [(usr, start_line, end_line)] for source-line fallback
    _fn_spans: list[tuple[str, int, int]] = []

    def _resolve_method_usr(
        method_name: str, field_name: str = ""
    ) -> str | None:
        """Find USR for *method_name*, preferring classes matching *field_name*."""
        usr = _qn_to_usr.get(method_name)
        if usr:
            return usr
        suffix = f"::{method_name}"
        candidates: list[tuple[str, str]] = [
            (qn, u) for qn, u in _qn_to_usr.items() if qn.endswith(suffix)
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][1]
        if field_name:
            _hint = field_name.lstrip("_").lower()
            _scored = []
            for qn, u in candidates:
                _class_part = qn.rsplit("::", 2)[-2] if "::" in qn else ""
                _score = 1 if _hint in _class_part.lower() else 0
                _scored.append((_score, qn, u))
            _scored.sort(key=lambda x: -x[0])
            if _scored[0][0] > 0:
                return _scored[0][2]
        return candidates[0][1]

    # Track enclosing function via walk_preorder() — the C implementation
    # is orders of magnitude faster than the equivalent manual stack DFS.
    # Each Python-level cursor.get_children() call crosses ctypes; the C
    # generator pays that cost once per cursor internally and just yields.
    fn_stack: list[tuple[str, int]] = []  # [(usr, end_line)]

    for cursor in tu.cursor.walk_preorder():
        # Pop enclosing functions whose body extent we've left.
        # walk_preorder() visits all descendants before any sibling, so
        # comparing line > end_line reliably detects scope exit.
        _cl = cursor.location
        while fn_stack and fn_stack[-1][1] > 0 and _cl is not None and _cl.line > fn_stack[-1][1]:
            fn_stack.pop()
        cur_fn = fn_stack[-1][0] if fn_stack else None

        # If this is a callable definition, it becomes the new enclosing
        # function for its children (which walk_preorder visits next).
        if cursor.kind in _CALLABLE_KINDS and cursor.is_definition():
            own_usr = cursor.get_usr()
            if own_usr:
                cur_fn = own_usr
            try:
                _ext = cursor.extent
                if _ext.start.file:
                    _ext_file = _ext.start.file.name
                    if str(Path(_ext_file).resolve()) == str(Path(tu.spelling).resolve()):
                        _fn_spans.append((cur_fn or '', _ext.start.line, _ext.end.line))
                        fn_stack.append((cur_fn or '', _ext.end.line))
                    else:
                        fn_stack.append((cur_fn or '', 0))
                else:
                    fn_stack.append((cur_fn or '', 0))
            except Exception:
                fn_stack.append((cur_fn or '', 0))

        ref_kind = _REF_KINDS.get(cursor.kind)
        if ref_kind is not None:
            referenced = cursor.referenced
            loc = cursor.location
            if referenced is not None and loc.file and _in_roots(loc.file.name) and _not_excluded(loc.file.name):
                to_usr = referenced.get_usr()
                # Only keep references whose target is itself project-indexable
                # (its declaration lives under source_roots) — bounds index size
                # by dropping refs into system/framework headers.
                ref_loc = referenced.location
                if to_usr and ref_loc.file and _in_roots(ref_loc.file.name) and _not_excluded(ref_loc.file.name):
                    key = (to_usr, loc.file.name, loc.line, cur_fn, ref_kind)
                    if key not in seen_ref:
                        seen_ref.add(key)
                        refs.append(Reference(
                            to_usr=to_usr,
                            from_file=loc.file.name,
                            from_line=loc.line,
                            from_usr=cur_fn,
                            ref_kind=ref_kind,
                        ))

            # --- Field-access call fallback ---
            # When a CALL_EXPR's direct referenced cursor is None or its
            # definition location is unresolvable (common for method calls on
            # member variables like ``_zmodem_driver.send()`` where libclang
            # cannot resolve the callee through the field type), walk the
            # CALL_EXPR's children looking for a MEMBER_REF_EXPR whose
            # referenced cursor points to a valid definition under source_roots.
            if cursor.kind == cx.CursorKind.CALL_EXPR:
                _callee_resolved = referenced is not None
                _callee_in_roots = False
                if _callee_resolved:
                    try:
                        _crl = referenced.location
                        if _crl.file and _in_roots(_crl.file.name) and _not_excluded(_crl.file.name):
                            _callee_in_roots = True
                    except Exception:
                        pass
                if not _callee_in_roots:
                    for child in cursor.get_children():
                        if child.kind == cx.CursorKind.MEMBER_REF_EXPR:
                            child_ref = child.referenced
                            if child_ref is not None and child_ref.kind in _CALLABLE_KINDS:
                                child_usr = child_ref.get_usr()
                                child_loc = child_ref.location
                                if child_usr and child_loc.file and _in_roots(child_loc.file.name) and _not_excluded(child_loc.file.name):
                                    key = (child_usr, loc.file.name, loc.line, cur_fn, "call")
                                    if key not in seen_ref:
                                        seen_ref.add(key)
                                        refs.append(Reference(
                                            to_usr=child_usr,
                                            from_file=loc.file.name,
                                            from_line=loc.line,
                                            from_usr=cur_fn,
                                            ref_kind="call",
                                        ))
                                    break  # one resolved callee per CALL_EXPR

        # --- Helper: function pointer type check ---
        def _is_fn_ptr_type(t: cx.Type) -> bool:
            """True when *t* is (or resolves via typedef to) a function pointer."""
            try:
                canon = t.get_canonical()
                if canon.kind == cx.TypeKind.POINTER:
                    pointee = canon.get_pointee()
                    return pointee.kind in (cx.TypeKind.FUNCTIONPROTO, cx.TypeKind.FUNCTIONNOPROTO)
            except Exception:
                pass
            return False

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
                return ""
            return " ".join(parts)

        # --- Indirect function pointer invocation detection ---
        # Detect CALL_EXPR where the callee is a function pointer field,
        # variable, or parameter.  libclang resolves cursor.referenced to the
        # FIELD_DECL / VAR_DECL / PARM_DECL (not a FUNCTION_DECL), so we
        # check the type directly on the callee cursor.
        if cursor.kind == cx.CursorKind.CALL_EXPR:
            loc = cursor.location
            if loc.file and _in_roots(loc.file.name) and _not_excluded(loc.file.name):
                callee = cursor.referenced
                if (callee is not None
                        and callee.kind in (cx.CursorKind.FIELD_DECL, cx.CursorKind.VAR_DECL, cx.CursorKind.PARM_DECL)
                        and _is_fn_ptr_type(callee.type)):
                    target_usr = callee.get_usr()
                    if target_usr:
                        indirect_call_sites.append(IndirectCallSite(
                            from_file=loc.file.name,
                            from_line=loc.line,
                            from_usr=cur_fn,
                            expr_text=_call_expr_text(cursor),
                            target_usr=target_usr,
                            target_name=callee.spelling,
                            fn_ptr_type=callee.type.spelling,
                        ))

        # --- Helper: extract left-hand side field/variable from assignment ---
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

        # --- Helper: emit "indirect" refs for function pointers in expression children ---
        # Shared by CALL_EXPR (callback arguments), BINARY_OPERATOR
        # (assignments), VAR_DECL (initializers), and INIT_LIST_EXPR
        # (struct/array init).  The recursive walk is handled by
        # _find_fn_refs_in_expr; this helper only iterates the top-level
        # children of *expr_cursor* and emits References.
        #
        # When *lhs_usr* / *lhs_name* are provided (from a BINARY_OPERATOR,
        # VAR_DECL, or INIT_LIST_EXPR LHS), also emits FnPointerAssignment
        # records to enable Phase 3 linking of assignment sites to call sites.
        def _emit_fn_ptr_targets(
            expr_cursor: cx.Cursor,
            caller_usr: str | None,
            skip_usr: str | None = None,
            lhs_usr: str | None = None,
            lhs_name: str = "",
            method: str = "assignment",
        ) -> None:
            loc = expr_cursor.location
            if not loc.file or not _in_roots(loc.file.name) or not _not_excluded(loc.file.name):
                return
            for child in expr_cursor.get_children():
                targets = _find_fn_refs_in_expr(child, _in_roots, _not_excluded, skip_usr)
                for target in targets:
                    target_usr = target.get_usr()
                    if not target_usr:
                        continue
                    if target_usr == skip_usr:
                        continue
                    target_loc = target.location
                    if target_loc.file and _in_roots(target_loc.file.name) and _not_excluded(target_loc.file.name):
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
                        # Phase 3: emit FnPointerAssignment when we have LHS info
                        if lhs_usr and lhs_usr != target_usr:
                            try:
                                _fp_type = child.type.spelling
                            except Exception:
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

        # --- Indirect calls: function pointers passed as arguments ---
        # e.g. callback(&Class::method, this), Thread::start(callback(...)),
        # EventQueue::call_every(ms, obj, &Class::handler).
        # Phase 3: also emits FnPointerAssignment for call-argument param flow —
        # when argument *i* carries a function pointer and callee parameter *i*
        # is a function pointer type, we record ``param_usr → function`` so
        # indirect invocation through that parameter can be linked back.
        if cursor.kind == cx.CursorKind.CALL_EXPR:
            direct_callee = cursor.referenced
            direct_callee_usr = direct_callee.get_usr() if direct_callee else None
            # Phase 3 param-flow: match arguments to callee parameters
            if direct_callee is not None:
                try:
                    callee_params = list(direct_callee.get_arguments())
                except Exception:
                    callee_params = []
                callee_args = list(cursor.get_arguments())
                for i, arg in enumerate(callee_args):
                    targets = _find_fn_refs_in_expr(arg, _in_roots, _not_excluded, direct_callee_usr)
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
                                            _fp_type = ""
                                        fp_assignments.append(FnPointerAssignment(
                                            from_file=loc.file.name if (loc := cursor.location) else "",
                                            from_line=loc.line if (loc := cursor.location) else 0,
                                            lhs_usr=param_usr,
                                            lhs_name=param.spelling,
                                            rhs_usr=target_usr,
                                            rhs_name=target.spelling,
                                            fn_ptr_type=_fp_type,
                                            method="call_arg",
                                            from_usr=cur_fn,
                                        ))
            _emit_fn_ptr_targets(cursor, cur_fn, direct_callee_usr)

        # --- Indirect calls: function pointers in assignments ---
        # field = &function, global = &function, *ptr = &function.
        # BINARY_OPERATOR covers =, +=, -= etc.; _find_fn_refs_in_expr only
        # returns function declarations so comparisons (==, !=) are harmless.
        if cursor.kind == cx.CursorKind.BINARY_OPERATOR:
            _lhs_usr, _lhs_name = _extract_lhs_field(cursor)
            _emit_fn_ptr_targets(cursor, cur_fn, lhs_usr=_lhs_usr, lhs_name=_lhs_name, method="assignment")

        # --- Indirect calls: function pointers in variable initializers ---
        # static void (*cb)(int) = &handler;  or local fn-ptr init.
        # Only emit FnPointerAssignment when the variable itself is a
        # function pointer type (avoids noise from struct/array variables
        # whose initializers happen to contain function references).
        if cursor.kind == cx.CursorKind.VAR_DECL and _is_fn_ptr_type(cursor.type):
            _lhs_usr = cursor.get_usr()
            _lhs_name = cursor.spelling
            _emit_fn_ptr_targets(cursor, cur_fn, lhs_usr=_lhs_usr, lhs_name=_lhs_name, method="var_init")

        # --- Indirect calls: function pointers in struct/array init lists ---
        # .on_data = &handler, {EV_DATA, &handler}, {&fn_a, &fn_b}.
        if cursor.kind == cx.CursorKind.INIT_LIST_EXPR:
            # For designated initializers (.field = &fn), extract LHS per child
            for child in cursor.get_children():
                _child_lhs_usr, _child_lhs_name = _extract_lhs_field(child)
                _emit_fn_ptr_targets(child, cur_fn, lhs_usr=_child_lhs_usr, lhs_name=_child_lhs_name, method="init_list")

        # --- Token fallback for UNEXPOSED_EXPR ---
        # When libclang cannot decompose a template expression (e.g. Mbed OS
        # callback(&Class::method, this)), the entire expression becomes an
        # opaque UNEXPOSED_EXPR with no children.  We scan the raw tokens for
        # &ClassName::methodName patterns and resolve them against the symbols
        # already extracted from this translation unit.
        #
        # The token sequence "& ClassName :: methodName" yields a partial
        # qualified name (e.g. "ZMODEM::thread_app" without the namespace
        # prefix).  We match by suffix against the full qualified-name map.
        if cursor.kind == cx.CursorKind.UNEXPOSED_EXPR:
            loc = cursor.location
            if loc.file and _in_roots(loc.file.name) and _not_excluded(loc.file.name):
                tokens = list(cursor.get_tokens())
                for i, tok in enumerate(tokens):
                    if tok.spelling == "&" and i + 3 < len(tokens):
                        t1 = tokens[i + 1]
                        t2 = tokens[i + 2]
                        t3 = tokens[i + 3]
                        if (t1.kind.name == "IDENTIFIER"
                                and t2.spelling == "::"
                                and t3.kind.name == "IDENTIFIER"):
                            partial = f"{t1.spelling}::{t3.spelling}"
                            # Try exact match first, then suffix match for
                            # namespace-qualified symbols (e.g. token
                            # "ZMODEM::thread_app" → "zbox::ZMODEM::thread_app")
                            target_usr = _qn_to_usr.get(partial)
                            if not target_usr:
                                suffix = f"::{partial}"
                                for qn, usr in _qn_to_usr.items():
                                    if qn.endswith(suffix):
                                        target_usr = usr
                                        break
                            if target_usr:
                                key = (target_usr, loc.file.name, loc.line, cur_fn, "indirect")
                                if key not in seen_ref:
                                    seen_ref.add(key)
                                    refs.append(Reference(
                                        to_usr=target_usr,
                                        from_file=loc.file.name,
                                        from_line=loc.line,
                                        from_usr=cur_fn,
                                        ref_kind="indirect",
                                    ))

        # --- Token fallback for template-obscured CALL_EXPR ---
        # When a CALL_EXPR is obscured by C++ standard library template
        # expansions (e.g. ``_zmodem_driver.network_init() != MODEM_RET_SUCCESS``
        # where the ``operator!=`` template dominates the AST), libclang
        # cannot resolve the callee through cursor.referenced or children.
        #
        # We scan the raw tokens of the CALL_EXPR for method-call patterns
        # (``field.method(`` or bare ``method(``) and resolve the callee
        # name against the symbol table built from this translation unit.
        if cursor.kind == cx.CursorKind.CALL_EXPR:
            loc = cursor.location
            if loc.file and _in_roots(loc.file.name) and _not_excluded(loc.file.name):
                tokens = list(cursor.get_tokens())
                # Pattern 1: obj.method( → IDENTIFIER DOT IDENTIFIER LPAREN
                for i, tok in enumerate(tokens):
                    if (tok.kind.name == "IDENTIFIER"
                            and i + 3 < len(tokens)
                            and tokens[i + 1].spelling == "."
                            and tokens[i + 2].kind.name == "IDENTIFIER"
                            and tokens[i + 3].spelling == "("):
                        field_name = tok.spelling
                        method_name = tokens[i + 2].spelling
                        target_usr = _resolve_method_usr(method_name, field_name)
                        if target_usr:
                            key = (target_usr, loc.file.name, loc.line, cur_fn, "call")
                            if key not in seen_ref:
                                seen_ref.add(key)
                                refs.append(Reference(
                                    to_usr=target_usr,
                                    from_file=loc.file.name,
                                    from_line=loc.line,
                                    from_usr=cur_fn,
                                    ref_kind="call",
                                ))
                    # Pattern 2: bare method( → IDENTIFIER LPAREN
                    if (tok.kind.name == "IDENTIFIER"
                            and i + 1 < len(tokens)
                            and tokens[i + 1].spelling == "("
                            and (i == 0 or tokens[i - 1].spelling != ".")):
                        method_name = tok.spelling
                        target_usr = _resolve_method_usr(method_name)
                        if target_usr:
                            key = (target_usr, loc.file.name, loc.line, cur_fn, "call")
                            if key not in seen_ref:
                                seen_ref.add(key)
                                refs.append(Reference(
                                    to_usr=target_usr,
                                    from_file=loc.file.name,
                                    from_line=loc.line,
                                    from_usr=cur_fn,
                                    ref_kind="call",
                                ))

    # --- Source-line fallback for template-obscured method calls ---
    # When C++ standard library template expansions dominate the AST
    # (e.g. ``_zmodem_driver.network_init() != MODEM_RET_SUCCESS`` where
    # ``operator!=`` templates consume all CALL_EXPR nodes at the same source
    # line), libclang cannot resolve the project-level callee.
    #
    # Post-processing pass: scan the original source file for lines that are
    # inside a known function body but have no ``call`` / ``indirect``
    # reference, and emit references for ``obj.method(`` patterns found there.
    #
    if with_refs:
        _tu_file = tu.spelling  # absolute path to the source file
        _lines_with_calls: set[int] = {
            r.from_line for r in refs if r.ref_kind in ("call", "indirect")
            and r.from_file == _tu_file
        }
        try:
            _source_text = Path(_tu_file).read_text(encoding="utf-8", errors="replace")
            _source_lines = _source_text.splitlines()
        except Exception:
            _source_lines = []
        for _lineno_0, _line in enumerate(_source_lines):
            _lineno = _lineno_0 + 1  # 1-based
            if _lineno in _lines_with_calls:
                continue
            # Find enclosing function for this line
            _line_fn = None
            for _fn_usr, _fn_start, _fn_end in _fn_spans:
                if _fn_start <= _lineno <= _fn_end:
                    _line_fn = _fn_usr
                    break
            if _line_fn is None:
                continue
            # Scan for obj.method( patterns
            for _m in re.finditer(r'(\w+)\.(\w+)\s*\(', _line):
                _field_name = _m.group(1)
                _method_name = _m.group(2)
                _target_usr = _resolve_method_usr(_method_name, _field_name)
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

    _t_total = _time.monotonic() - _t_start
    _t_refwalk = _t_total - _t_parse - _t_symwalk
    _log.debug(
        "  parse=%.1fs symwalk=%.1fs refwalk=%.1fs syms=%d refs=%d",
        _t_parse, _t_symwalk, _t_refwalk, len(symbols), len(refs),
    )
    return symbols, refs, inheritance, indirect_call_sites, fp_assignments
