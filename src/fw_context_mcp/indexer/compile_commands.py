"""Parse and normalize compile_commands.json for libclang consumption.

WHY: compile_commands.json emitted by build tools contains GCC-specific flags
that libclang (clang's C API) does not understand.  Passing them directly
causes ``TranslationUnitLoadError`` or silent incorrect parsing.  This module
strips unsupported flags, resolves relative include paths, detects target
triples from compiler names, and injects GCC cross-compiler system include
paths so libclang can find ``stdint.h`` and friends.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from fw_context_mcp.utils import CPP_SOURCE_EXTENSIONS, TU_EXTENSIONS

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
    # GCC-only format warnings (libclang does not implement these)
    "-Wformat-signedness",
    "-Wno-format-signedness",
    "-Wformat-overflow",
    "-Wno-format-overflow",
    "-Wformat-truncation",
    "-Wno-format-truncation",
    # GCC-only code-generation flags.  They change no declaration and no
    # macro, so dropping them cannot change what the index sees — but clang
    # rejects them as unknown arguments and the whole parse dies.
    #
    # Found by running clang over the flags of every real project rather
    # than one at a time: five, not the two that first showed up.  They
    # blocked the preprocessor on assembly units, where there is no libclang
    # fallback to hide the failure.
    "-fno-reorder-functions",
    "-fno-printf-return-value",
    "-fstrict-volatile-bitfields",
    "-fno-tree-switch-conversion",
})

# GCC-only warning flags that take a level suffix (=1, =2) — drop any token
# matching one of these prefixes. Clang rejects them as unknown options,
# which becomes a hard error under -Werror and aborts the TU parse.
_DROP_PREFIXES = frozenset({
    "-Wformat-overflow=",
    "-Wno-format-overflow=",
    "-Wformat-truncation=",
    "-Wno-format-truncation=",
    # GCC-only ABI switch carrying a value (=ieee, =alternative).  A prefix
    # rather than an exact match, because the value varies by project.
    "-mfp16-format=",
})

# Two-token flags: drop both the flag and its next argument
_DROP_WITH_ARG = frozenset({
    "-o",    # output file
    "-MF",   # dependency file path
    "-MT",   # dependency target
    "-MQ",   # dependency target (quoted)
})



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
    # NOTE: standard libclang distributions may lack the WebAssembly backend.
    # If wasm32/wasm64 targets cause TranslationUnitLoadError, rebuild
    # libclang with -DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD=WebAssembly.
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
    "cortex-r": "arm-none-eabi",
    # 32-bit Cortex-A (ARMv7-A)
    "cortex-a5": "arm-none-eabi",
    "cortex-a7": "arm-none-eabi",
    "cortex-a8": "arm-none-eabi",
    "cortex-a9": "arm-none-eabi",
    "cortex-a15": "arm-none-eabi",
    "cortex-a17": "arm-none-eabi",
    # 64-bit Cortex-A (AArch64)
    "cortex-a53": "aarch64-none-elf",
    "cortex-a57": "aarch64-none-elf",
    "cortex-a72": "aarch64-none-elf",
    "cortex-a": "aarch64-none-elf",
}


def _detect_target_triple(
    compiler: Path | None, args: list[str]
) -> str | None:
    """Detect the GNU target triple for the cross-compiler.

    WHY: libclang needs ``--target=<triple>`` to correctly interpret
    architecture-specific flags like ``-mcpu=cortex-m4`` and ``-mfpu=fpv4-sp-d16``.
    Without it, libclang uses the host triple (e.g. ``x86_64-linux-gnu``)
    and rejects ARM/RISC-V flags as unknown, emitting hard errors.

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
        # Strip version suffix: arm-none-eabi-g++-12 → arm-none-eabi-g++
        name = re.sub(r'-\d+(\.\d+)*$', '', name)
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
    # Unsupported targets (xtensa, msp430, avr, etc.) cause
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
    raw_entry: dict | None = None


def expand_response_file(token: str, cwd: Path | None = None) -> list[str]:
    rsp = Path(token[1:])
    if cwd is not None and not rsp.is_absolute():
        rsp = cwd / rsp
    if not rsp.exists():
        return []
    return shlex.split(rsp.read_text())


def _is_source_file(token: str) -> bool:
    # No .lower(): the suffix table of gcc is case-sensitive, and `.C`
    # is a C++ source while `.c` is a C one.
    return Path(token).suffix in TU_EXTENSIONS


