"""Parse and normalize compile_commands.json for libclang consumption."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

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

# Target triple prefixes known to be supported by the bundled libclang.
# Only inject --target for these; unsupported triples (xtensa, etc.) cause
# TranslationUnitLoadError when libclang lacks that backend.
# Covers all LLVM backends that have corresponding GCC cross-compiler triples.
_SUPPORTED_TARGET_PREFIXES = frozenset({
    # ARM family
    "arm-",
    "armv6-", "armv7-", "armv8-",
    "thumb-",
    "aarch64-",
    # RISC-V
    "riscv32-", "riscv64-",
    # x86
    "i386-", "i486-", "i586-", "i686-", "i786-",
    "x86_64-",
    # MIPS
    "mips-", "mipsel-", "mips64-", "mips64el-",
    "mipsisa32r6-", "mipsisa64r6-",
    # PowerPC
    "powerpc-", "powerpc64-", "powerpc64le-", "powerpcle-",
    # Sparc
    "sparc-", "sparc64-",
    # SystemZ (s390x)
    "s390-", "s390x-",
    # LoongArch
    "loongarch32-", "loongarch64-",
    # WebAssembly
    "wasm32-", "wasm64-",
    # AVR
    "avr-",
    # MSP430
    "msp430-",
    # XCore
    "xcore-",
    # BPF
    "bpf-",
    # Hexagon
    "hexagon-",
    # Lanai
    "lanai-",
    # VE
    "ve-",
})

# Known -mcpu prefix → GNU target triple mapping for architectures where
# the flag alone is sufficient to identify the target.
_MCPU_TO_TRIPLE: dict[str, str] = {
    "cortex-m": "arm-none-eabi",
    "cortex-a": "aarch64-none-elf",
    "cortex-r": "arm-none-eabi",
}


def _detect_target_triple(
    compiler: Path | None, args: list[str]
) -> str | None:
    """Detect the GNU target triple for the cross-compiler.

    1. Extract from compiler name: ``<triple>-gcc`` → ``<triple>``
       (e.g. ``arm-none-eabi-g++`` → ``arm-none-eabi``,
             ``riscv32-unknown-elf-gcc`` → ``riscv32-unknown-elf``,
             ``xtensa-esp32-elf-g++`` → ``xtensa-esp32-elf``)
    2. Fallback: infer from ``-mcpu=`` flags (Cortex-M → arm-none-eabi).
    3. Fallback: infer from ``-march=`` flags (``-march=rv32*`` → riscv,
       ``-march=armv*-m*`` → arm-none-eabi).

    Returns None when the target cannot be determined or is not supported
    by the installed libclang (e.g. xtensa, msp430, avr).
    """
    triple: str | None = None

    # 1 — Compiler name: strip trailing -gcc/-g++/-clang suffix
    if compiler is not None:
        name = compiler.name
        for suffix in ("-g++", "-gcc", "-clang", "-clang++"):
            if name.endswith(suffix):
                triple = name[: -len(suffix)]
                break
        if triple is None and "-" in name:
            # Heuristic: any compiler with a hyphen likely has a target prefix
            # like "arm-none-eabi-g++". Try stripping the last two components.
            parts = name.rsplit("-", 2)
            if len(parts) == 3 and parts[-1] in ("gcc", "g++", "clang", "clang++"):
                triple = parts[0] + "-" + parts[1]
            else:
                # Single-hyphen: e.g. "riscv64-gcc" → "riscv64"
                parts2 = name.rsplit("-", 1)
                if len(parts2) == 2 and parts2[-1] in ("gcc", "g++", "clang", "clang++"):
                    triple = parts2[0]

    # 2 — -mcpu= flags
    if triple is None:
        for arg in args:
            if arg.startswith("-mcpu="):
                cpu = arg[6:].split("+")[0].lower()  # strip +extensions
                for prefix, t in _MCPU_TO_TRIPLE.items():
                    if cpu.startswith(prefix):
                        triple = t
                        break
                break  # -mcpu without mapping → unknown, don't fall through

    # 3 — -march= flags
    if triple is None:
        for arg in args:
            if arg.startswith("-march="):
                arch = arg[7:].lower()
                if arch.startswith("rv"):
                    triple = "riscv32-unknown-elf"
                elif "armv" in arch and "-m" in arch:
                    triple = "arm-none-eabi"
                break

    if triple is None:
        return None

    # Only inject --target for triples the installed libclang actually supports.
    # Unspported targets (xtensa, msp430, avr, etc.) cause
    # TranslationUnitLoadError — libclang parses them fine as host target.
    for prefix in _SUPPORTED_TARGET_PREFIXES:
        if triple.startswith(prefix):
            return triple
    return None


@dataclass
class CompilationUnit:
    """One compilation unit from compile_commands.json.

    Attributes:
        file: Absolute or relative path to the source file being compiled.
        directory: Working directory for the compilation command.
        language: Source language — ``"c"`` or ``"cpp"``.
        clang_args: Full compiler argument list (resolved and normalized for libclang).
    """
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


def normalize_args(
    raw_args: list[str],
    cwd: Path,
    source_file: str | None = None,
    compiler: Path | None = None,
) -> list[str]:
    """Expand response files and strip GCC-specific flags incompatible with libclang.

    If source_file is given, any token matching that filename (by basename)
    is dropped — handles build systems where the source file is not the last arg.
    """
    # Expand @response_files first
    expanded: list[str] = []
    for token in raw_args:
        if token.startswith("@"):
            expanded.extend(_expand_response_file(token, cwd))
        else:
            expanded.append(token)

    source_basename = Path(source_file).name if source_file else None

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

        # Source file — libclang receives file path separately
        if _is_source_file(token):
            if source_basename and Path(token).name == source_basename:
                continue
            if source_basename is None and i == len(expanded) - 1:
                continue

        result.append(token)

    # Inject --target triple so clang understands -mcpu=/-mfpu=/-mfloat-abi= on the host
    target = _detect_target_triple(compiler, result)
    if target:
        result = [f"--target={target}"] + result

    return result


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir()) if path.is_dir() else []
    except OSError:
        return []


def _gcc_system_includes(compiler: Path) -> list[str]:
    """Return -isystem flags for a GCC ARM cross-compiler's built-in headers."""
    # compiler: .../gcc-arm-none-eabi-X/bin/arm-none-eabi-g++
    # lib dir:  .../gcc-arm-none-eabi-X/lib/gcc/arm-none-eabi/<ver>/include
    toolchain_root = compiler.parent.parent
    lib_gcc = toolchain_root / "lib" / "gcc"
    result: list[str] = []
    triple_dirs = _safe_iterdir(lib_gcc)
    for triple_dir in triple_dirs:
        for ver_dir in _safe_iterdir(triple_dir):
            inc = ver_dir / "include"
            if inc.is_dir():
                result += ["-isystem", str(inc)]
            inc_fixed = ver_dir / "include-fixed"
            if inc_fixed.is_dir():
                result += ["-isystem", str(inc_fixed)]
    for triple_dir in triple_dirs:
        triple = triple_dir.name
        libc_inc = toolchain_root / triple / "include"
        if libc_inc.is_dir():
            result += ["-isystem", str(libc_inc)]
        # C++ standard library headers (arm-none-eabi/include/c++/<ver>)
        cxx_inc_base = toolchain_root / triple / "include" / "c++"
        for ver_dir in _safe_iterdir(cxx_inc_base):
            if ver_dir.is_dir():
                result += ["-isystem", str(ver_dir)]
                # Per-target subdir (e.g. arm-none-eabi, thumb, ...)
                for sub in _safe_iterdir(ver_dir):
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

        clang_args = normalize_args(raw_args, cwd, str(file), compiler)

        # For GCC cross-compilers inject system include paths so libclang
        # finds stdint.h and friends when --target is active.
        # Pass raw_args so -mcpu=/-march= heuristics can augment compiler-name detection.
        if compiler is not None:
            triple = _detect_target_triple(compiler, raw_args)
            if triple is not None:
                clang_args = clang_args + _gcc_system_includes(compiler)

        lang = _detect_language(file, clang_args)

        yield CompilationUnit(
            file=file,
            directory=cwd,
            language=lang,
            clang_args=clang_args,
        )
