"""Parse and normalize compile_commands.json for libclang consumption."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Flags libclang does not support — drop silently
_DROP_FLAGS = frozenset({
    "-flto",
    "-fno-common",
    "-pipe",
    "-save-temps",
    # dependency generation
    "-MD", "-MP", "-MMD", "-MG",
    # GCC-only warning variants
    "-Wno-stringop-truncation",
    "-Wstringop-truncation",
    "-Wstringop-overflow",
    "-Wno-stringop-overflow",
    "-Wlogical-op",
    "-Wno-logical-op",
    "-Wmissing-parameter-type",
    "-Wno-missing-parameter-type",
    "-Wold-style-declaration",
    "-Wno-old-style-declaration",
})

# Two-token flags: drop both the flag and its next argument
_DROP_WITH_ARG = frozenset({
    "-o",    # output file
    "-MF",   # dependency file path
    "-MT",   # dependency target
    "-MQ",   # dependency target (quoted)
})

_SOURCE_EXTS = frozenset({".c", ".cpp", ".cc", ".cxx", ".c++"})


@dataclass
class CompilationUnit:
    file: Path
    directory: Path
    language: str        # "c" or "cpp"
    clang_args: list[str] = field(default_factory=list)


def _expand_response_file(token: str, cwd: Path) -> list[str]:
    rsp = Path(token[1:])
    if not rsp.is_absolute():
        rsp = cwd / rsp
    if not rsp.exists():
        return []
    return shlex.split(rsp.read_text())


def _is_source_file(token: str) -> bool:
    return Path(token).suffix.lower() in _SOURCE_EXTS


def _detect_language(file: Path, clang_args: list[str]) -> str:
    if file.suffix.lower() in {".cpp", ".cc", ".cxx", ".c++"}:
        return "cpp"
    std = next((a for a in clang_args if a.startswith("-std=")), "")
    if "++" in std:
        return "cpp"
    return "c"


def normalize_args(raw_args: list[str], cwd: Path) -> list[str]:
    """Expand response files and strip GCC-specific flags incompatible with libclang."""
    # Expand @response_files first
    expanded: list[str] = []
    for token in raw_args:
        if token.startswith("@"):
            expanded.extend(_expand_response_file(token, cwd))
        else:
            expanded.append(token)

    result: list[str] = []
    skip_next = False
    for i, token in enumerate(expanded):
        if skip_next:
            skip_next = False
            continue

        if token in _DROP_WITH_ARG:
            skip_next = True
            continue

        if token in _DROP_FLAGS:
            continue

        # -specs=<arg> and -specs <arg>
        if token.startswith("-specs=") or token == "-specs":
            if token == "-specs":
                skip_next = True
            continue

        # Source file as last argument — libclang receives file path separately
        if i == len(expanded) - 1 and _is_source_file(token):
            continue

        result.append(token)

    return result


def parse(path: Path) -> Iterator[CompilationUnit]:
    """Yield one CompilationUnit per entry in compile_commands.json."""
    entries = json.loads(path.read_text())
    for entry in entries:
        file = Path(entry["file"])
        cwd = Path(entry.get("directory", path.parent))

        if not file.is_absolute():
            file = (cwd / file).resolve()

        raw_args: list[str] = entry.get("arguments") or shlex.split(
            entry.get("command", "")
        )
        # First token is the compiler binary — libclang doesn't need it
        if raw_args and not raw_args[0].startswith("-"):
            raw_args = raw_args[1:]

        clang_args = normalize_args(raw_args, cwd)
        lang = _detect_language(file, clang_args)

        yield CompilationUnit(
            file=file,
            directory=cwd,
            language=lang,
            clang_args=clang_args,
        )
