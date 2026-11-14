"""Parse and normalize compile_commands.json for libclang consumption.

WHY: compile_commands.json emitted by build tools contains GCC-specific flags
that libclang (clang's C API) does not understand.  Passing them directly
causes ``TranslationUnitLoadError`` or silent incorrect parsing.  This module
strips unsupported flags, resolves relative include paths, and asks the GCC
driver of the build for its system include directories and its predefined
macros (see ``_driver_query``).  Without a GCC driver it detects the target
triple from the compiler name.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fw_context_mcp.indexer._driver_options import DRIVER_TAKES_NEXT
from fw_context_mcp.indexer._driver_query import (
    DriverInfo,
    is_cxx_driver_name,
    is_gcc_driver_name,
    known_compilers,
    libclang_args,
    predefine_flags,
    query_gcc_driver,
    resolve_compiler,
    triple_of_driver_name,
)
from fw_context_mcp.path_key import canonical_path, resolve_path
from fw_context_mcp.utils import CPP_SOURCE_EXTENSIONS, TU_EXTENSIONS, command_words

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
    # ESP-IDF with GCC 13: every one of 833 units gave "unknown argument".
    "-fno-shrink-wrap",
    # Zephyr/NCS (mbedtls units): libclang gives the error "optimization
    # flag '-fno-defer-pop' is not supported" in 33 units of each image.
    # Measured 2026-10-24: the parse goes on, but the error counts against
    # the health of the index and hides a real error among the others.
    "-fno-defer-pop",
    # GCC 15: removal of a malloc and free pair.  ESP-IDF 6.1 gives
    # -fno-malloc-dce to each unit, and clang 23 refused it: "unknown
    # argument", thus the macros step failed in each of 968 units of an
    # ESP32 build.  A sweep of all 47 options of that build with clang found
    # no other one (measured 2026-11-14).  GCC defines no macro for it.
    "-fmalloc-dce", "-fno-malloc-dce",
    # Xtensa GCC (ESP32): code generation flags that clang does not know.
    # libclang 22 parses an ESP32 build for the real Xtensa target, where
    # libclang 18 used an x86 proxy without the -m flags, thus the flags now
    # reach the parse: "unknown argument '-mlongcalls'" in each unit of two
    # ESP32 projects (measured 2026-10-24).  Each flag below gave "unknown
    # argument" or "unsupported option" in libclang 22 for
    # --target=xtensa-esp32-elf (probe of 2026-10-24).  The names exist only
    # in Xtensa GCC, thus no other target loses a flag.
    "-mlongcalls", "-mno-longcalls",
    "-mtext-section-literals", "-mno-text-section-literals",
    "-mauto-litpools", "-mno-auto-litpools",
    "-mtarget-align", "-mno-target-align",
    "-mserialize-volatile", "-mno-serialize-volatile",
    "-mfix-esp32-psram-cache-issue",
    "-mdisable-hardware-atomics",
    "-mforce-no-pic",
    "-mconst16", "-mno-const16",
    "-mabi=windowed", "-mabi=call0",
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
    # Xtensa GCC (ESP32 with PSRAM): the value names the work-around.
    "-mfix-esp32-psram-cache-strategy=",
    # GCC tuning parameters of the code generation (--param=name=value).
    # Zephyr/NCS: "argument unused during compilation:
    # '--param=min-pagesize=0'" was an error in 33 units of three images
    # (measured 2026-10-24).  No parameter changes what the source means.
    "--param=",
    # GCC 15: zero-initialization of the padding bits (=all, =unions,
    # =standard).  ESP-IDF 6.1 gives it to each unit, and clang 23 refused
    # it: "unknown argument", thus the macros step failed in each of 968
    # units of an ESP32 build (measured 2026-11-14).  GCC defines no macro
    # for it, and it changes no declaration: only the code that clears a
    # padding.
    "-fzero-init-padding-bits=",
})

# Two-token flags: drop both the flag and its next argument
_DROP_WITH_ARG = frozenset({
    "-o",    # output file
    "-MF",   # dependency file path
    "-MT",   # dependency target
    "-MQ",   # dependency target (quoted)
    "--param",  # the two-token form of the GCC tuning parameters above
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


def _detect_target_triple(compiler: Path | None) -> str | None:
    """Return the GNU target triple that the name of the cross-compiler names, or None.

    WHY: libclang needs ``--target=<triple>`` to correctly interpret
    architecture-specific flags like ``-mcpu=cortex-m4`` and ``-mfpu=fpv4-sp-d16``.
    Without it, libclang uses the host triple (e.g. ``x86_64-linux-gnu``)
    and rejects ARM/RISC-V flags as unknown, emitting hard errors.

    The name of a cross-compiler is ``<triple>-<driver>`` (the convention
    of GNU): ``arm-none-eabi-g++`` → ``arm-none-eabi``,
    ``riscv32-unknown-elf-gcc`` → ``riscv32-unknown-elf``.  The grammar of
    the name is the one of the driver query (``triple_of_driver_name``):
    the version suffix and ``.exe`` in each case (``arm-none-eabi-gcc.EXE``
    of Windows) go first.

    WHY no target from ``-mcpu=`` or ``-march=``: the flag does not name
    the target.  ``arm-none-eabi-gcc -mcpu=cortex-a53`` compiles 32-bit
    code, and a table of CPU names gave it ``aarch64-none-elf``; it also
    gave ``cortex-a53`` the 32-bit triple, because ``cortex-a5`` is a
    prefix of it, and ``-march=rv64*`` the triple ``riscv32-unknown-elf``.
    Only the compiler knows its target: the driver query asks it
    (``_driver_query``), and this function is for a driver that does not
    answer.

    Returns None when the name names no target, or when the target is not in
    ``_SUPPORTED_TARGET_PREFIXES`` (e.g. xtensa).
    """
    triple = triple_of_driver_name(compiler) if compiler is not None else None
    if triple is None:
        return None
    # Only inject --target for triples the installed libclang actually supports.
    # An unsupported target (xtensa) causes TranslationUnitLoadError.
    # A triple can be the bare architecture: `avr-g++` gives `avr`, which
    # the prefix `avr-` does not match, thus the bare name counts too.
    for prefix in _SUPPORTED_TARGET_PREFIXES:
        if triple.startswith(prefix) or triple == prefix.rstrip("-"):
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
    return shlex.split(rsp.read_text(encoding="utf-8"))


def _names_file(token: str, cwd: Path, source: Path) -> bool:
    """Return True when the argument *token* (relative to *cwd*) names the file *source*.

    *source* is a canonical path (``path_key.canonical_path``).  WHY the
    identity of the file and not its base name: the entry can name the
    file through a link or, on a case-insensitive file system, in a
    different case.  The unit then has the canonical name, and a compare
    of base names kept the token: libclang got two input files and refused
    the unit.  WHY each positional token and not only a token with the
    suffix of a source: a link can have a different suffix, and an
    assembly source (``.S``) has no suffix of a C or C++ unit.

    A token that cannot be resolved names no file, thus not *source*,
    which was resolved: a name that the system refuses (OSError,
    ValueError).  A loop of links gives a path and no error
    (``path_key.resolve_path``), and that path is not *source*.
    """
    try:
        return canonical_path(cwd / token) == source
    except (OSError, ValueError):
        return False


def _is_source_file(token: str) -> bool:
    # No .lower(): the suffix table of gcc is case-sensitive, and `.C`
    # is a C++ source while `.c` is a C one.
    return Path(token).suffix in TU_EXTENSIONS


#: The suffixes that a C++ driver compiles as C++ (``is_cxx_driver_name``).
_CXX_DRIVER_C_SUFFIXES = frozenset({".c", ".h", ".i"})

XLanguage = Literal["c", "cpp", "none", "other"]


def _x_language(value: str) -> XLanguage:
    """Return what the value of ``-x`` makes of the unit: ``"cpp"``, ``"c"``, ``"none"`` or ``"other"``.

    ``none`` gives the decision back to the suffix (GCC).  ``other`` is a
    language that is not C or C++ (``assembler-with-cpp``, ``objective-c``,
    ``objective-c++``): the unit then keeps its ``-x``, and no rule of C or
    C++ applies to it.  WHY ``objective-c++`` is ``other``: an ``-x c++`` in
    its place makes libclang parse Objective-C syntax as C++.
    """
    if value == "none":
        return "none"
    if value.startswith("c++"):
        return "cpp"
    if value == "c" or value.startswith(("c-", "cpp-output")):
        return "c"
    return "other"


def _x_values(args: Sequence[str]) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end, value)`` of each ``-x`` of *args*: ``-x c++`` (two tokens) and ``-xc++`` (one).

    WHY the positions: GCC applies an ``-x`` to the input files after it,
    thus the caller compares the position with the one of the source, and
    removes the tokens from the libclang arguments.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token == "-x" and i + 1 < len(args):
            yield i, i + 2, args[i + 1]
            i += 2
            continue
        if token.startswith("-x") and len(token) > 2:
            yield i, i + 1, token[2:]
        i += 1


def _source_position(file: Path, args: Sequence[str], cwd: Path) -> int:
    """Return the position of the token of *args* that names *file*, or ``len(args)`` when none names it.

    Only a token that is no flag can be the source, as ``normalize_args``
    reads it; each check resolves a path, thus the flags are left out.
    """
    for i, token in enumerate(args):
        if not token.startswith("-") and _names_file(token, cwd, file):
            return i
    return len(args)


def _language_of_unit(
    file: Path, args: Sequence[str], compiler: Path | None, cwd: Path | None,
) -> tuple[str, bool]:
    """Return the language (``"c"`` or ``"cpp"``) that the compiler gives *file*, and True when no suffix rule decided it.

    The rules of the compiler, in this order:

    1. The last ``-x`` before the source file (GCC: ``-x`` applies to the
       input files after it).  With *cwd*, the source token of *args* ends
       the search; without it (the libclang arguments of a manifest, which
       hold no source token) each ``-x`` counts.  An ``-x`` of a language
       that is not C or C++ leaves the suffix to decide, and no driver rule
       applies.
    2. A C++ driver (``is_cxx_driver_name``) compiles a `.c`, `.h` or `.i`
       file as C++.
    3. The suffix: the C++ suffixes of ``man gcc`` (CPP_SOURCE_EXTENSIONS),
       in their case (`.C` is C++, `.c` is C); else C.

    WHY no ``-std=``: it does not select the language.  Measured
    2026-11-13 with GCC 14 (arm-zephyr-eabi): ``gcc -std=gnu++17 -c f.c``
    compiles C, with the warning "valid for C++/ObjC++ but not for C".  An
    earlier rule took such a unit for C++, and libclang then refused the
    unit (TranslationUnitLoadError).
    """
    chosen: XLanguage = "none"
    found = list(_x_values(args))
    if found:
        end = _source_position(file, args, cwd) if cwd is not None else len(args)
        for start, _end, value in found:
            if start < end:
                chosen = _x_language(value)
    if chosen == "c" or chosen == "cpp":
        return chosen, True
    if file.suffix in CPP_SOURCE_EXTENSIONS:
        return "cpp", False
    if chosen == "none" and compiler is not None and is_cxx_driver_name(compiler) and file.suffix in _CXX_DRIVER_C_SUFFIXES:
        return "cpp", True
    return "c", False


def unit_language(file: Path, args: Sequence[str], compiler: Path | None = None, cwd: Path | None = None) -> str:
    """Return the language (``"c"`` or ``"cpp"``) that the compiler gives the unit of *file* (``_language_of_unit``).

    *args* are the arguments after the compiler: those of the entry (with
    *compiler* and *cwd*), or the libclang arguments of a unit, which carry
    an explicit ``-x`` when the suffix does not decide the language
    (``_with_language``).  WHY one function: the run, the driver query and
    ``reindex_file`` order the claims of the headers by this language.
    """
    return _language_of_unit(file, args, compiler, cwd)[0]


def _with_language(clang_args: list[str], language: str, explicit: bool) -> list[str]:
    """Return *clang_args* with the language of the unit as libclang must read it.

    Each ``-x`` of C or C++ (or ``none``) leaves: in the libclang arguments
    the source token is gone, thus an ``-x`` after the source file of the
    entry would apply to it.  When a rule other than the suffix decided the
    language (*explicit*), one ``-x`` goes first: libclang knows no driver,
    and reads a `.c` file of ``g++`` as C.  An ``-x`` of an other language
    (``assembler-with-cpp``) stays as it is.

    A ``-std=`` of the other language leaves too.  GCC ignores it with a
    warning ("valid for C++/ObjC++ but not for C"), and clang refuses the
    unit ("invalid argument '-std=gnu++17' not allowed with 'C'"); measured
    2026-11-13, both directions.  A C++ standard holds ``++`` in its name,
    a standard of C never does.
    """
    drop: set[int] = set()
    for start, end, value in _x_values(clang_args):
        if _x_language(value) != "other":
            drop.update(range(start, end))
    for i, arg in enumerate(clang_args):
        if arg.startswith(("-std=", "--std=")) and ("++" in arg) != (language == "cpp"):
            drop.add(i)
    kept = [arg for i, arg in enumerate(clang_args) if i not in drop]
    if explicit:
        return ["-x", "c++" if language == "cpp" else "c", *kept]
    return kept


# The attached forms of the path flags of `normalize_args`.  No flag of the
# tuple starts with an other one (`-isysroot` is no `-isystem`), thus the
# order does not matter.  `-iwithprefix*` is not here: its value is
# relative to the prefix.
_ATTACHED_PATH_FLAGS = ("-isystem", "-idirafter", "-iquote", "-imacros", "-include", "-iprefix", "-isysroot")


def _absolute_flag_path(flag: str, value: str, cwd: Path) -> str:
    """Return the relative *value* of the path flag *flag* as an absolute path of the directory *cwd*.

    WHY ``-iprefix`` keeps its trailing separator: GCC joins the prefix and
    the value of ``-iwithprefix*`` as text (``include/`` + ``newlib`` gives
    ``include/newlib``), and ``resolve_path`` removes the separator.
    """
    absolute = str(resolve_path(cwd / value))
    if flag == "-iprefix" and value.endswith(("/", "\\")):
        absolute += value[-1]
    return absolute


def normalize_args(
    raw_args: list[str],
    cwd: Path,
    source_file: str | None = None,
) -> list[str]:
    """Expand response files and strip GCC-specific flags incompatible with libclang.

    WHY: libclang rejects flags that clang does not support (``-flto``,
    ``-fno-common``, ``-specs=``, ``-Wno-stringop-truncation``, etc.).
    Passing them causes ``TranslationUnitLoadError``.  This function removes
    them before the argument list reaches libclang.

    Also resolves relative ``-I`` paths to absolute because libclang uses
    the process CWD, not the compile_commands.json directory — ``-I.``
    would resolve to the wrong location otherwise.

    If *source_file* is given, each positional token that names that file
    (:func:`_names_file`) is dropped, also when it is not the last argument.
    Each reader of the flags names the file once on its own: named twice,
    libclang refuses the unit, and clang preprocesses it twice.

    The result has no ``--target``: the caller adds the target
    (``_clang_args_for_unit``).  WHY: the target of the GCC driver comes
    from the driver (``libclang_args``).  A target that this function added
    from ``-march=rv*`` gave the unit a second ``--target``
    (``riscv32-unknown-elf`` after ``riscv32-esp-elf``), and the last one
    won.
    """
    # Expand @response_files first
    expanded: list[str] = []
    for token in raw_args:
        if token.startswith("@"):
            expanded.extend(expand_response_file(token, cwd))
        else:
            expanded.append(token)

    source = canonical_path(cwd / source_file) if source_file else None

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
        if source is not None and not token.startswith("-") and _names_file(token, cwd, source):
            continue
        if source is None and _is_source_file(token) and i == len(expanded) - 1:
            continue

        result.append(token)

    # Resolve relative -I paths to absolute (libclang uses process CWD, not
    # compile_commands.json directory, so -I. would resolve to wrong location).
    # resolve_path: a loop of links in a directory of the flags stopped the
    # parse of the whole build on Python 3.11 and 3.12 (RuntimeError).
    # `-iprefix`, `--sysroot` and `-isysroot` name a directory relative to
    # the directory of the compilation too.  The values of
    # `-iwithprefix*` and `-imultilib` are relative to the prefix or the
    # sysroot, thus they stay as they are.
    _INC_FLAGS = frozenset({
        "-I", "-isystem", "-idirafter", "-iquote", "-imacros", "-include",
        "-iprefix", "--sysroot", "-isysroot",
    })
    resolved: list[str] = []
    pending = ""
    verbatim_next = False
    for token in result:
        if pending:
            if not Path(token).is_absolute():
                token = _absolute_flag_path(pending, token, cwd)
            resolved.append(token)
            pending = ""
            continue
        if verbatim_next:
            resolved.append(token)
            verbatim_next = False
            continue
        if token in _INC_FLAGS:
            resolved.append(token)
            pending = token
        elif token in DRIVER_TAKES_NEXT:
            # An other option with a value (`-include-pch x.pch`): the
            # value stays as it is, and the option is no attached form of
            # a path flag (`-include` + `-pch`).
            resolved.append(token)
            verbatim_next = True
        elif token.startswith("--sysroot="):
            path = token[len("--sysroot="):]
            if path and not Path(path).is_absolute():
                path = _absolute_flag_path("--sysroot", path, cwd)
            resolved.append(f"--sysroot={path}")
        elif token.startswith("-I"):
            path = token[2:]
            if path and not Path(path).is_absolute():
                path = str(resolve_path(cwd / path))
            resolved.append(f"-I{path}")
        elif token.startswith(_ATTACHED_PATH_FLAGS):
            # Attached form: -isystem/path, -include/path, -iprefixdir/
            for f in _ATTACHED_PATH_FLAGS:
                if token.startswith(f):
                    path = token[len(f):]
                    if path and not Path(path).is_absolute():
                        path = _absolute_flag_path(f, path, cwd)
                    resolved.append(f"{f}{path}")
                    break
        else:
            resolved.append(token)

    # A flag that the parse does not use is no fault of the source: libclang
    # parses and makes no object, thus "-c" and the code generation flags are
    # unused.  Under the -Werror of a unit the warning was an error.
    # Measured with libclang 22 on Zephyr/NCS (mbedtls units, -Werror):
    # "argument unused during compilation: '-c'" in 33 units of each of
    # two images.
    resolved.append("-Wno-unused-command-line-argument")
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
    """Return -isystem flags for a GCC cross-compiler's built-in headers, from its layout.

    WHY this is only the fallback: it GUESSES the directories from the
    layout of the toolchain, and the guess is not the search order of GCC
    (libstdc++ comes after the C library here, and before it in GCC).
    ``_driver_query`` asks the driver instead.  This guess stays for a
    driver that does not answer.

    WHY at all: when ``--target=arm-none-eabi`` is injected, libclang
    switches to that target's header search path, but the GCC toolchain's
    built-in headers (``stdint.h``, ``stddef.h``, newlib, libstdc++) are not
    in clang's internal resource directory.  Without these ``-isystem``
    flags every ``#include <stdint.h>`` is a fatal error.

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


