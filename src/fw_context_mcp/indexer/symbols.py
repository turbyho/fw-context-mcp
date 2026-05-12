"""Extract symbol records from a CompilationUnit using libclang."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

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

    source_roots: only emit symbols whose file is under one of these paths.
    exclude_paths: skip symbols whose file is under any of these paths (applied after source_roots).
    If source_roots is None, emit symbols from the unit's own file only.
    """
    if source_roots is None:
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

    # Maps USR → is_definition; allows promotion from declaration to definition
    seen_usrs: dict[str, bool] = {}

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

        # For most kinds require definition; for declarations of callables allow decl too
        is_def = cursor.is_definition()
        if not is_def and cursor.kind not in _DECL_KINDS:
            continue

        usr = cursor.get_usr()
        if not usr:
            continue

        prev = seen_usrs.get(usr)
        if prev is not None:
            if is_def and not prev:
                # Promote: declaration seen before, now we have the definition
                seen_usrs[usr] = True
            else:
                continue
        else:
            seen_usrs[usr] = is_def

        yield Symbol(
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
        )