def _detect_language(file: Path, clang_args: list[str]) -> str:
    """Say whether the compiler reads this unit as C or as C++.

    The suffix decides when the compiler has a rule for it — see
    CPP_SOURCE_EXTENSIONS, taken from the table in `man gcc`.  Case is part
    of that rule: `.C` is C++ and `.c` is C, so this must not lowercase.

    A suffix the table does not cover falls through to `-std=`, which is how
    a project building `.S` or an unusual extension still gets an answer.
    """
    if file.suffix in CPP_SOURCE_EXTENSIONS:
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

    WHY: libclang rejects flags that clang does not support (``-flto``,
    ``-fno-common``, ``-specs=``, ``-Wno-stringop-truncation``, etc.).
    Passing them causes ``TranslationUnitLoadError``.  This function removes
    them before the argument list reaches libclang.

    Also resolves relative ``-I`` paths to absolute because libclang uses
    the process CWD, not the compile_commands.json directory — ``-I.``
    would resolve to the wrong location otherwise.

    If source_file is given, any token matching that filename (by basename)
    is dropped — handles build systems where the source file is not the last arg.
    """
    # Expand @response_files first
    expanded: list[str] = []
    for token in raw_args:
        if token.startswith("@"):
            expanded.extend(expand_response_file(token, cwd))
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

        if token.startswith(tuple(_DROP_PREFIXES)):
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

    # Resolve relative -I paths to absolute (libclang uses process CWD, not
    # compile_commands.json directory, so -I. would resolve to wrong location).
    _INC_FLAGS = frozenset({"-I", "-isystem", "-idirafter", "-iquote", "-imacros", "-include"})
    resolved: list[str] = []
    skip_next = False
    for token in result:
        if skip_next:
            skip_next = False
            if not Path(token).is_absolute():
                token = str((cwd / token).resolve())
            resolved.append(token)
            continue
        if token in _INC_FLAGS:
            resolved.append(token)
            skip_next = True
        elif token.startswith("-I"):
            path = token[2:]
            if path and not Path(path).is_absolute():
                path = str((cwd / path).resolve())
            resolved.append(f"-I{path}")
        elif any(token.startswith(f) for f in ("-isystem", "-idirafter", "-iquote", "-imacros", "-include")):
            # Attached form: -isystem/path, -include/path
            for f in ("-isystem", "-idirafter", "-iquote", "-imacros", "-include"):
                if token.startswith(f):
                    path = token[len(f):]
                    if path and not Path(path).is_absolute():
                        path = str((cwd / path).resolve())
                    resolved.append(f"{f}{path}")
                    break
        else:
            resolved.append(token)

    # Silences clang's "unknown warning option" diagnostic for every GCC-only
    # -W flag at once.  Appended last so it also wins over an earlier -Werror,
    # which would otherwise turn the diagnostic into a hard parse error.
    resolved.append("-Wno-unknown-warning-option")

    return resolved


def validate_include_files(clang_args: list[str]) -> None:
    """Check that all -include and -imacros referenced files exist.

    WHY: build-systems generate configuration headers during the build
    (e.g. ``mbed_config.h`` in ``BUILD/``).  If the build was cleaned
    but compile_commands.json was preserved, these files are missing.
    libclang would fail for every TU that includes the SDK, producing a
    partial index and wasting seconds per TU on doomed parse attempts.

    *clang_args* should already be normalized by ``normalize_args()`` (paths
    resolved to absolute).  Raises ``RuntimeError`` listing every missing file
    so the user can fix them all at once rather than one at a time.
    """
    _FILE_FLAGS = frozenset({"-include", "-imacros"})
    missing: list[str] = []
    skip_next = False
    for token in clang_args:
        if skip_next:
            skip_next = False
            p = Path(token)
            if not p.exists():
                missing.append(str(p))
            continue
        if token in _FILE_FLAGS:
            skip_next = True
        elif token.startswith("-include") or token.startswith("-imacros"):
            for prefix in ("-include", "-imacros"):
                if token.startswith(prefix):
                    p = Path(token[len(prefix) :])
                    if not p.exists():
                        missing.append(str(p))
                    break
    if missing:
        raise RuntimeError(
            "Build-generated configuration files referenced by -include/-imacros "
            "were not found:\n  "
            + "\n  ".join(missing)
            + "\n\nRun 'fw-context index --build' to regenerate the build output."
        )


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir()) if path.is_dir() else []
    except OSError:
        return []


def _gcc_system_includes(compiler: Path, triple: str) -> list[str]:
    """Return -isystem flags for a GCC cross-compiler's built-in headers.

    WHY: when ``--target=arm-none-eabi`` is injected, libclang switches to
    that target's header search path.  But the GCC toolchain's built-in
    headers (``stdint.h``, ``stddef.h``, newlib, libstdc++) are not in
    clang's internal resource directory.  Without these ``-isystem`` flags,
    libclang fails to find fundamental headers — every ``#include <stdint.h>``
    becomes a fatal error and the entire TU parse fails.

    *triple* is the GNU target triple (e.g. ``arm-none-eabi``) detected
    by :func:`_detect_target_triple`.  Only include directories matching
    this triple are added — host GCC includes are filtered out.

    For toolchains installed in a dedicated directory (e.g.
    ``/opt/gcc-arm-none-eabi/``), the root is ``compiler.parent.parent``.
    For system-installed cross-compilers (e.g. ``/usr/bin/arm-none-eabi-g++``),
    the root is ``/usr`` and the triple-directories live under
    ``/usr/lib/gcc/<triple>/`` and ``/usr/<triple>/include``.
    """
    toolchain_root = compiler.parent.parent
    lib_gcc = toolchain_root / "lib" / "gcc"
    result: list[str] = []

    # Scan only the directory matching the detected triple
    triple_gcc = lib_gcc / triple
    for ver_dir in _safe_iterdir(triple_gcc):
        inc = ver_dir / "include"
        if inc.is_dir():
            result += ["-isystem", str(inc)]
        inc_fixed = ver_dir / "include-fixed"
        if inc_fixed.is_dir():
            result += ["-isystem", str(inc_fixed)]

    # Newlib/libc headers: <toolchain_root>/<triple>/include
    libc_inc = toolchain_root / triple / "include"
    if libc_inc.is_dir():
        result += ["-isystem", str(libc_inc)]

    # C++ standard library headers: <triple>/include/c++/<ver>
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
    """Yield one CompilationUnit per entry in compile_commands.json.

    WHY iterator: compile_commands.json for large firmware projects (mbed-os,
    Zephyr) contains thousands of entries.  Yielding one at a time avoids
    loading the entire structure into memory at once — only the current
    TU is materialized.
    """
    entries = json.loads(path.read_text(encoding="utf-8-sig"))
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
                clang_args = clang_args + _gcc_system_includes(compiler, triple)

        lang = _detect_language(file, clang_args)

        yield CompilationUnit(
            file=file,
            directory=cwd,
            language=lang,
            clang_args=clang_args,
            raw_entry=entry,
        )