def _clang_args_for_unit(
    cwd: Path,
    file: Path,
    compiler: Path | None,
    known: dict[str, Path],
    language: str,
    expanded: list[str],
) -> list[str]:
    """Build the libclang flags of one unit, from the GCC driver when it answers.

    WHY the driver: see ``_driver_query``.  A GCC driver gives the system
    directories in its own order and the macros of its own target.  The
    earlier code guessed both from the layout of the toolchain, and a
    target that libclang has no backend for got the headers of the host.

    When the driver does not answer, the flags are the earlier ones: the
    flags of ``compile_commands.json``, a target from the compiler name, and
    the guessed toolchain directories.  A unit that names its own target
    (``--target=``, ``-target``: a clang build) keeps it alone: a second
    target came after it and won, and the directories of a GCC toolchain
    are no directories of a clang build.

    *expanded* are the arguments with the response files replaced: each
    reader of the flags reads them, thus a response file is read once.
    """
    driver = _driver_for_unit(expanded, cwd, compiler, known, language)
    clang_args = normalize_args(expanded, cwd, str(file))
    if driver is not None:
        return libclang_args(driver, clang_args)
    if any(arg.startswith(("--target=", "-target=")) or arg == "-target" for arg in clang_args):
        return clang_args
    # The target, so that clang understands -mcpu=/-mfpu=/-mfloat-abi= on
    # the host.
    #
    # A unit without a target is parsed for the host: a build for the host,
    # or a compiler that did not answer, which _driver_query reported.
    triple = _detect_target_triple(compiler)
    if triple is None:
        return clang_args
    clang_args = [f"--target={triple}", *clang_args]
    # The directories of a GCC toolchain are for a GCC driver alone, not
    # for `<triple>-clang`.
    if compiler is not None and is_gcc_driver_name(compiler):
        clang_args += _gcc_system_includes(compiler, triple)
    return clang_args


