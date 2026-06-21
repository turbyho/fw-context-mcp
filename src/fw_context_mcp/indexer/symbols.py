"""Extract symbol records from a CompilationUnit using libclang."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import clang.cindex as cx

from .compile_commands import CompilationUnit

_INDEX = cx.Index.create()

_SYMBOL_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
    cx.CursorKind.CONSTRUCTOR,
    cx.CursorKind.DESTRUCTOR,
    cx.CursorKind.CLASS_DECL,
    cx.CursorKind.CLASS_TEMPLATE,
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
}

# Cursor kinds that are valid targets for an indirect call (function pointers)
_INDIRECT_TARGET_KINDS = frozenset({
    cx.CursorKind.FUNCTION_DECL,
    cx.CursorKind.FUNCTION_TEMPLATE,
    cx.CursorKind.CXX_METHOD,
})


@dataclass
class Reference:
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
class Symbol:
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


def _cursor_kind_label(kind: cx.CursorKind) -> str:
    mapping = {
        cx.CursorKind.FUNCTION_DECL: "function",
        cx.CursorKind.FUNCTION_TEMPLATE: "function",
        cx.CursorKind.CXX_METHOD: "method",
        cx.CursorKind.CONSTRUCTOR: "constructor",
        cx.CursorKind.DESTRUCTOR: "destructor",
        cx.CursorKind.CLASS_DECL: "class",
        cx.CursorKind.CLASS_TEMPLATE: "class",
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
    parts: list[str] = []
    c = cursor
    while c and c.kind != cx.CursorKind.TRANSLATION_UNIT:
        if c.spelling:
            parts.append(c.spelling)
        c = c.semantic_parent
    parts.reverse()
    return "::".join(parts)


def _signature(cursor: cx.Cursor) -> str:
    """Build a human-readable signature for callables."""
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
    symbols, _, _ = extract_all(unit, source_roots, exclude_paths, with_refs=False)
    return iter(symbols)


def extract_all(
    unit: CompilationUnit,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    with_refs: bool = False,
) -> tuple[list[Symbol], list[Reference], list[InheritanceRecord]]:
    """Parse unit once and return (symbols, references, inheritance).

    symbols: definitions/declarations whose file is under source_roots.
    references: when with_refs is True, call/ref/member expressions whose BOTH
                ends (referencing site and referenced definition) are under
                source_roots — i.e. project-internal references, bounded in size.
    inheritance: C++ base class edges for class/struct definitions under
                 source_roots (always extracted, regardless of with_refs).
    source_roots: only emit symbols/refs whose file is under one of these paths.
    exclude_paths: skip files under any of these paths (applied after source_roots).
    If source_roots is None, restrict to the unit's own file only.
    """
    if not source_roots:
        source_roots = [unit.file.parent]
    if exclude_paths is None:
        exclude_paths = []

    tu = _INDEX.parse(
        str(unit.file),
        args=unit.clang_args,
        options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )

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

        # Virtual method flags (CXX_METHOD only; False for all other kinds)
        is_virtual = bool(cursor.is_virtual_method()) if cursor.kind == cx.CursorKind.CXX_METHOD else False
        is_pure = bool(cursor.is_pure_virtual_method()) if cursor.kind == cx.CursorKind.CXX_METHOD else False

        # Parent class/struct USR — set for methods, fields, and nested types
        parent_usr = ""
        sem_parent = cursor.semantic_parent
        if sem_parent and sem_parent.kind in (
            cx.CursorKind.CLASS_DECL,
            cx.CursorKind.STRUCT_DECL,
            cx.CursorKind.CLASS_TEMPLATE,
        ):
            parent_usr = sem_parent.get_usr() or ""

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
        ))

        # Collect class/struct definition cursors for inheritance extraction
        if cursor.kind in (cx.CursorKind.CLASS_DECL, cx.CursorKind.STRUCT_DECL) and is_def:
            class_cursors.append(cursor)

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
        return symbols, [], inheritance

    # Build qualified-name → USR lookup for token-based fallback
    # (UNEXPOSED_EXPR nodes hide template expansions like mbed::callback(...)
    #  so we fall back to scanning raw tokens for &Class::method patterns)
    # Only include callable symbols — token-based fallbacks resolve
    # method/function names, so including fields/variables/enum_constants
    # would create spurious "call"/"indirect" references to non-callable
    # targets, polluting call-graph queries like find_hotspots.
    _callable_kind_strs = frozenset({"function", "method", "constructor", "destructor"})
    _qn_to_usr: dict[str, str] = {}
    for s in symbols:
        if s.qualified_name and s.kind in _callable_kind_strs:
            _qn_to_usr[s.qualified_name] = s.usr

    # --- References (explicit stack DFS to track the enclosing function) ---
    refs: list[Reference] = []
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

    # stack of (cursor, enclosing_function_usr)
    stack: list[tuple] = [(tu.cursor, None)]
    while stack:
        cursor, fn_usr = stack.pop()
        cur_fn = fn_usr
        if cursor.kind in _CALLABLE_KINDS and cursor.is_definition():
            cur_fn = cursor.get_usr() or fn_usr
            # Track function span for source-line fallback
            try:
                _ext = cursor.extent
                if _ext.start.file:
                    _ext_file = _ext.start.file.name
                    _resolved_ext = Path(_ext_file).resolve()
                    _resolved_tu = Path(tu.spelling).resolve()
                    if str(_resolved_ext) == str(_resolved_tu):
                        _fn_spans.append((cur_fn, _ext.start.line, _ext.end.line))
            except Exception:
                pass

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

        # --- Indirect calls: detect function pointers passed as arguments ---
        # When a CALL_EXPR passes a function/method pointer as an argument
        # (e.g. callback(&Class::method, this), Thread::start(callback(...)),
        #  EventQueue::call_every(ms, obj, &Class::handler)),
        # extract the target and emit an "indirect" reference edge.
        # This is platform-agnostic: works for Mbed OS, Zephyr, FreeRTOS, POSIX,
        # and any other framework that accepts function pointers as arguments.
        if cursor.kind == cx.CursorKind.CALL_EXPR:
            loc = cursor.location
            direct_callee = cursor.referenced
            direct_callee_usr = direct_callee.get_usr() if direct_callee else None
            if loc.file and _in_roots(loc.file.name) and _not_excluded(loc.file.name):
                for arg in cursor.get_children():
                    targets = _find_fn_refs_in_expr(arg, _in_roots, _not_excluded, direct_callee_usr)
                    for target in targets:
                        target_usr = target.get_usr()
                        if not target_usr:
                            continue
                        # Don't emit indirect if it's the same as the direct callee
                        if target_usr == direct_callee_usr:
                            continue
                        target_loc = target.location
                        if target_loc.file and _in_roots(target_loc.file.name) and _not_excluded(target_loc.file.name):
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

        for child in cursor.get_children():
            stack.append((child, cur_fn))

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

    return symbols, refs, inheritance
