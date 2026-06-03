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


@dataclass
class Reference:
    to_usr: str        # USR of the referenced definition (links to Symbol.usr)
    from_file: str     # file containing the reference (absolute, as clang reports)
    from_line: int
    from_usr: str | None   # USR of the enclosing function/method (caller), or None
    ref_kind: str      # "call" | "ref" | "member"


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


def extract(
    unit: CompilationUnit,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
) -> Iterator[Symbol]:
    """Parse unit and yield Symbol records for definitions in source_roots.

    Thin backward-compatible wrapper over ``extract_all`` (symbols only).
    """
    symbols, _ = extract_all(unit, source_roots, exclude_paths, with_refs=False)
    return iter(symbols)


def extract_all(
    unit: CompilationUnit,
    source_roots: list[Path] | None = None,
    exclude_paths: list[Path] | None = None,
    with_refs: bool = False,
) -> tuple[list[Symbol], list[Reference]]:
    """Parse unit once and return (symbols, references).

    symbols: definitions/declarations whose file is under source_roots.
    references: when with_refs is True, call/ref/member expressions whose BOTH
                ends (referencing site and referenced definition) are under
                source_roots — i.e. project-internal references, bounded in size.
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
            column=loc.offset,
            is_definition=is_def,
            signature=_signature(cursor),
            docstring=_docstring(cursor),
            usr=usr,
            end_line=_end_line(cursor, loc),
        ))

    if not with_refs:
        return symbols, []

    # --- References (explicit stack DFS to track the enclosing function) ---
    refs: list[Reference] = []
    seen_ref: set[tuple] = set()
    # stack of (cursor, enclosing_function_usr)
    stack: list[tuple] = [(tu.cursor, None)]
    while stack:
        cursor, fn_usr = stack.pop()
        cur_fn = fn_usr
        if cursor.kind in _CALLABLE_KINDS and cursor.is_definition():
            cur_fn = cursor.get_usr() or fn_usr

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

        for child in cursor.get_children():
            stack.append((child, cur_fn))

    return symbols, refs