def _driver_for_unit(
    expanded: list[str],
    cwd: Path,
    compiler: Path | None,
    known: dict[str, Path],
    language: str,
) -> DriverInfo | None:
    """Ask the GCC driver of the unit for its target, system directories and macros, in *language*.

    *expanded* are the arguments of the entry with the response files
    replaced (``_expanded``); *language* is ``unit_language`` of the unit.
    WHY the caller gives both: ``parse`` needs them for the libclang
    arguments too, and a response file is read once for each unit.
    """
    if compiler is None or not is_gcc_driver_name(compiler):
        return None
    resolved = resolve_compiler(str(compiler), cwd, known)
    if resolved is None:
        return None
    return query_gcc_driver(resolved, language, predefine_flags(expanded), cwd)


def _expanded(raw_args: list[str], cwd: Path) -> list[str]:
    """Return *raw_args* with each response file (``@file``) replaced by its arguments.

    WHY: a build that gives its flags in a response file (``@flags.rsp``)
    names its ``-x`` and the flags of the driver query there, and the rules
    of the language and of the driver must read them.
    """
    expanded: list[str] = []
    for token in raw_args:
        expanded.extend(expand_response_file(token, cwd) if token.startswith("@") else [token])
    return expanded


def load_entries(path: Path) -> Any:
    """Return the JSON of the compilation database *path*, as each reader of it must read it.

    WHY ``utf-8-sig``: a tool on Windows can write the file with a byte
    order mark.  ``json.loads`` refuses a text that starts with it, thus a
    reader with ``utf-8`` reported a damaged file, or no defines, for a
    file that ``parse`` indexed.  WHY one function: eight readers of the
    file had two encodings.

    Raise OSError when the file cannot be read, and ValueError when it is
    not UTF-8 or not JSON (``UnicodeDecodeError`` and ``JSONDecodeError``
    are ValueError).  The value is any JSON value, as ``json.loads`` gives
    it: the caller checks the shape (a list of entries).
    """
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse(path: Path) -> Iterator[CompilationUnit]:
    """Yield one CompilationUnit per entry in compile_commands.json.

    WHY iterator: compile_commands.json for large firmware projects (mbed-os,
    Zephyr) contains thousands of entries.  Yielding one at a time avoids
    loading the entire structure into memory at once — only the current
    TU is materialized.

    Each GCC driver of the build is asked for its system directories and
    macros (``_driver_query``).  An earlier release took a list of globs
    and the project root as more arguments; a caller that still gives them
    fails at once with a TypeError.
    """
    entries = load_entries(path)
    # One build can name a compiler with its directory in some entries and
    # without it in others; see known_compilers().
    known = known_compilers(entries)
    for entry in entries:
        cwd = Path(entry.get("directory", path.parent))
        # The identity of the file: links resolved, the case of the disk
        # (path_key.canonical_path).  A build tool can spell a file of a
        # case-insensitive file system in a different case, and each later
        # resolve() of a canonical path keeps it canonical.  An absolute
        # entry gets the same form: it was not resolved before, and its
        # spelling then differed from the key of its own file.
        file = canonical_path(cwd / entry["file"])

        # The arguments as the compiler got them, by the rules of the host:
        # shlex removed each backslash of a Windows command
        # (utils.command_words).
        raw_args: list[str] = entry.get("arguments") or command_words(entry.get("command", ""))

        # Extract compiler binary before stripping it
        compiler: Path | None = None
        if raw_args and not raw_args[0].startswith("-"):
            compiler = Path(raw_args[0])
            raw_args = raw_args[1:]

        expanded = _expanded(raw_args, cwd)
        lang, explicit = _language_of_unit(file, expanded, compiler, cwd)
        clang_args = _with_language(
            _clang_args_for_unit(cwd, file, compiler, known, lang, expanded), lang, explicit,
        )

        yield CompilationUnit(
            file=file,
            directory=cwd,
            language=lang,
            clang_args=clang_args,
            raw_entry=entry,
        )


