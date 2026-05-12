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

# Map from -mcpu= prefix to clang --target triple
_ARM_CORTEX_TARGET = "--target=arm-none-eabi"


def _infer_target(args: list[str]) -> str | None:
    """Return a clang --target triple inferred from -mcpu= flags, or None."""
    for arg in args:
        if arg.startswith("-mcpu=cortex-m"):
            return _ARM_CORTEX_TARGET
    return None


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

    # Inject --target triple so clang understands -mcpu=/-mfpu=/-mfloat-abi= on the host
    target = _infer_target(result)
    if target:
        result = [target] + result

    return result


def _gcc_system_includes(compiler: Path) -> list[str]:
    """Return -isystem flags for a GCC ARM cross-compiler's built-in headers."""
    # compiler: .../gcc-arm-none-eabi-X/bin/arm-none-eabi-g++
    # lib dir:  .../gcc-arm-none-eabi-X/lib/gcc/arm-none-eabi/<ver>/include
    toolchain_root = compiler.parent.parent
    lib_gcc = toolchain_root / "lib" / "gcc"
    result: list[str] = []
    if lib_gcc.is_dir():
        for triple_dir in lib_gcc.iterdir():
            for ver_dir in triple_dir.iterdir():
                inc = ver_dir / "include"
                if inc.is_dir():
                    result += ["-isystem", str(inc)]
                inc_fixed = ver_dir / "include-fixed"
                if inc_fixed.is_dir():
                    result += ["-isystem", str(inc_fixed)]
    triple = triple_dir.name  # e.g. arm-none-eabi
    libc_inc = toolchain_root / triple / "include"
    if libc_inc.is_dir():
        result += ["-isystem", str(libc_inc)]
    # C++ standard library headers (arm-none-eabi/include/c++/<ver>)
    cxx_inc_base = toolchain_root / triple / "include" / "c++"
    if cxx_inc_base.is_dir():
        for ver_dir in cxx_inc_base.iterdir():
            if ver_dir.is_dir():
                result += ["-isystem", str(ver_dir)]
                # Per-target subdir (e.g. arm-none-eabi, thumb, ...)
                for sub in ver_dir.iterdir():
                    if sub.is_dir():
                        result += ["-isystem", str(sub)]
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

        # Extract compiler binary before stripping it
        compiler: Path | None = None
        if raw_args and not raw_args[0].startswith("-"):
            compiler = Path(raw_args[0])
            raw_args = raw_args[1:]

        clang_args = normalize_args(raw_args, cwd)

        # For ARM GCC cross-compilers inject system include paths so libclang
        # finds stdint.h and friends when --target=arm-none-eabi is active
        if compiler and "arm-none-eabi" in compiler.name:
            clang_args = clang_args + _gcc_system_includes(compiler)

        lang = _detect_language(file, clang_args)

        yield CompilationUnit(
            file=file,
            directory=cwd,
            language=lang,
            clang_args=clang_args,
        )