# ── Command-line defines ───────────────────────────────────────────────────


def _defines_of_entry(entry: dict) -> dict[str, str]:
    """Return the `-D` flags of one compilation database entry.

    Both spellings: `-DNAME=value` and `-D NAME=value`.  A name with no
    value maps to an empty string, which is what the compiler does — a bare
    `-DNAME` defines it as `1`, and the index reports the flag as written
    rather than the value the preprocessor derives.
    """
    tokens = entry.get("arguments")
    if not isinstance(tokens, list):
        # The words of the command as ``parse`` reads them.  ``str.split``
        # kept the quotes: `-DVERSION=\\"1.2\\"` gave the value `\\"1.2\\"`,
        # and `-DNAME="a b"` gave two words.
        command = entry.get("command")
        tokens = command_words(str(command)) if command else []
    found: dict[str, str] = {}
    expect_value = False
    for token in tokens:
        text = str(token)
        if expect_value:
            name, _, value = text.partition("=")
            found[name] = value
            expect_value = False
            continue
        if text == "-D":
            expect_value = True
        elif text.startswith("-D") and len(text) > 2:
            name, _, value = text[2:].partition("=")
            found[name] = value
    return found


def command_line_defines(cc_path: Path) -> tuple[dict[str, str], int]:
    """Return the defines EVERY unit shares, and how many names vary.

    The `-D` flags of a build are its configuration.  The Mbed project passes
    `APPLICATION_ADDR=0x10200`, `APPLICATION_SIZE=0xefe00`, and
    `CMSIS_VECTAB_VIRTUAL` — the memory map and the fact that the vector
    table moves to RAM, stated by the build itself.

    WHY only the shared set: a tool that describes ONE build must not show
    the defines of one file as the defines of the build.  Measured:
    The Mbed project has 881 units, 27 names in every one and 59 in only some —
    the three `.S` files get a shorter set.  The ESP32 project splits on
    `ARDUINO_CORE_BUILD`, which 46 of its 114 units carry.  The second
    return value counts the names left out, so a caller can tell "not
    defined" from "not defined everywhere".

    WHY this reads the JSON itself instead of calling `parse`: `parse`
    normalizes every flag for libclang, which costs 6966 ms on the Mbed project
    against 19 ms here — measured.  `get_build_info` gives the defines,
    and seven seconds of flag normalization to read 27 defines
    would be paid at each call.

    WHY not the `macros` table: that holds every macro the preprocessor
    saw — 27800 distinct names on the STM32 project, 23507 on the Mbed project.  Three orders
    of magnitude more, and almost all of it comes from the headers and the
    compiler rather than from the build.

    Returns an empty dict and 0 when the file is unreadable, also when the
    ``command`` of an entry cannot be split (a quote that does not end):
    ``parse`` refuses the same file.  A missing answer is correct there; an
    invented one is not.  The file is read as ``parse`` reads it
    (``utf-8-sig``): a file with a byte order mark gave no defines here
    while ``parse`` indexed it.

    No log line on that path, and this module keeps no logger on purpose.
    An unreadable compile_commands.json is not a detail a caller could
    miss: the whole index is built from it, and `get_project_status` reports
    that state on its own.
    """
    try:
        entries = load_entries(cc_path)
    except (OSError, ValueError):  # ValueError: also UnicodeDecodeError
        return {}, 0
    if not isinstance(entries, list):
        return {}, 0

    shared: dict[str, str] | None = None
    every_name: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            one = _defines_of_entry(entry)
        except ValueError:
            # utils.command_words: a quote that does not end.
            return {}, 0
        every_name |= set(one)
        if shared is None:
            shared = dict(one)
        else:
            # A name survives only with the SAME value everywhere.  Two
            # units that define one name differently do not agree on it,
            # and reporting either value would be a guess.
            shared = {
                name: value
                for name, value in shared.items()
                if name in one and one[name] == value
            }
    if shared is None:
        return {}, 0
    return shared, len(every_name) - len(shared)


def compile_directory(cc_path: Path) -> Path | None:
    """Return the directory the compiler worked in, or None.

    The ``directory`` field of an entry is the working directory of that
    compilation, which is the build tree.  The location of
    compile_commands.json is NOT that tree: fw-context writes its own copy
    under ``.fw-context/build/``, one file per variant and image, so the
    parent of that file holds no build artifact at all.

    Measured on the Zephyr project: the parent is ``.fw-context/build``,
    while ``directory`` is ``build/nrf52840_dev/stage0`` — where
    ``zephyr/zephyr_pre0.elf`` and its ``.intList`` section really are.
    A caller that took the parent found nothing and had no way to tell
    "no artifact" from "wrong directory".

    WHY this reads the JSON itself instead of calling `parse`: the same
    reason `command_line_defines` does — flag normalization costs three
    orders of magnitude more than reading one field, and this answers a
    question about the build, not about a translation unit.

    The first entry that names a directory answers.  The entries of one
    build configuration are compiled in one tree, and an entry without
    the field says nothing about where that tree is.

    Returns None when the file is unreadable or names no directory.  The
    caller then has the same answer it had before this existed, which is
    "not known" rather than a guess.
    """
    try:
        entries = load_entries(cc_path)
    except (OSError, ValueError):  # ValueError: also UnicodeDecodeError
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("directory"):
            return Path(str(entry["directory"]))
    return None
